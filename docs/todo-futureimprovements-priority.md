# Future Improvements (Do Before Markdown Memory)

These are targeted `sender.py` fixes that don't touch any files the markdown memory refactor will overhaul. Do these first.

## Temp-File Trick for AppleScript Escaping (from imessage_tools)

Write the message to a temp file, then use `read (POSIX file "...") as «class utf8»` in AppleScript instead of string interpolation. Message content never enters AppleScript string literals, eliminating ALL escaping edge cases — emoji, quotes, backslashes, newlines, Unicode. Strictly superior to our `_escape_applescript()` string replacements.

**Approach:** Replace `_build_send_text_script()` in `sender.py`. Use `tempfile.mkstemp()` for unique paths (the original imessage_tools code has a race condition with a hardcoded filename). Cleanup via `try/finally`.

```python
async def _send_text_once(self, phone: str, message: str) -> tuple[bool, str]:
    fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="bluepopcorn_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(message)
        safe_phone = self._sanitize_phone(phone)
        script = f'''
tell application "Messages"
    set targetAccount to first account whose service type = iMessage
    set targetParticipant to participant "{safe_phone}" of targetAccount
    send (read (POSIX file "{tmp_path}") as «class utf8») to targetParticipant
end tell
'''
        return await self._run_applescript(script)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
```

This replaces `_build_send_text_script()` + `_escape_applescript()`. The `_sanitize_phone()` check stays (phone is still interpolated). Each concurrent send gets a unique temp path — no race conditions. Low effort, high reliability.

## Error Dialog Clearing (from CamHenlin/imessageclient)

AppleScript send failures can leave modal error dialogs in Messages.app ("There was an error sending the previous message" with Ignore/Open Messages/Resend buttons) that block ALL future sends silently. We retry with backoff but never dismiss the dialog, so the bot goes deaf after a single failure until Messages.app is manually interacted with.

**Approach:** Add a `_dismiss_error_dialogs()` method to `sender.py`, called before each send retry. No major project does this well — CamHenlin just presses Enter blindly, BlueBubbles clicks `button 1` of extra windows.

```applescript
tell application "System Events"
    tell process "Messages"
        -- Escape dismisses sheets/popovers without triggering actions
        try
            key code 53
            delay 0.2
        end try
        -- Dismiss extra windows (error dialogs appear as separate windows)
        try
            set winCount to count windows
            repeat while winCount > 1
                tell window 1
                    try
                        click button "Ignore"
                    on error
                        try
                            click button "OK"
                        on error
                            try
                                click button 1
                            end try
                        end try
                    end try
                end tell
                delay 0.3
                set winCount to count windows
            end repeat
        end try
    end tell
end tell
```

Wire into `send_text()` retry loop: call `_dismiss_error_dialogs()` after each failed attempt, before the backoff sleep. Wrap in try — this is best-effort cleanup, never block on it. Requires Accessibility (already granted).
