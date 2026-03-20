from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


class MessageSender:
    def __init__(self, settings: Settings) -> None:
        self.max_length = settings.max_message_length
        self._send_lock = asyncio.Lock()

    async def _run_cmd(self, *args: str) -> int:
        """Run a command silently and return its exit code."""
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode

    async def _ensure_messages_running(self) -> bool:
        """Ensure Messages.app is running, launching it if needed.

        Uses pgrep (fast, no side effects) to check, and ``open -g -j -a``
        to launch without stealing focus. Polls readiness up to 10s.
        Returns True if Messages is running and responsive.
        """
        if await self._run_cmd("pgrep", "-x", "Messages") == 0:
            return True

        log.warning("Messages.app not running, launching")
        await self._run_cmd("open", "-g", "-j", "-a", "Messages")
        for _ in range(10):
            await asyncio.sleep(1)
            if await self._run_cmd(
                "osascript", "-e",
                'tell application "Messages" to count of accounts',
            ) == 0:
                log.info("Messages.app is ready")
                return True
        log.error("Messages.app launched but not responding after 10s")
        return False

    async def _restart_messages(self) -> bool:
        """Quit and relaunch Messages.app as a last resort for stuck state.

        Returns True if Messages restarted and is responsive.
        """
        log.warning("Restarting Messages.app (last-resort recovery)")

        # Try graceful quit first
        quit_proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", 'tell application "Messages" to quit',
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(quit_proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            quit_proc.kill()
            await quit_proc.wait()

        # Wait for Messages to exit, fall back to killall
        for _ in range(5):
            await asyncio.sleep(1)
            if await self._run_cmd("pgrep", "-x", "Messages") != 0:
                break
        else:
            log.warning("Messages.app didn't quit gracefully, using killall")
            await self._run_cmd("killall", "Messages")
            await asyncio.sleep(2)

        # Relaunch and wait for readiness
        ready = await self._ensure_messages_running()
        if ready:
            log.info("Messages.app restarted successfully")
        else:
            log.error("Messages.app failed to restart")
        return ready

    async def _run_applescript(self, script: str, timeout: int = 10) -> tuple[bool, str]:
        """Run an AppleScript and return (success, output)."""
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("AppleScript timed out after %ds", timeout)
            return False, "timeout"
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="ignore").strip()
            if "not allowed to send keystrokes" in error:
                log.error(
                    "Accessibility permission denied — remove and re-add BluePopcorn.app "
                    "in System Settings > Privacy & Security > Accessibility"
                )
            else:
                log.error("AppleScript error: %s", error)
            return False, error
        return True, stdout.decode("utf-8", errors="ignore").strip()

    def _build_gallery_script(self, phone: str, image_paths: list[str]) -> str:
        """Build AppleScript to send multiple images as a grouped gallery.

        Uses ASObjC bridge to put file URLs on NSPasteboard, then GUI-scripts
        a Cmd+V paste + Return into Messages. Images arrive as a single
        swipeable gallery on iOS.
        """
        safe_phone = self._sanitize_phone(phone)
        url_lines = "\n".join(
            f'    urls\'s addObject:(current application\'s |NSURL|\'s fileURLWithPath:"{self._escape_applescript(p)}")'
            for p in image_paths
        )
        # Scale render delay with image count: 2s base + 0.3s per image beyond 2
        render_delay = 2.0 + max(0, len(image_paths) - 2) * 0.3
        return f'''use framework "AppKit"
use scripting additions

-- 1. Set clipboard to file URLs via NSPasteboard
set pb to current application's NSPasteboard's generalPasteboard()
pb's clearContents()
set urls to current application's NSMutableArray's array()
{url_lines}
pb's writeObjects:urls

-- 2. Open conversation
tell application "Messages" to activate
delay 0.3
open location "imessage://{safe_phone}"
delay 0.7

-- 3. Clear compose, paste, wait for render, send
tell application "System Events"
    tell process "Messages"
        keystroke "a" using command down
        key code 51
        delay 0.2
        keystroke "v" using command down
        delay {render_delay}
        key code 36
    end tell
end tell
'''

    async def _send_images_gallery(self, phone: str, image_paths: list[str]) -> bool:
        """Attempt to send images as a grouped gallery via clipboard+paste.

        Returns True on success, False if the gallery approach failed
        (caller should fall back to individual sends).
        Assumes paths are already validated by send_images().
        """
        script = self._build_gallery_script(phone, image_paths)
        # Longer timeout: script has ~3s+ of built-in delays
        timeout = 15 + len(image_paths)
        ok, err = await self._run_applescript(script, timeout=timeout)
        if ok:
            log.info("Sent %d images as gallery to %s", len(image_paths), phone)
            return True
        log.warning("Gallery send failed: %s", err)
        return False

    async def _clear_compose_field(self) -> None:
        """Clear the Messages compose field (Cmd+A, Delete).

        Used to clean up after a failed gallery paste before falling back
        to individual sends.
        """
        script = '''
tell application "System Events"
    tell process "Messages"
        keystroke "a" using command down
        key code 51
    end tell
end tell
'''
        ok, _ = await self._run_applescript(script, timeout=5)
        if ok:
            log.debug("Compose field cleared")

    def _build_send_image_script(self, phone: str, image_path: str) -> str:
        """Build AppleScript to send an image file.

        Image must be in ~/Pictures/ due to Messages.app sandbox.
        """
        safe_phone = self._sanitize_phone(phone)
        safe_path = self._escape_applescript(image_path)
        return f'''
tell application "Messages"
    set targetAccount to first account whose service type = iMessage
    set targetParticipant to participant "{safe_phone}" of targetAccount
    send POSIX file "{safe_path}" to targetParticipant
end tell
'''

    @staticmethod
    def _sanitize_phone(phone: str) -> str:
        """Validate phone is a safe format for AppleScript interpolation."""
        if not re.match(r"^[\+\d@.\w-]+$", phone):
            raise ValueError(f"Invalid phone format: {phone}")
        return phone

    @staticmethod
    def _escape_applescript(s: str) -> str:
        """Escape a string for safe AppleScript interpolation."""
        return s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")

    async def _send_text_once(self, phone: str, message: str) -> tuple[bool, str]:
        """Send a text message via a temp file to avoid AppleScript escaping issues.

        Writes the message to a temp file and reads it in AppleScript via
        ``read (POSIX file ...) as «class utf8»``, so the message content
        never enters a string literal. Safe for emoji, quotes, backslashes, etc.
        """
        safe_phone = self._sanitize_phone(phone)
        fd, path = tempfile.mkstemp(prefix="bp_msg_", suffix=".txt")
        try:
            os.write(fd, message.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            safe_path = self._escape_applescript(path)
            script = f'''
set msgText to read (POSIX file "{safe_path}") as «class utf8»
tell application "Messages"
    set targetAccount to first account whose service type = iMessage
    set targetParticipant to participant "{safe_phone}" of targetAccount
    send msgText to targetParticipant
end tell
'''
            return await self._run_applescript(script)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def start_typing(self, phone: str) -> None:
        """Trigger typing indicator by putting text in the compose field.

        Uses System Events GUI automation. Requires Accessibility permissions.
        Best-effort — skipped if a send is in progress to avoid GUI interference.
        """
        if self._send_lock.locked():
            return
        phone = self._sanitize_phone(phone)
        script = f'''
tell application "Messages" to activate
delay 0.3
open location "imessage://{phone}"
delay 0.7
tell application "System Events"
    tell process "Messages"
        keystroke "a" using command down
        key code 51
        delay 0.1
        keystroke "."
    end tell
end tell
'''
        ok, err = await self._run_applescript(script)
        if ok:
            log.debug("Typing indicator started for %s", phone)

    async def stop_typing(self) -> None:
        """Clear the compose field to stop typing indicator.

        Best-effort — skipped if a send is in progress to avoid GUI interference.
        """
        if self._send_lock.locked():
            return
        script = '''
tell application "System Events"
    tell process "Messages"
        keystroke "a" using command down
        key code 51
    end tell
end tell
'''
        ok, _ = await self._run_applescript(script)
        if ok:
            log.debug("Typing indicator cleared")

    async def _dismiss_error_dialogs(self) -> None:
        """Best-effort dismissal of modal error dialogs in Messages.app.

        Send failures can leave sheets or extra windows that block all future
        sends. Press Escape first (dismisses sheets/popovers), then click
        Ignore/OK/button 1 on any remaining extra windows.

        Calls osascript directly (bypasses _run_applescript) to avoid
        redundant health checks inside the retry loop.
        """
        try:
            script = '''
tell application "System Events"
    tell process "Messages"
        -- Escape dismisses sheets and popovers (only if one exists)
        if exists sheet 1 of window 1 then
            key code 53
            delay 0.2
        end if
        -- If an extra window appeared (error dialog), try standard buttons
        if (count of windows) > 1 then
            tell window 1
                if exists button "Ignore" then
                    click button "Ignore"
                else if exists button "OK" then
                    click button "OK"
                else if exists button 1 then
                    click button 1
                end if
            end tell
        end if
    end tell
end tell
'''
            proc = await asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception as e:
            log.debug("Failed to dismiss error dialogs: %s", e)

    async def send_text(self, phone: str, message: str, retries: int = 3) -> bool:
        """Send a text message via iMessage, chunking if needed.

        Retries up to `retries` times with exponential backoff.
        """
        if not await self._ensure_messages_running():
            log.error("Cannot send message: Messages.app not available")
            return False
        async with self._send_lock:
            chunks = self._chunk_message(message)
            for i, chunk in enumerate(chunks):
                sent = False
                for attempt in range(retries):
                    success, error = await self._send_text_once(phone, chunk)
                    if success:
                        log.info("Sent message to %s (chunk %d/%d)", phone, i + 1, len(chunks))
                        sent = True
                        break
                    backoff = 2 ** attempt
                    log.warning(
                        "Send failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, retries, backoff, error,
                    )
                    await self._dismiss_error_dialogs()
                    await asyncio.sleep(backoff)
                if not sent:
                    # Last resort: restart Messages and try once more
                    if await self._restart_messages():
                        success, error = await self._send_text_once(phone, chunk)
                        if success:
                            log.info("Sent message to %s after restart (chunk %d/%d)", phone, i + 1, len(chunks))
                            sent = True
                    if not sent:
                        log.error("Failed to send message to %s after %d retries + restart", phone, retries)
                        return False
                # Delay between chunks to maintain order
                if i < len(chunks) - 1:
                    await asyncio.sleep(1)
            return True

    async def send_image(self, phone: str, image_path: str, retries: int = 3) -> bool:
        """Send an image via iMessage.

        Image must be in ~/Pictures/ due to Messages.app sandbox restriction.
        """
        pictures_dir = str(Path.home() / "Pictures")
        resolved = str(Path(image_path).resolve())
        if not resolved.startswith(pictures_dir + "/") and resolved != pictures_dir:
            log.error(
                "Image path %s is not in ~/Pictures/, refusing to send", image_path
            )
            return False

        if not await self._ensure_messages_running():
            log.error("Cannot send image: Messages.app not available")
            return False
        async with self._send_lock:
            for attempt in range(retries):
                script = self._build_send_image_script(phone, image_path)
                success, error = await self._run_applescript(script)
                if success:
                    log.info("Sent image to %s: %s", phone, image_path)
                    return True
                backoff = 2 ** attempt
                log.warning(
                    "Image send failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, retries, backoff, error,
                )
                await self._dismiss_error_dialogs()
                await asyncio.sleep(backoff)
            # Last resort: restart Messages and try once more
            if await self._restart_messages():
                script = self._build_send_image_script(phone, image_path)
                success, error = await self._run_applescript(script)
                if success:
                    log.info("Sent image to %s after restart: %s", phone, image_path)
                    return True
            log.error("Failed to send image to %s after %d retries + restart", phone, retries)
            return False

    async def send_images(self, phone: str, image_paths: list[str], retries: int = 3) -> bool:
        """Send multiple images via iMessage, grouped as a gallery when possible.

        Tries clipboard+paste gallery first (images arrive as a single swipeable
        group on iOS). Falls back to one-by-one sends if gallery fails.
        Images must be in ~/Pictures/ due to Messages.app sandbox restriction.
        """
        pictures_dir = str(Path.home() / "Pictures")
        for path in image_paths:
            resolved = str(Path(path).resolve())
            if not resolved.startswith(pictures_dir + "/") and resolved != pictures_dir:
                log.error("Image path %s is not in ~/Pictures/, refusing to send", path)
                return False

        if not await self._ensure_messages_running():
            log.error("Cannot send images: Messages.app not available")
            return False

        # Try clipboard+paste gallery (grouped photos on iOS) — skip for single image
        async with self._send_lock:
            if len(image_paths) >= 2 and await self._send_images_gallery(phone, image_paths):
                return True
            if len(image_paths) >= 2:
                log.warning("Gallery send failed, falling back to individual sends")
                await self._clear_compose_field()

            # Fall back to one-by-one sends
            for i, image_path in enumerate(image_paths):
                sent = False
                for attempt in range(retries):
                    script = self._build_send_image_script(phone, image_path)
                    success, error = await self._run_applescript(script)
                    if success:
                        log.info("Sent image %d/%d to %s", i + 1, len(image_paths), phone)
                        sent = True
                        break
                    backoff = 2 ** attempt
                    log.warning(
                        "Image send failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, retries, backoff, error,
                    )
                    await self._dismiss_error_dialogs()
                    await asyncio.sleep(backoff)
                if not sent:
                    if await self._restart_messages():
                        script = self._build_send_image_script(phone, image_path)
                        success, error = await self._run_applescript(script)
                        if success:
                            log.info("Sent image %d/%d to %s after restart", i + 1, len(image_paths), phone)
                            sent = True
                    if not sent:
                        log.error("Failed to send image %d/%d to %s after %d retries + restart", i + 1, len(image_paths), phone, retries)
                        return False
                # 1s delay between images for iOS stacking
                if i < len(image_paths) - 1:
                    await asyncio.sleep(1)
        return True

    def _chunk_message(self, message: str) -> list[str]:
        """Split a message into chunks at word boundaries."""
        if len(message) <= self.max_length:
            return [message]

        chunks: list[str] = []
        while message:
            if len(message) <= self.max_length:
                chunks.append(message)
                break
            # Find last space before limit
            split_at = message.rfind(" ", 0, self.max_length)
            if split_at == -1:
                split_at = self.max_length
            chunks.append(message[:split_at])
            message = message[split_at:].lstrip()
        return chunks
