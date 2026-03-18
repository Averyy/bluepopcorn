# Telegram Support

Add Telegram as a second messaging platform alongside iMessage. The goal is a clean `MessagePlatform` abstraction so both adapters share the same bot logic — same two-call LLM pattern, same actions, same conversation history.

## Overview

- **Platform**: Telegram Bot API via `python-telegram-bot` (async, v20+)
- **Deploy**: Runs anywhere (Linux, VPS) — no Mac required for Telegram
- **Auth**: Bot token from `@BotFather`, stored in `.env`
- **Receive**: Long polling (no public webhook URL needed) or webhook mode
- **Allowed senders**: Telegram user IDs in `.env` (same concept as `ALLOWED_SENDERS`)

## Architecture

### New: `src/bluepopcorn/platforms/base.py`

Abstract interface that both adapters implement:

```python
from abc import ABC, abstractmethod
from bluepopcorn.types import IncomingMessage

class MessagePlatform(ABC):
    @abstractmethod
    async def get_new_messages(self) -> list[IncomingMessage]: ...

    @abstractmethod
    async def send_text(self, recipient: str, text: str) -> None: ...

    @abstractmethod
    async def send_image(self, recipient: str, image_path: str) -> None: ...

    @abstractmethod
    async def set_typing(self, recipient: str, typing: bool) -> None: ...
```

### Refactor: `src/bluepopcorn/platforms/imessage.py`

Move existing `monitor.py` + `sender.py` behind this interface. The current `MessageMonitor` and `MessageSender` become implementation details of `iMessagePlatform`. `__main__.py` talks to the platform interface, not directly to monitor/sender.

### New: `src/bluepopcorn/platforms/telegram.py`

```python
class TelegramPlatform(MessagePlatform):
    """Telegram adapter using python-telegram-bot."""
```

Key points:
- Incoming messages queued via `python-telegram-bot` update handler → asyncio queue → `get_new_messages()` drains it
- `send_text()` uses `bot.send_message(chat_id=recipient, text=text)`
- `send_image()` uses `bot.send_photo(chat_id=recipient, photo=open(path, 'rb'))`
- `set_typing()` uses `bot.send_chat_action(chat_id=recipient, action=ChatAction.TYPING)`
- `recipient` is the Telegram `chat_id` (integer as string, e.g. `"123456789"`)
- Long polling via `Application.run_polling()` in a background thread, bridged to asyncio via queue

### Modify: `src/bluepopcorn/types.py`

Add `platform: str` field to `IncomingMessage` so logs and history are scoped per platform:

```python
@dataclass
class IncomingMessage:
    rowid: int          # or update_id for Telegram
    sender: str         # phone for iMessage, chat_id for Telegram
    text: str
    timestamp: float
    platform: str = "imessage"   # "imessage" | "telegram"
```

Conversation history in `db.py` already keys on `sender` — add `platform` to the key so the same user on different platforms has separate history.

### Modify: `src/bluepopcorn/__main__.py`

The daemon loop currently is iMessage-specific. Refactor to:

```python
platforms: list[MessagePlatform] = []
if config.imessage_enabled:
    platforms.append(iMessagePlatform(...))
if config.telegram_token:
    platforms.append(TelegramPlatform(...))

# Poll all platforms concurrently
await asyncio.gather(*[run_platform(p) for p in platforms])
```

Each platform runs its own polling loop, feeding into the same `_process_message()` handler.

### Modify: `src/bluepopcorn/config.py`

Add Telegram config:

```toml
# config.toml
[telegram]
enabled = false
```

```bash
# .env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_SENDERS=123456789,987654321   # Telegram user IDs
```

## Sender Identity

iMessage uses phone numbers. Telegram uses integer `chat_id`. The platform abstraction uses `sender: str` throughout — just stringify the Telegram chat_id. Conversation history, user facts, and locks all key on `(platform, sender)` combined.

## Typing Indicator

Telegram has a native `sendChatAction` with `typing` action — call it at the start of processing, no need to clear it (auto-expires after ~5s). Simpler than iMessage's AppleScript hack. Call it once before the first LLM call; if processing takes >4s, re-send it.

## Images

`send_image()` on Telegram sends the file directly (no sandbox restriction like Messages.app). Poster collages from `posters.py` work as-is — just pass the same path.

## Markdown

Telegram supports `parse_mode=MarkdownV2`. The personality prompt says "plain text only" (for iMessage). We can override this per-platform: Telegram adapter can enable markdown, but the LLM still sends plain text by default. Leave this for later — start with plain text on both.

## Allowed Senders

Same concept as iMessage `ALLOWED_SENDERS`: a set of Telegram user IDs that the bot responds to. Any message from an unknown ID is silently ignored. Load from `TELEGRAM_ALLOWED_SENDERS` env var (comma-separated integers).

## Implementation Order

1. `platforms/base.py` — `MessagePlatform` ABC
2. `platforms/imessage.py` — wrap existing monitor + sender (no behavior changes)
3. `types.py` — add `platform` field to `IncomingMessage`
4. `db.py` — scope conversation history by `(platform, sender)`
5. `__main__.py` — multi-platform loop
6. `platforms/telegram.py` — Telegram adapter
7. `config.py` — add telegram config + env vars
8. Test: send a message on Telegram, verify same bot logic responds

## Dependencies

```toml
# pyproject.toml
dependencies = [
    ...
    "python-telegram-bot>=21.0",
]
```

## Verification

1. `TELEGRAM_BOT_TOKEN` set, bot started → `/start` on Telegram → bot responds
2. Unknown Telegram user → silently ignored
3. Allowed user sends message → same LLM flow, same actions as iMessage
4. Poster image → `send_photo` works, image shows in chat
5. Typing indicator → appears during processing, clears automatically
6. iMessage and Telegram both active → each has independent conversation history
7. Test on Linux (no macOS) with `imessage_enabled = false` → Telegram-only works

## Notes

- `python-telegram-bot` v20+ is fully async — fits our asyncio model cleanly
- Long polling is fine for personal use (no need for webhook infra)
- Telegram `chat_id` == `user_id` for private chats (DMs to the bot)
- BotFather setup: `/newbot` → get token → optionally set description + avatar
- The bot only responds to DMs (not group chats) unless explicitly extended later
