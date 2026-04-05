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
from .llm import LLMAuthError, LLMClient
from .memory import UserMemory
from .monitor import MessageMonitor
from .prompts import COMPRESS_DAILY_PROMPT, COMPRESS_MONTHLY_PROMPT, COMPRESS_WEEKLY_PROMPT
from .utils import mask_phone, safe_data_path
from .schemas import COMPRESSION_SCHEMA, ROLLUP_SCHEMA
from .types import HistoryEntry

log = logging.getLogger(__name__)


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
        return safe_data_path(self._data_dir, "last_compressed", sender)

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

        # Filter out morning digest messages — they're bot-generated noise
        messages = [
            m for m in messages
            if not (m.role == "assistant" and m.content.startswith("Good morning."))
        ]
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

        prompt = COMPRESS_DAILY_PROMPT.format(
            prefs_text=prefs_text,
            tastes_text=tastes_text,
            recent_text=recent_text,
            conversation=conversation,
        )

        try:
            result = await self.llm.summarize(prompt, COMPRESSION_SCHEMA)
        except LLMAuthError as e:
            log.error("Daily compression auth failed for %s: %s", mask_phone(sender), e)
            raise
        except Exception as e:
            log.error("Daily compression LLM call failed for %s: %s", mask_phone(sender), e)
            return

        summary = result.get("summary", "").strip()
        if not summary:
            log.warning("Empty summary from compression for %s", mask_phone(sender))
            return

        # Determine the date from the first message (use configured timezone)
        tz = ZoneInfo(self.settings.timezone)
        first_ts = messages[0].timestamp
        date_str = datetime.datetime.fromtimestamp(first_ts, tz=tz).strftime("%Y-%m-%d")

        # Hold the memory lock for all writes — single load/write cycle
        async with self.memory.get_lock(sender):
            content = self.memory.load_or_create(sender)
            sections = self.memory.parse_sections(content)

            self.memory.append_summary_to(sections, date_str, summary, tier="Recent")
            log.info("Daily compression for %s on %s: %s", mask_phone(sender), date_str, summary[:100])

            # Update tastes (persistent, never compressed)
            genres = result.get("genres", [])
            avoid_genres = result.get("avoid_genres", [])
            movies = result.get("liked_movies", [])
            shows = result.get("liked_shows", [])
            avoid = result.get("avoid_titles", [])
            if genres or movies or shows or avoid_genres or avoid:
                added = self.memory.update_tastes_in(
                    sections, genres=genres, movies=movies, shows=shows,
                    avoid_genres=avoid_genres, avoid=avoid,
                )
                if added:
                    log.info("Updated tastes for %s (+%d items)", mask_phone(sender), added)

            # Auto-append suggested preferences
            suggested = result.get("suggested_preferences", [])
            for pref in suggested:
                pref = pref.strip()
                if pref:
                    tagged = f"{pref} (auto {date_str})"
                    if self.memory.add_preference_to(sections, tagged):
                        log.info("Auto-preference for %s: %s", mask_phone(sender), tagged)

            self.memory.save(sender, sections)

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
        prompt = COMPRESS_WEEKLY_PROMPT.format(text=text)

        try:
            result = await self.llm.summarize(prompt, ROLLUP_SCHEMA)
        except LLMAuthError as e:
            log.error("Weekly compression auth failed for %s: %s", mask_phone(sender), e)
            raise
        except Exception as e:
            log.error("Weekly compression failed for %s: %s", mask_phone(sender), e)
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
        log.info("Weekly compression for %s: %s", mask_phone(sender), summary[:100])

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
        prompt = COMPRESS_MONTHLY_PROMPT.format(text=text)

        try:
            result = await self.llm.summarize(prompt, ROLLUP_SCHEMA)
        except LLMAuthError as e:
            log.error("Monthly compression auth failed for %s: %s", mask_phone(sender), e)
            raise
        except Exception as e:
            log.error("Monthly compression failed for %s: %s", mask_phone(sender), e)
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
        log.info("Monthly compression for %s: %s", mask_phone(sender), summary[:100])

    async def run_compression(self, sender: str) -> None:
        """Run all compression tiers for a sender, catching up missed days."""
        tz = ZoneInfo(self.settings.timezone)
        today = datetime.datetime.now(tz).date()
        yesterday = today - datetime.timedelta(days=1)
        last_compressed = self._read_last_compressed(sender)

        if last_compressed and last_compressed >= yesterday:
            log.debug("Compression already done for %s through %s", mask_phone(sender), last_compressed)
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
                    log.info("Compressed %s for %s (%d messages)", current, mask_phone(sender), len(messages))
                else:
                    log.debug("No messages for %s on %s, skipping", mask_phone(sender), current)
            except LLMAuthError:
                return  # Auth failure — skip everything, don't retry with bad credentials
            except Exception as e:
                log.error("Daily compression failed for %s on %s: %s", mask_phone(sender), current, e)
                return  # Don't update last_compressed — next run will retry
            current += datetime.timedelta(days=1)

        self._write_last_compressed(sender, yesterday)

        # Roll up older entries
        try:
            await self.compress_weekly(sender)
        except LLMAuthError:
            return
        except Exception as e:
            log.error("Weekly compression failed for %s: %s", mask_phone(sender), e)

        try:
            await self.compress_monthly(sender)
        except LLMAuthError:
            return
        except Exception as e:
            log.error("Monthly compression failed for %s: %s", mask_phone(sender), e)
