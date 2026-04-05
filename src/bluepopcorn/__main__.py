from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import logging.handlers
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

from .actions import ActionExecutor
from .cli import cli_mode
from .compression import Compressor
from .config import Settings, load_settings
from .memory import UserMemory
from .morning_digest import MorningDigest
from .llm import LLMAuthError, LLMClient
from .monitor import MessageMonitor
from .posters import PosterHandler
from .seerr import SeerrClient
from .sender import MessageSender
from .types import IncomingMessage
from .prompts import ERROR_AUTH, ERROR_GENERIC
from .request_tracker import RequestTracker
from .utils import mask_phone, safe_data_path
from .watcher import ChatDBWatcher
from .webhooks import WebhookServer

log = logging.getLogger("bluepopcorn")

# Will be set from settings in run_daemon()
_last_rowid_path: Path | None = None


def _read_last_rowid() -> int | None:
    """Read the last processed ROWID from file."""
    if _last_rowid_path is None:
        raise RuntimeError("_last_rowid_path not initialized — run_daemon() must be called first")
    try:
        return int(_last_rowid_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_last_rowid(rowid: int) -> None:
    """Write the last processed ROWID to file (atomic)."""
    if _last_rowid_path is None:
        raise RuntimeError("_last_rowid_path not initialized — run_daemon() must be called first")
    _last_rowid_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _last_rowid_path.with_suffix(".tmp")
    try:
        tmp.write_text(str(rowid))
        tmp.rename(_last_rowid_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def setup_logging(level: str, log_path: str = "bluepopcorn.log") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Clear any existing handlers to avoid duplicates
    root.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # When running under launchd, stdout already goes to the log file,
    # so only add a FileHandler to avoid duplicate lines.
    if sys.stdout.isatty():
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(fmt)
        root.addHandler(stdout_handler)
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5_000_000, backupCount=3
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    # Quiet down httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load_last_digest(data_dir: Path, sender: str) -> str | None:
    """Read the last digest text sent to this user."""
    path = safe_data_path(data_dir, "last_digest", sender)
    try:
        return path.read_text()
    except FileNotFoundError:
        return None


def _save_last_digest(data_dir: Path, sender: str, text: str) -> None:
    """Persist the digest text for dedup on next run."""
    path = safe_data_path(data_dir, "last_digest", sender)
    # Strip angle brackets — this text is re-injected into the next prompt
    sanitized = text.replace("<", "").replace(">", "")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(sanitized)
        tmp.chmod(0o600)
        tmp.rename(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


async def _send_digest_to_all(
    settings: Settings,
    digest: MorningDigest,
    data_dir: Path,
    msg_sender: MessageSender,
    *,
    on_skip: Callable[[str], None],
    on_send: Callable[[str, str], None],
    on_error: Callable[[str, Exception], None],
) -> None:
    """Fetch data and send per-user digests to all allowed senders.

    Shared between the one-shot CLI (``run_digest``) and the daemon
    scheduler (``_schedule_digest``).  Callers provide callbacks for
    skip / send / error reporting so each site can log in its own style.
    """
    available, pending = await asyncio.gather(
        digest.fetch_available(),
        digest.fetch_pending(),
    )

    for phone in settings.allowed_senders:
        try:
            last = _load_last_digest(data_dir, phone)
            text = await digest.build(
                sender=phone, last_digest=last,
                available=available, pending=pending,
            )
            if text is None:
                on_skip(phone)
                continue
            await msg_sender.send_text(phone, text)
            _save_last_digest(data_dir, phone, text)
            on_send(phone, text)
        except Exception as e:
            on_error(phone, e)


async def run_digest(settings: Settings) -> None:
    """One-shot digest: print and optionally send."""
    seerr = SeerrClient(settings)
    llm = LLMClient(settings)
    memory = UserMemory(settings)
    data_dir = settings.resolve_path(settings.data_dir)
    msg_sender = MessageSender(settings)

    try:
        digest = MorningDigest(settings, seerr, llm, memory)
        await _send_digest_to_all(
            settings, digest, data_dir, msg_sender,
            on_skip=lambda ph: print(f"[{mask_phone(ph)}] Skipped — nothing new to report"),
            on_send=lambda ph, t: print(f"[{mask_phone(ph)}] {t}"),
            on_error=lambda ph, e: print(f"[{mask_phone(ph)}] Digest failed: {e}"),
        )
    finally:
        await seerr.close()
        await llm.close()


async def run_daemon(settings: Settings) -> None:
    """Main async daemon loop."""
    global _last_rowid_path
    _last_rowid_path = settings.resolve_path(settings.data_dir) / "last_rowid"

    log.info("Starting BluePopcorn daemon")

    # Init components
    seerr = SeerrClient(settings)
    llm = LLMClient(settings)
    sender = MessageSender(settings)
    posters = PosterHandler(settings)
    memory = UserMemory(settings)
    monitor = MessageMonitor(settings)

    # Ensure poster dir exists
    settings.resolve_path(settings.poster_dir).mkdir(parents=True, exist_ok=True)

    # Trigger Accessibility permission prompt on first run
    # (System Events keystroke requires Accessibility access)
    await _check_accessibility()

    # Request tracker for targeted notifications
    request_tracker = RequestTracker(settings.resolve_path(settings.data_dir))

    executor = ActionExecutor(
        seerr=seerr,
        llm=llm,
        sender=sender,
        posters=posters,
        memory=memory,
        monitor=monitor,
        settings=settings,
        request_tracker=request_tracker,
    )

    # Initialize ROWID cursor (file-based)
    last_rowid = _read_last_rowid()
    if last_rowid is None:
        last_rowid = await monitor.get_max_rowid()
        _write_last_rowid(last_rowid)
        log.info("Initialized ROWID cursor to %d", last_rowid)
    else:
        log.info("Resuming from ROWID %d", last_rowid)

    # Webhook server for Seerr notifications
    async def send_notification(message: str, target_phone: str | None = None) -> None:
        if _is_quiet_hours(settings):
            log.info("Quiet hours — skipping webhook notification: %s", message[:80])
            return
        if target_phone:
            if target_phone in settings.allowed_senders:
                await sender.send_text(target_phone, message)
            else:
                log.warning("Skipping notification for stale phone %s", mask_phone(target_phone))
        else:
            for phone in settings.allowed_senders:
                await sender.send_text(phone, message)

    webhook_server = WebhookServer(settings, on_notification=send_notification, request_tracker=request_tracker)
    await webhook_server.start()

    # Digest + compression scheduler
    compressor = Compressor(settings, llm, monitor, memory)
    digest_task = asyncio.create_task(
        _schedule_digest(settings, seerr, llm, memory, sender, compressor)
    )

    # Shutdown event
    shutdown = asyncio.Event()

    def _signal_handler() -> None:
        log.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Per-sender locks
    sender_locks: dict[str, asyncio.Lock] = {}

    # Track background tasks
    background_tasks: set[asyncio.Task] = set()

    def _track_task(task: asyncio.Task) -> None:
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    _track_task(digest_task)

    # kqueue file watcher for faster message detection
    watcher = ChatDBWatcher(settings.chat_db_path)
    watcher.start(loop)

    log.info("Daemon running. Polling every %.1fs.", settings.poll_interval)

    consecutive_errors = 0

    try:
        while not shutdown.is_set():
            try:
                messages = await monitor.get_new_messages(last_rowid)
                consecutive_errors = 0  # Reset on success
            except Exception as e:
                consecutive_errors += 1
                # Exponential backoff: 0.5s, 1s, 2s, 4s, ... capped at 30s
                backoff = min(settings.poll_interval * (2 ** (consecutive_errors - 1)), 30)
                if consecutive_errors <= 3 or consecutive_errors % 20 == 0:
                    log.error("Monitor error (attempt %d, next retry in %.0fs): %s", consecutive_errors, backoff, e)
                await asyncio.sleep(backoff)
                continue

            # Advance cursor for all messages
            for msg in messages:
                if msg.rowid > last_rowid:
                    last_rowid = msg.rowid
                    _write_last_rowid(last_rowid)

            # Group by sender, keep only the latest message per sender
            latest_by_sender: dict[str, IncomingMessage] = {}
            for msg in messages:
                latest_by_sender[msg.sender] = msg

            for msg in latest_by_sender.values():
                log.info("Message from %s", mask_phone(msg.sender))
                log.debug("Message from %s: %s", mask_phone(msg.sender), msg.text[:100])

                # Process with per-sender lock (debounce happens inside)
                lock = sender_locks.setdefault(msg.sender, asyncio.Lock())
                task = asyncio.create_task(
                    _process_message(msg, lock, executor, sender, settings, monitor)
                )
                _track_task(task)

            await watcher.wait(settings.poll_interval)

    finally:
        log.info("Shutting down...")
        watcher.stop()
        digest_task.cancel()
        # Cancel and await all background tasks
        for task in list(background_tasks):
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        await webhook_server.stop()
        await monitor.close()
        await seerr.close()
        await posters.close()
        await llm.close()
        log.info("Shutdown complete")


async def _process_message(msg, lock, executor, sender, settings, monitor):
    """Process a single message with per-sender locking and debounce."""
    async with lock:
        # Debounce: wait and check for newer messages from same sender
        await asyncio.sleep(settings.debounce_delay)
        try:
            newer = await monitor.get_new_messages(msg.rowid)
            newer_from_same = [m for m in newer if m.sender == msg.sender]
            if newer_from_same:
                log.debug("Skipping debounced message from %s", mask_phone(msg.sender))
                return
        except Exception as e:
            log.warning("Debounce check failed (proceeding anyway): %s", e)

        try:
            log.info("IN  %s: %s", mask_phone(msg.sender), msg.text[:200])
            # Show typing indicator while processing
            await sender.start_typing(msg.sender)
            response = await executor.handle_message(msg.sender, msg.text)
            await sender.stop_typing()
            log.info("OUT %s: %s", mask_phone(msg.sender), response[:200])
            await sender.send_text(msg.sender, response)
        except LLMAuthError as e:
            log.error("Auth error for %s: %s", mask_phone(msg.sender), e)
            await sender.stop_typing()
            try:
                await sender.send_text(msg.sender, ERROR_AUTH)
            except Exception:
                log.error("Failed to send auth error to %s", mask_phone(msg.sender))
        except Exception as e:
            log.error("Error processing message from %s: %s", mask_phone(msg.sender), e)
            await sender.stop_typing()
            try:
                await sender.send_text(
                    msg.sender, ERROR_GENERIC
                )
            except Exception:
                log.error("Failed to send error message to %s", mask_phone(msg.sender))


async def _check_accessibility() -> None:
    """Trigger Accessibility permission prompt on first run.

    Sends a no-op System Events command so macOS shows the permission
    dialog attributed to the running binary (e.g. 'BluePopcorn').
    """
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e",
        'tell application "System Events" to return name of first process',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="ignore").strip()
        log.warning("Accessibility not granted yet: %s", err)
    else:
        log.info("Accessibility permission OK")


def _is_quiet_hours(settings: Settings) -> bool:
    """Check if the current time falls within quiet hours."""
    tz = ZoneInfo(settings.timezone)
    now = datetime.datetime.now(tz)
    current_time = now.time()

    start_h, start_m = map(int, settings.quiet_start.split(":"))
    end_h, end_m = map(int, settings.quiet_end.split(":"))
    start = datetime.time(start_h, start_m)
    end = datetime.time(end_h, end_m)

    if start <= end:
        # Same-day range (e.g. 08:00 - 18:00)
        return start <= current_time < end
    else:
        # Overnight range (e.g. 22:00 - 07:00)
        return current_time >= start or current_time < end


async def _schedule_digest(
    settings: Settings,
    seerr: SeerrClient,
    llm: LLMClient,
    memory: UserMemory,
    msg_sender: MessageSender,
    compressor: Compressor,
) -> None:
    """Schedule daily morning digest + memory compression."""
    tz = ZoneInfo(settings.timezone)
    data_dir = settings.resolve_path(settings.data_dir)

    hour, minute = map(int, settings.digest_time.split(":"))

    while True:
        now = datetime.datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += datetime.timedelta(days=1)

        wait_seconds = (target - now).total_seconds()
        log.info("Next digest at %s (in %.0f seconds)", target, wait_seconds)

        await asyncio.sleep(wait_seconds)

        try:
            digest = MorningDigest(settings, seerr, llm, memory)
            await _send_digest_to_all(
                settings, digest, data_dir, msg_sender,
                on_skip=lambda ph: log.info("Digest skipped for %s (nothing new)", mask_phone(ph)),
                on_send=lambda ph, t: log.info("Digest sent to %s: %s", mask_phone(ph), t[:80]),
                on_error=lambda ph, e: log.error("Digest failed for %s: %s", mask_phone(ph), e),
            )
        except Exception as e:
            log.error("Digest fetch failed: %s", e)

        # Run compression after digest for each allowed sender
        for phone in settings.allowed_senders:
            try:
                await compressor.run_compression(phone)
            except Exception as e:
                log.error("Compression failed for %s: %s", mask_phone(phone), e)

        # Sleep past the target minute to prevent double-fire from sleep imprecision
        now = datetime.datetime.now(tz)
        seconds_left_in_minute = 60 - now.second
        if seconds_left_in_minute < 60:
            await asyncio.sleep(seconds_left_in_minute + 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="BluePopcorn - iMessage Seerr bot")
    parser.add_argument("--cli", action="store_true", help="Run in CLI test mode")
    parser.add_argument("--digest", action="store_true", help="Run one-shot digest")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging(settings.log_level, str(settings.resolve_path(settings.log_path)))

    if args.cli:
        asyncio.run(cli_mode())
    elif args.digest:
        asyncio.run(run_digest(settings))
    else:
        asyncio.run(run_daemon(settings))


if __name__ == "__main__":
    main()
