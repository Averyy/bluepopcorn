from __future__ import annotations

import asyncio
import logging

from .config import Settings
from .seerr import SeerrClient

log = logging.getLogger(__name__)


class MorningDigest:
    def __init__(self, settings: Settings, seerr: SeerrClient) -> None:
        self.settings = settings
        self.seerr = seerr

    async def build(self) -> str:
        """Build the morning digest message."""
        media = await self._get_media_status()

        if not media:
            return "Good morning. Couldn't fetch any updates right now."

        return "Good morning. " + media

    async def _get_media_status(self) -> str | None:
        """Get media status from Seerr."""
        try:
            available_result, pending_result = await asyncio.gather(
                self._fetch_available(),
                self._fetch_pending(),
            )

            parts: list[str] = []
            if available_result:
                parts.append(available_result)
            if pending_result:
                parts.append(pending_result)

            return ". ".join(parts) + "." if parts else None

        except Exception as e:
            log.error("Media status fetch failed: %s", e)
            return None

    async def _fetch_available(self) -> str | None:
        """Fetch recently available media from Seerr."""
        try:
            added = await self.seerr.get_recently_added(take=3)
            if added:
                titles = [item["title"] for item in added if item.get("title")]
                if titles:
                    return f"Recently available: {', '.join(titles)}"
        except Exception as e:
            log.debug("Failed to fetch available media: %s", e)
        return None

    async def _fetch_pending(self) -> str | None:
        """Fetch pending requests from Seerr."""
        try:
            pending = await self.seerr.get_pending()
            if pending:
                return f"{len(pending)} request{'s' if len(pending) != 1 else ''} pending"
        except Exception as e:
            log.debug("Failed to fetch pending requests: %s", e)
        return None

