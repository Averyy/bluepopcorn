from __future__ import annotations

import logging
import time
from pathlib import Path

import aiosqlite

from .config import Settings
from .types import HistoryEntry

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_sender_ts
    ON history(sender, timestamp);

CREATE TABLE IF NOT EXISTS user_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    fact TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class BotDatabase:
    def __init__(self, settings: Settings) -> None:
        self.db_path = settings.resolve_path(settings.db_path)
        self.history_window = settings.history_window
        self.history_gap_hours = settings.history_gap_hours
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Initialize database and create tables."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("Bot database initialized at %s", self.db_path)

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not initialized — call init() first")
        return self._db

    async def get_last_rowid(self) -> int | None:
        """Get the last processed ROWID from chat.db."""
        async with self.db.execute(
            "SELECT value FROM state WHERE key = 'last_rowid'"
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0]) if row else None

    async def set_last_rowid(self, rowid: int) -> None:
        """Update the last processed ROWID."""
        await self.db.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES ('last_rowid', ?)",
            (str(rowid),),
        )
        await self.db.commit()

    async def add_history(self, sender: str, role: str, content: str) -> None:
        """Add a message to conversation history."""
        now = time.time()
        await self.db.execute(
            "INSERT INTO history (sender, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (sender, role, content, now),
        )
        await self.db.commit()

    async def get_history(self, sender: str) -> list[HistoryEntry]:
        """Get recent conversation history for a sender.

        Returns up to history_window entries, clearing if there's
        a gap longer than history_gap_hours.
        """
        now = time.time()
        gap_threshold = now - (self.history_gap_hours * 3600)

        # Get recent history
        async with self.db.execute(
            """SELECT role, content, timestamp FROM history
               WHERE sender = ? ORDER BY timestamp DESC LIMIT ?""",
            (sender, self.history_window),
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return []

        # Check for gap — if most recent message is too old, clear history
        most_recent_ts = rows[0][2]
        if most_recent_ts < gap_threshold:
            await self.clear_history(sender)
            return []

        # Return in chronological order
        entries = [
            HistoryEntry(role=row[0], content=row[1], timestamp=row[2])
            for row in reversed(rows)
        ]
        return entries

    async def clear_history(self, sender: str) -> None:
        """Clear conversation history for a sender."""
        await self.db.execute("DELETE FROM history WHERE sender = ?", (sender,))
        await self.db.commit()
        log.info("Cleared history for %s", sender)

    async def add_fact(self, sender: str, fact: str) -> None:
        """Store a user fact/preference."""
        now = time.time()
        await self.db.execute(
            "INSERT INTO user_facts (sender, fact, created_at) VALUES (?, ?, ?)",
            (sender, fact, now),
        )
        await self.db.commit()
        log.info("Stored fact for %s: %s", sender, fact[:80])

    async def get_facts(self, sender: str) -> list[str]:
        """Get all stored facts for a sender."""
        async with self.db.execute(
            "SELECT fact FROM user_facts WHERE sender = ? ORDER BY created_at",
            (sender,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def remove_fact(self, sender: str, keyword: str) -> bool:
        """Remove the first fact containing the keyword. Returns True if a fact was removed."""
        keyword_lower = keyword.lower()
        async with self.db.execute(
            "SELECT id, fact FROM user_facts WHERE sender = ? ORDER BY created_at",
            (sender,),
        ) as cursor:
            rows = await cursor.fetchall()

        for row_id, fact in rows:
            if keyword_lower in fact.lower():
                await self.db.execute("DELETE FROM user_facts WHERE id = ?", (row_id,))
                await self.db.commit()
                log.info("Removed fact for %s: %s", sender, fact[:80])
                return True
        return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
