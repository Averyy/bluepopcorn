"""Tiered compression engine for per-user markdown memory.

Runs daily after the morning digest. Summarizes yesterday's chat.db
messages into the memory file, then rolls up older summaries into
weekly and monthly tiers.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .llm import LLMClient
from .memory import UserMemory
from .monitor import MessageMonitor
from .types import HistoryEntry

log = logging.getLogger(__name__)

COMPRESSION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "1-3 sentence summary. Always include specific title names and outcomes.",
        },
        "suggested_preferences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genuine repeated patterns to add as preferences (empty if none)",
        },
        "genres": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genres the user showed interest in (e.g. 'horror', 'sci-fi', 'Korean drama')",
        },
        "avoid_genres": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genres the user said they dislike or want to avoid, with reason in brackets if known (e.g. 'reality TV [finds it trashy]', 'romance')",
        },
        "liked_movies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Movies the user requested or expressed interest in, with year (e.g. 'Sinners (2025)')",
        },
        "liked_shows": {
            "type": "array",
            "items": {"type": "string"},
            "description": "TV shows the user requested or expressed interest in, with year (e.g. 'Severance (2022)')",
        },
        "avoid_titles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Titles the user rejected, disliked, or said to avoid, with year and reason in brackets if known (e.g. 'The Monkey (2025) [too campy]', 'Love Is Blind (2020) [hates reality TV]')",
        },
    },
    "required": ["summary", "suggested_preferences", "genres", "avoid_genres", "liked_movies", "liked_shows", "avoid_titles"],
    "additionalProperties": False,
}


class Compressor:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        monitor: MessageMonitor,
        memory: UserMemory,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.monitor = monitor
        self.memory = memory
        self._data_dir = settings.resolve_path(settings.data_dir)

    def _last_compressed_path(self, sender: str) -> Path:
        """Per-sender last-compressed date file."""
        safe = sender.lstrip("+").replace("/", "_")
        path = self._data_dir / f"last_compressed_{safe}"
        if not path.resolve().is_relative_to(self._data_dir.resolve()):
            raise ValueError(f"Invalid sender for compression path: {sender!r}")
        return path

    def _read_last_compressed(self, sender: str) -> datetime.date | None:
        """Read the last compression date for a sender."""
        path = self._last_compressed_path(sender)
        try:
            return datetime.date.fromisoformat(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            return None

    def _write_last_compressed(self, sender: str, date: datetime.date) -> None:
        """Write the last compression date for a sender."""
        path = self._last_compressed_path(sender)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(date.isoformat())
            tmp.rename(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    async def compress_daily(
        self, sender: str, messages: list[HistoryEntry]
    ) -> None:
        """Summarize a day's messages and append to # Recent."""
        if not messages:
            return

        # Build conversation text for the LLM
        lines: list[str] = []
        for m in messages:
            role = "User" if m.role == "user" else "Bot"
            lines.append(f"{role}: {m.content}")
        conversation = "\n".join(lines)

        # Load file once and extract all needed sections
        content = self.memory.load(sender)
        sections = self.memory.parse_sections(content) if content else {}

        existing_prefs = [
            line[2:] for line in sections.get("Preferences", [])
            if line.startswith("- ")
        ]
        prefs_text = "\n".join(f"- {p}" for p in existing_prefs) if existing_prefs else "(none)"

        existing_tastes = self.memory.parse_tastes(sections)
        likes = [f"{k}: {', '.join(v)}" for k, v in existing_tastes.items() if not k.startswith("avoid")]
        dislikes = [f"{k.removeprefix('avoid ')}: {', '.join(v)}" for k, v in existing_tastes.items() if k.startswith("avoid")]
        tastes_text = "Likes: " + "; ".join(likes) if likes else "(none)"
        if dislikes:
            tastes_text += "\nDislikes: " + "; ".join(dislikes)

        recent = sections.get("Recent", [])
        recent_text = "\n".join(ln for ln in recent if ln.startswith("- "))[:500] or "(none)"

        prompt = (
            "Summarize this day's iMessage conversation between a user and a media bot.\n\n"
            "Rules:\n"
            "- Always mention specific title names and outcomes (e.g. 'Requested Severance S3, added successfully')\n"
            "- Keep it to 1-3 sentences. Skip conversation mechanics ('user asked', 'bot responded').\n"
            "- Extract genres the user showed interest in for the genres array.\n"
            "- Extract genres the user said they dislike or want to avoid for avoid_genres. Include reason in [brackets] if stated.\n"
            "- Extract specific movies/shows the user requested or asked about for liked_movies/liked_shows. Include year.\n"
            "- Extract titles the user rejected or disliked for avoid_titles. Include reason in [brackets] if stated.\n"
            "- Only suggest preferences for genuine repeated patterns, not one-off requests.\n\n"
            f"Already known preferences (do NOT re-suggest):\n{prefs_text}\n\n"
            f"Already known tastes (do NOT re-add):\n{tastes_text}\n\n"
            f"Recent summaries (for context, don't repeat):\n{recent_text}\n\n"
            f"Conversation:\n{conversation}"
        )

        try:
            result = await self.llm.summarize(prompt, COMPRESSION_SCHEMA)
        except Exception as e:
            log.error("Daily compression LLM call failed for %s: %s", sender, e)
            return

        summary = result.get("summary", "").strip()
        if not summary:
            log.warning("Empty summary from compression for %s", sender)
            return

        # Determine the date from the first message (use configured timezone)
        tz = ZoneInfo(self.settings.timezone)
        first_ts = messages[0].timestamp
        date_str = datetime.datetime.fromtimestamp(first_ts, tz=tz).strftime("%Y-%m-%d")

        # Hold the memory lock for all writes to prevent race with user remember/forget
        async with self.memory.get_lock(sender):
            self.memory.append_summary(sender, date_str, summary, tier="Recent")
            log.info("Daily compression for %s on %s: %s", sender, date_str, summary[:100])

            # Update tastes (persistent, never compressed)
            genres = result.get("genres", [])
            avoid_genres = result.get("avoid_genres", [])
            movies = result.get("liked_movies", [])
            shows = result.get("liked_shows", [])
            avoid = result.get("avoid_titles", [])
            if genres or movies or shows or avoid_genres or avoid:
                self.memory.update_tastes(
                    sender, genres=genres, movies=movies, shows=shows,
                    avoid_genres=avoid_genres, avoid=avoid,
                )

            # Auto-append suggested preferences
            suggested = result.get("suggested_preferences", [])
            for pref in suggested:
                pref = pref.strip()
                if pref:
                    tagged = f"{pref} (auto {date_str})"
                    self.memory.add_preference(sender, tagged)
                    log.info("Auto-preference for %s: %s", sender, tagged)

    async def compress_weekly(self, sender: str) -> None:
        """Roll up daily entries older than 7 days into # Weekly."""
        recent_lines = self.memory.get_section(sender, "Recent")
        if not recent_lines:
            return

        tz = ZoneInfo(self.settings.timezone)
        today = datetime.datetime.now(tz).date()
        cutoff = today - datetime.timedelta(days=7)

        old_entries: list[str] = []
        keep_lines: list[str] = []
        for line in recent_lines:
            if not line.startswith("- "):
                continue
            m = re.match(r"^- (\d{4}-\d{2}-\d{2}):", line)
            if m:
                try:
                    entry_date = datetime.date.fromisoformat(m.group(1))
                    if entry_date < cutoff:
                        old_entries.append(line[2:])  # Strip "- " prefix
                    else:
                        keep_lines.append(line)
                    continue
                except ValueError:
                    pass
            keep_lines.append(line)

        if not old_entries:
            return

        # Summarize old entries into a weekly summary
        text = "\n".join(old_entries)
        prompt = (
            "Combine these daily conversation summaries into one weekly summary. "
            "1-2 sentences. Preserve specific title names. "
            "Focus on patterns and key events.\n\n"
            f"Daily summaries:\n{text}"
        )

        try:
            result = await self.llm.summarize(prompt, {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            })
        except Exception as e:
            log.error("Weekly compression failed for %s: %s", sender, e)
            return

        summary = result.get("summary", "").strip()
        if not summary:
            return

        # Calculate the week label from the oldest entry
        # Extract date from oldest entry for week label
        wm = re.search(r"(\d{4}-\d{2}-\d{2})", old_entries[0]) if old_entries else None
        week_label = f"Week of {wm.group(1)}" if wm else f"Week of {cutoff}"

        async with self.memory.get_lock(sender):
            self.memory.append_summary(sender, week_label, summary, tier="Weekly")
            self.memory.replace_section(sender, "Recent", keep_lines)
        log.info("Weekly compression for %s: %s", sender, summary[:100])

    async def compress_monthly(self, sender: str) -> None:
        """Roll up weekly entries older than 4 weeks into # History."""
        weekly_lines = self.memory.get_section(sender, "Weekly")
        if not weekly_lines:
            return

        tz = ZoneInfo(self.settings.timezone)
        today = datetime.datetime.now(tz).date()
        cutoff = today - datetime.timedelta(weeks=4)

        old_entries: list[str] = []
        keep_lines: list[str] = []
        for line in weekly_lines:
            if not line.startswith("- "):
                continue
            m = re.match(r"^- Week of (\d{4}-\d{2}-\d{2}):", line)
            if m:
                try:
                    entry_date = datetime.date.fromisoformat(m.group(1))
                    if entry_date < cutoff:
                        old_entries.append(line[2:])
                    else:
                        keep_lines.append(line)
                    continue
                except ValueError:
                    pass
            keep_lines.append(line)

        if not old_entries:
            return

        text = "\n".join(old_entries)
        prompt = (
            "Combine these weekly conversation summaries into one monthly summary. "
            "1-2 sentences. Preserve specific title names where notable. "
            "Focus on big-picture patterns and key events.\n\n"
            f"Weekly summaries:\n{text}"
        )

        try:
            result = await self.llm.summarize(prompt, {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            })
        except Exception as e:
            log.error("Monthly compression failed for %s: %s", sender, e)
            return

        summary = result.get("summary", "").strip()
        if not summary:
            return

        # Use month label from oldest entry
        m = re.search(r"(\d{4}-\d{2}-\d{2})", old_entries[0]) if old_entries else None
        if m:
            try:
                month_label = datetime.date.fromisoformat(m.group(1)).strftime("%b %Y")
            except ValueError:
                month_label = today.strftime("%b %Y")
        else:
            month_label = today.strftime("%b %Y")

        async with self.memory.get_lock(sender):
            self.memory.append_summary(sender, month_label, summary, tier="History")
            self.memory.replace_section(sender, "Weekly", keep_lines)
            self.memory.truncate_if_needed(sender)
        log.info("Monthly compression for %s: %s", sender, summary[:100])

    async def run_compression(self, sender: str) -> None:
        """Run all compression tiers for a sender, catching up missed days."""
        tz = ZoneInfo(self.settings.timezone)
        today = datetime.datetime.now(tz).date()
        yesterday = today - datetime.timedelta(days=1)
        last_compressed = self._read_last_compressed(sender)

        if last_compressed and last_compressed >= yesterday:
            log.debug("Compression already done for %s through %s", sender, last_compressed)
            return

        # Determine which days need compression
        start_date = (last_compressed + datetime.timedelta(days=1)) if last_compressed else yesterday

        # Process each missed day sequentially to avoid stale-read races on the memory file
        current = start_date
        while current <= yesterday:
            try:
                messages = await self.monitor.get_messages_for_date(sender, current, tz=tz)
                if messages:
                    await self.compress_daily(sender, messages)
                    log.info("Compressed %s for %s (%d messages)", current, sender, len(messages))
                else:
                    log.debug("No messages for %s on %s, skipping", sender, current)
            except Exception as e:
                log.error("Daily compression failed for %s on %s: %s", sender, current, e)
                return  # Don't update last_compressed — next run will retry
            current += datetime.timedelta(days=1)

        self._write_last_compressed(sender, yesterday)

        # Roll up older entries
        try:
            await self.compress_weekly(sender)
        except Exception as e:
            log.error("Weekly compression failed for %s: %s", sender, e)

        try:
            await self.compress_monthly(sender)
        except Exception as e:
            log.error("Monthly compression failed for %s: %s", sender, e)
