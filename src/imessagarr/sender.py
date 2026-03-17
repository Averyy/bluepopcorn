from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .config import Settings

log = logging.getLogger(__name__)


class MessageSender:
    def __init__(self, settings: Settings) -> None:
        self.bot_apple_id = settings.bot_apple_id
        self.max_length = settings.max_message_length

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
                    "Accessibility permission denied — remove and re-add iMessagarr.app "
                    "in System Settings > Privacy & Security > Accessibility"
                )
            else:
                log.error("AppleScript error: %s", error)
            return False, error
        return True, stdout.decode("utf-8", errors="ignore").strip()

    def _build_send_text_script(self, phone: str, message: str) -> str:
        """Build AppleScript to send a text message.

        Uses account/participant pattern for macOS Tahoe (26+).
        """
        safe_phone = self._sanitize_phone(phone)
        escaped = self._escape_applescript(message)
        return f'''
tell application "Messages"
    set targetAccount to first account whose service type = iMessage
    set targetParticipant to participant "{safe_phone}" of targetAccount
    send "{escaped}" to targetParticipant
end tell
'''

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

    async def start_typing(self, phone: str) -> None:
        """Trigger typing indicator by putting text in the compose field.

        Uses System Events GUI automation. Requires Accessibility permissions.
        Best-effort — failures are logged but don't block the bot.
        """
        script = f'''
tell application "Messages" to activate
delay 0.3
open location "imessage://{phone}"
delay 0.5
tell application "System Events"
    tell process "Messages"
        keystroke "."
    end tell
end tell
'''
        ok, err = await self._run_applescript(script)
        if ok:
            log.debug("Typing indicator started for %s", phone)

    async def stop_typing(self) -> None:
        """Clear the compose field to stop typing indicator.

        Best-effort — failures don't block the bot.
        """
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

    async def send_text(self, phone: str, message: str, retries: int = 3) -> bool:
        """Send a text message via iMessage, chunking if needed.

        Retries up to `retries` times with exponential backoff.
        """
        chunks = self._chunk_message(message)
        for i, chunk in enumerate(chunks):
            sent = False
            for attempt in range(retries):
                script = self._build_send_text_script(phone, chunk)
                success, error = await self._run_applescript(script)
                if success:
                    log.info("Sent message to %s (chunk %d/%d)", phone, i + 1, len(chunks))
                    sent = True
                    break
                backoff = 2 ** attempt
                log.warning(
                    "Send failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, retries, backoff, error,
                )
                await asyncio.sleep(backoff)
            if not sent:
                log.error("Failed to send message to %s after %d retries", phone, retries)
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
            await asyncio.sleep(backoff)
        log.error("Failed to send image to %s after %d retries", phone, retries)
        return False

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
