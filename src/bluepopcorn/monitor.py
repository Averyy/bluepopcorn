from __future__ import annotations

import logging
import re
import time
from datetime import date as Date
from datetime import datetime, time as Time, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite

from .config import Settings
from .types import HistoryEntry, IncomingMessage

log = logging.getLogger(__name__)

# Core Foundation epoch: 2001-01-01 00:00:00 UTC = Unix timestamp 978307200
CF_EPOCH_UNIX = 978307200


def cf_to_unix(cf_nanos: int) -> float:
    """Convert Core Foundation nanosecond timestamp to Unix timestamp."""
    return cf_nanos / 1_000_000_000 + CF_EPOCH_UNIX


def unix_to_cf(unix_ts: float) -> int:
    """Convert Unix timestamp to Core Foundation nanosecond timestamp."""
    return int((unix_ts - CF_EPOCH_UNIX) * 1_000_000_000)


def _dedup_chunked(entries: list[HistoryEntry]) -> list[HistoryEntry]:
    """Merge consecutive outgoing messages within 2 seconds (chunked sends)."""
    if not entries:
        return entries
    deduped: list[HistoryEntry] = [entries[0]]
    for entry in entries[1:]:
        prev = deduped[-1]
        if (
            entry.role == "assistant"
            and prev.role == "assistant"
            and entry.timestamp - prev.timestamp < 2.0
        ):
            deduped[-1] = HistoryEntry(
                role="assistant",
                content=prev.content + "\n" + entry.content,
                timestamp=prev.timestamp,
            )
        else:
            deduped.append(entry)
    return deduped


def _rows_to_entries(rows: list[tuple]) -> list[HistoryEntry]:
    """Convert raw chat.db rows to HistoryEntry list with noise filtering."""
    entries: list[HistoryEntry] = []
    for _rowid, text, attr_body, is_from_me, date_val in rows:
        if not text and attr_body:
            text = parse_attributed_body(attr_body)
        if not text or not text.strip():
            continue
        timestamp = cf_to_unix(date_val) if date_val else 0.0
        role = "assistant" if is_from_me else "user"
        entries.append(HistoryEntry(role=role, content=text.strip(), timestamp=timestamp))
    return entries


def _read_typedstream_length(data: bytes, offset: int) -> tuple[int, int]:
    """Read a variable-length integer from typedstream encoding.

    Returns (value, new_offset). Encoding (little-endian):
    - byte < 0x80: literal value (1 byte)
    - 0x80: next 1 byte as unsigned value
    - 0x81: next 2 bytes little-endian
    - 0x82: next 4 bytes little-endian
    """
    if offset >= len(data):
        return 0, offset
    first = data[offset]
    if first < 0x80:
        return first, offset + 1
    if first == 0x80 and offset + 1 < len(data):
        return data[offset + 1], offset + 2
    if first == 0x81 and offset + 2 < len(data):
        return data[offset + 1] | (data[offset + 2] << 8), offset + 3
    if first == 0x82 and offset + 4 < len(data):
        val = int.from_bytes(data[offset + 1 : offset + 5], "little")
        return val, offset + 5
    return 0, offset + 1


def parse_attributed_body(blob: bytes) -> str | None:
    """Extract plain text from chat.db attributedBody column.

    attributedBody stores a typedstream-encoded NSAttributedString.
    The string data follows: NSString [header] '+' [length] [text bytes]
    """
    if not blob:
        return None
    try:
        # Find NSString marker then the '+' (0x2b) that precedes string data
        ns_idx = blob.find(b"NSString")
        if ns_idx >= 0:
            # Find '+' marker after NSString (within a small window)
            search_start = ns_idx + 8  # len("NSString")
            plus_idx = blob.find(b"+", search_start, search_start + 10)
            if plus_idx >= 0:
                str_len, text_start = _read_typedstream_length(
                    blob, plus_idx + 1
                )
                if str_len > 0 and text_start + str_len <= len(blob):
                    text = blob[text_start : text_start + str_len].decode(
                        "utf-8", errors="ignore"
                    ).strip()
                    # Reject attachment metadata and image-only messages
                    if text and not any(c < "\x09" for c in text[:10]):
                        # Strip object replacement chars (inline attachments)
                        cleaned = text.replace("\ufffc", "").strip()
                        if cleaned:
                            return cleaned
    except Exception as e:
        log.debug("attributedBody parse failed: %s", e)

    return None


class MessageMonitor:
    def __init__(self, settings: Settings) -> None:
        self.chat_db_path = str(settings.resolve_path(settings.chat_db_path))
        self.allowed_senders = set(settings.allowed_senders)
        self._db: aiosqlite.Connection | None = None

    async def _get_db(self) -> aiosqlite.Connection:
        """Get or open a persistent read-only connection to chat.db."""
        if self._db is not None:
            try:
                # Verify the connection is still usable
                await self._db.execute("SELECT 1")
            except Exception:
                try:
                    await self._db.close()
                except Exception:
                    pass
                self._db = None
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

    async def get_recent_messages(
        self,
        sender: str,
        limit: int = 20,
        since_hours: float = 24.0,
    ) -> list[HistoryEntry]:
        """Query chat.db for bidirectional messages (incoming + outgoing).

        Uses chat_message_join to get both is_from_me=0 and is_from_me=1.
        Filters noise and deduplicates chunked outgoing messages.
        """
        db = await self._get_db()
        cutoff_cf = unix_to_cf(time.time() - since_hours * 3600)

        query = """
            SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE c.chat_identifier = ?
              AND m.item_type = 0
              AND m.date > ?
            ORDER BY m.date ASC
            LIMIT ?
        """
        # Fetch extra rows to account for noise filtering + dedup
        async with db.execute(query, (sender, cutoff_cf, limit * 3)) as cursor:
            rows = await cursor.fetchall()

        entries = _dedup_chunked(_rows_to_entries(rows))[-limit:]
        log.debug(
            "get_recent_messages(%s): %d raw rows, %d entries",
            sender, len(rows), len(entries),
        )
        return entries

    async def get_messages_for_date(
        self,
        sender: str,
        date: Date,
        tz: ZoneInfo | None = None,
    ) -> list[HistoryEntry]:
        """Get all messages on a specific date (for compression).

        ``tz`` should be the configured timezone so day boundaries are correct.
        Falls back to UTC if not provided.
        """
        db = await self._get_db()
        tzinfo = tz or timezone.utc
        start_dt = datetime.combine(date, Time.min, tzinfo=tzinfo)
        end_dt = datetime.combine(date + timedelta(days=1), Time.min, tzinfo=tzinfo)
        start_cf = unix_to_cf(start_dt.timestamp())
        end_cf = unix_to_cf(end_dt.timestamp())

        query = """
            SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date
            FROM message m
            JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE c.chat_identifier = ?
              AND m.item_type = 0
              AND m.date >= ?
              AND m.date < ?
            ORDER BY m.date ASC
            LIMIT 500
        """
        async with db.execute(query, (sender, start_cf, end_cf)) as cursor:
            rows = await cursor.fetchall()

        return _dedup_chunked(_rows_to_entries(rows))
