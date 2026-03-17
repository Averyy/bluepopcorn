from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import logging.handlers
import signal
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from .actions import ActionExecutor, ERROR_GENERIC
from .cli import cli_mode
from .config import Settings, load_settings
from .db import BotDatabase
from .morning_digest import MorningDigest
from .llm import LLMClient
from .monitor import MessageMonitor
from .posters import PosterHandler
from .seerr import SeerrClient
from .sender import MessageSender
from .types import IncomingMessage
from .webhooks import WebhookServer

log = logging.getLogger("imessagarr")


def setup_logging(level: str, log_path: str = "imessagarr.log") -> None:
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


async def run_digest(settings: Settings) -> None:
    """One-shot digest: print and optionally send."""
    seerr = SeerrClient(settings)
    try:
        await seerr.authenticate()
    except Exception as e:
        print(f"Seerr auth failed: {e}")

    digest = MorningDigest(settings, seerr)
    text = await digest.build()
    print(text)

    # Also send to all allowed senders if running as daemon
    sender = MessageSender(settings)
    for phone in settings.allowed_senders:
        await sender.send_text(phone, text)

    await digest.close()
    await seerr.close()


async def run_daemon(settings: Settings) -> None:
    """Main async daemon loop."""
    log.info("Starting iMessagarr daemon")

    # Init components
    seerr = SeerrClient(settings)
    llm = LLMClient(settings)
    sender = MessageSender(settings)
    posters = PosterHandler(settings)
    db = BotDatabase(settings)
    monitor = MessageMonitor(settings)

    await db.init()

    for attempt in range(1, 13):  # retry up to ~2 minutes
        try:
            await seerr.authenticate()
            break
        except Exception as e:
            if attempt == 12:
                log.error("Seerr auth failed after 12 attempts: %s", e)
                log.warning("Starting without Seerr — search/request won't work")
            else:
                delay = min(attempt * 5, 15)
                log.warning("Seerr auth attempt %d failed: %s — retrying in %ds", attempt, e, delay)
                await asyncio.sleep(delay)

    # Ensure poster dir exists
    settings.resolve_path(settings.poster_dir).mkdir(parents=True, exist_ok=True)

    # Trigger Accessibility permission prompt on first run
    # (System Events keystroke requires Accessibility access)
    await _check_accessibility()

    executor = ActionExecutor(
        seerr=seerr,
        llm=llm,
        sender=sender,
        posters=posters,
        db=db,
        settings=settings,
    )

    # Initialize ROWID cursor
    last_rowid = await db.get_last_rowid()
    if last_rowid is None:
        last_rowid = await monitor.get_max_rowid()
        await db.set_last_rowid(last_rowid)
        log.info("Initialized ROWID cursor to %d", last_rowid)
    else:
        log.info("Resuming from ROWID %d", last_rowid)

    # Webhook server for Seerr notifications
    async def send_to_all(message: str) -> None:
        if _is_quiet_hours(settings):
            log.info("Quiet hours — skipping webhook notification: %s", message[:80])
            return
        for phone in settings.allowed_senders:
            await sender.send_text(phone, message)

    webhook_server = WebhookServer(settings, on_notification=send_to_all)
    await webhook_server.start()

    # Digest scheduler
    digest_task = asyncio.create_task(_schedule_digest(settings, seerr, sender))

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
                    await db.set_last_rowid(last_rowid)

            # Group by sender, keep only the latest message per sender
            latest_by_sender: dict[str, IncomingMessage] = {}
            for msg in messages:
                latest_by_sender[msg.sender] = msg

            for msg in latest_by_sender.values():
                log.info("Message from %s", msg.sender)
                log.debug("Message from %s: %s", msg.sender, msg.text[:100])

                # Process with per-sender lock (debounce happens inside)
                lock = sender_locks.setdefault(msg.sender, asyncio.Lock())
                task = asyncio.create_task(
                    _process_message(msg, lock, executor, sender, settings, monitor)
                )
                _track_task(task)

            await asyncio.sleep(settings.poll_interval)

    finally:
        log.info("Shutting down...")
        digest_task.cancel()
        # Cancel and await all background tasks
        for task in list(background_tasks):
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        await webhook_server.stop()
        await monitor.close()
        await db.close()
        await seerr.close()
        await posters.close()
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
                log.debug("Skipping debounced message from %s", msg.sender)
                return
        except Exception as e:
            log.debug("Debounce check failed: %s", e)

        try:
            log.info("IN  %s: %s", msg.sender, msg.text[:200])
            # Show typing indicator while processing
            await sender.start_typing(msg.sender)
            response = await executor.handle_message(msg.sender, msg.text)
            await sender.stop_typing()
            log.info("OUT %s: %s", msg.sender, response[:200])
            await sender.send_text(msg.sender, response)
        except Exception as e:
            log.error("Error processing message from %s: %s", msg.sender, e)
            await sender.stop_typing()
            try:
                await sender.send_text(
                    msg.sender, ERROR_GENERIC
                )
            except Exception:
                log.error("Failed to send error message to %s", msg.sender)


async def _check_accessibility() -> None:
    """Trigger Accessibility permission prompt on first run.

    Sends a no-op System Events command so macOS shows the permission
    dialog attributed to the running binary (e.g. 'iMessagarr').
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
    settings: Settings, seerr: SeerrClient, msg_sender: MessageSender
) -> None:
    """Schedule daily morning digest."""
    tz = ZoneInfo(settings.timezone)

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
            digest = MorningDigest(settings, seerr)
            text = await digest.build()
            log.info("Digest: %s", text)
            for phone in settings.allowed_senders:
                await msg_sender.send_text(phone, text)
            await digest.close()
        except Exception as e:
            log.error("Digest failed: %s", e)

        # Sleep past the target minute to prevent double-fire from sleep imprecision
        now = datetime.datetime.now(tz)
        seconds_left_in_minute = 60 - now.second
        if seconds_left_in_minute < 60:
            await asyncio.sleep(seconds_left_in_minute + 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="iMessagarr - iMessage Seerr bot")
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
