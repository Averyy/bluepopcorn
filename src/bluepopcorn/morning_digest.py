from __future__ import annotations

import logging
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm import LLMClient
    from .memory import UserMemory

from .config import Settings
from .prompts import DIGEST_COMPOSE_PROMPT, DIGEST_FALLBACK, DIGEST_SYSTEM_PROMPT
from .schemas import DIGEST_SCHEMA
from .seerr import SeerrClient
from .types import MediaStatus
from .utils import mask_phone, safe_data_path

log = logging.getLogger(__name__)


class MorningDigest:
    def __init__(
        self,
        settings: Settings,
        seerr: SeerrClient,
        llm: LLMClient,
        memory: UserMemory,
    ) -> None:
        self.settings = settings
        self.seerr = seerr
        self.llm = llm
        self.memory = memory
        self._data_dir = settings.resolve_path(settings.data_dir)

    async def build(
        self,
        sender: str,
        last_digest: str | None = None,
        *,
        available: str | None = None,
        pending: str | None = None,
        trending: str | None = None,
    ) -> str | None:
        """Build the morning digest via LLM.

        Python fetches raw data (available, pending, trending),
        then Haiku composes the message using the user's memory.
        Returns None if Haiku decides there's nothing new to report.

        *available* and *pending* can be pre-fetched and passed in to
        avoid redundant Seerr API calls when building for multiple users.
        *trending* is always fetched per-user so that each user's
        suggested-ID exclusion list is applied.
        """
        suggested_ids = self._load_suggested_ids(sender)

        # Fetch any data not provided by the caller
        if available is None:
            available = await self.fetch_available()
        if pending is None:
            pending = await self.fetch_pending()
        if trending is None:
            trending = await self.fetch_trending(exclude_ids=set(suggested_ids) if suggested_ids else None)

        user_memory = self.memory.load(sender) or "(new user, no history)"

        prompt = Template(DIGEST_COMPOSE_PROMPT).safe_substitute(
            memory=user_memory,
            available=available or "(none)",
            pending=pending or "0",
            last_digest=last_digest or "(first digest)",
            trending=trending or "(nothing trending right now)",
        )

        try:
            result = await self.llm.summarize(
                prompt, DIGEST_SCHEMA,
                system_prompt=DIGEST_SYSTEM_PROMPT,
            )
        except Exception as e:
            log.error("Digest LLM call failed, sending fallback: %s", e)
            return DIGEST_FALLBACK

        if not result.get("send", False):
            log.info("LLM decided to skip digest for %s (nothing new)", mask_phone(sender))
            return None

        message = result.get("message", "").strip()
        if not message:
            log.warning("Empty message from digest LLM call, skipping")
            return None

        # Track the suggested tmdb_id for rotation
        suggested_id = result.get("suggested_tmdb_id")
        if isinstance(suggested_id, int):
            self._save_suggested_id(sender, suggested_id, existing=suggested_ids)

        return message

    # ── Data fetchers (raw values for the LLM prompt) ────────────

    async def fetch_available(self) -> str | None:
        """Fetch recently available titles as a comma-separated string."""
        try:
            added = await self.seerr.get_recently_added(take=3)
            if added:
                titles = [item["title"] for item in added if item.get("title")]
                if titles:
                    return ", ".join(titles)
        except Exception as e:
            log.warning("Failed to fetch available media for digest: %s", e)
        return None

    async def fetch_pending(self) -> str | None:
        """Fetch pending request count as a string."""
        try:
            pending = await self.seerr.get_pending()
            if pending:
                return str(len(pending))
        except Exception as e:
            log.warning("Failed to fetch pending requests for digest: %s", e)
        return None

    async def fetch_trending(self, exclude_ids: set[int] | None = None) -> str | None:
        """Fetch trending titles not in the library, formatted for the LLM."""
        try:
            trending = await self.seerr.discover_trending(
                take=20, exclude_ids=exclude_ids,
            )
            lines: list[str] = []
            for item in trending:
                if item.status in (MediaStatus.NOT_TRACKED, MediaStatus.UNKNOWN):
                    if item.rating and item.rating >= 7.0 and item.overview:
                        overview = item.overview[:120] + "..." if len(item.overview) > 120 else item.overview
                        year_str = f" ({item.year})" if item.year else ""
                        lines.append(
                            f"- [tmdb:{item.tmdb_id}] {item.title}{year_str} "
                            f"{item.media_type} — {item.rating}/10 — {overview}"
                        )
            return "\n".join(lines) if lines else None
        except Exception as e:
            log.warning("Failed to fetch trending for digest: %s", e)
        return None

    # ── Suggested ID tracking (rotation) ─────────────────────────

    def _suggested_ids_path(self, sender: str) -> Path:
        return safe_data_path(self._data_dir, "suggested_ids", sender)

    def _load_suggested_ids(self, sender: str) -> list[int]:
        """Load previously suggested tmdb_ids preserving insertion order."""
        try:
            text = self._suggested_ids_path(sender).read_text().strip()
        except FileNotFoundError:
            return []
        ids: list[int] = []
        seen: set[int] = set()
        for line in text.split("\n"):
            line = line.strip()
            if line:
                try:
                    val = int(line)
                except ValueError:
                    continue
                if val not in seen:
                    ids.append(val)
                    seen.add(val)
        return ids

    def _save_suggested_id(
        self, sender: str, tmdb_id: int,
        existing: list[int] | None = None,
    ) -> None:
        """Append a tmdb_id to the suggested history (max 100)."""
        path = self._suggested_ids_path(sender)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ids = list(existing) if existing is not None else self._load_suggested_ids(sender)
        if tmdb_id not in ids:
            ids.append(tmdb_id)
        if len(ids) > 100:
            ids = ids[-100:]
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text("\n".join(str(i) for i in ids) + "\n")
            tmp.chmod(0o600)
            tmp.rename(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
