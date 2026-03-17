from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import aiosqlite

from .config import Settings
from .types import IncomingMessage

log = logging.getLogger(__name__)

# Core Foundation epoch: 2001-01-01 00:00:00 UTC
CF_EPOCH = datetime(2001, 1, 1)


def cf_to_unix(cf_nanos: int) -> float:
    """Convert Core Foundation nanosecond timestamp to Unix timestamp."""
    seconds = cf_nanos / 1_000_000_000
    dt = CF_EPOCH + timedelta(seconds=seconds)
    return dt.timestamp()


def parse_attributed_body(blob: bytes) -> str | None:
    """Extract plain text from chat.db attributedBody column.

    attributedBody stores a typedstream-encoded NSAttributedString.
    We extract the text content using byte pattern matching.
    """
    if not blob:
        return None
    try:
        # Method 1: Split on NSString marker, skip header bytes
        parts = blob.split(b"NSString")
        if len(parts) > 1:
            data = parts[1]
            # Skip type/length header (typically 5 bytes)
            data = data[5:]
            # Find end of text: first low control byte (< 0x09)
            # High bytes (>= 0x80) are part of UTF-8 multibyte sequences
            end = len(data)
            for i, byte in enumerate(data):
                if byte < 0x09:
                    end = i
                    break
            # Decode as UTF-8, which drops orphan continuation bytes
            text = data[:end].decode("utf-8", errors="ignore")
            # Strip leading/trailing control characters and whitespace
            text = text.lstrip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c"
                               "\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18"
                               "\x19\x1a\x1b\x1c\x1d\x1e\x1f").strip()
            if text:
                return text
    except Exception as e:
        log.debug("attributedBody NSString parse failed: %s", e)

    try:
        # Method 2: Regex for +text pattern
        match = re.search(rb"\x01\+(.+?)(?:\x00|\x03|\x06)", blob)
        if match:
            return match.group(1).decode("utf-8", errors="ignore").strip()
    except Exception as e:
        log.debug("attributedBody regex parse failed: %s", e)

    return None


class MessageMonitor:
    def __init__(self, settings: Settings) -> None:
        self.chat_db_path = str(settings.resolve_path(settings.chat_db_path))
        self.allowed_senders = set(settings.allowed_senders)
        self._db: aiosqlite.Connection | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        """Get or open a persistent read-only connection to chat.db."""
        if self._db is None:
            uri = f"file:{self.chat_db_path}?mode=ro"
            self._db = await aiosqlite.connect(uri, uri=True)
        return self._db

    async def get_max_rowid(self) -> int:
        """Get the current maximum ROWID in chat.db message table."""
        db = await self._get_db()
        async with db.execute("SELECT MAX(ROWID) FROM message") as cursor:
            row = await cursor.fetchone()
            return row[0] or 0

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def get_new_messages(self, after_rowid: int) -> list[IncomingMessage]:
        """Poll chat.db for new incoming messages after the given ROWID.

        Filters:
        - is_from_me = 0 (incoming only)
        - item_type = 0 (regular messages, not reactions/edits)
        - Joins handle table for sender phone/email
        """
        db = await self._get_db()
        query = """
            SELECT
                m.ROWID,
                h.id AS sender,
                m.text,
                m.attributedBody,
                m.date
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.ROWID > ?
              AND m.is_from_me = 0
              AND m.item_type = 0
            ORDER BY m.ROWID ASC
        """
        async with db.execute(query, (after_rowid,)) as cursor:
            rows = await cursor.fetchall()

        messages: list[IncomingMessage] = []
        for row in rows:
            rowid, sender, text, attr_body, date_val = row

            # Try text field first, fall back to attributedBody
            if not text and attr_body:
                text = parse_attributed_body(attr_body)

            if not text or not text.strip():
                continue

            # Filter to allowed senders
            if sender not in self.allowed_senders:
                log.debug("Ignoring message from non-allowed sender: %s", sender)
                continue

            timestamp = cf_to_unix(date_val) if date_val else 0.0

            messages.append(
                IncomingMessage(
                    rowid=rowid,
                    sender=sender,
                    text=text.strip(),
                    timestamp=timestamp,
                )
            )

        if messages:
            log.info("Found %d new messages", len(messages))
        return messages
