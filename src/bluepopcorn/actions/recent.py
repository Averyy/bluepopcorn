from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..types import LLMDecision
from ._base import ERROR_GENERIC, resolve_request_title

log = logging.getLogger(__name__)


async def handle_recent(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Check recently added media and pending requests."""
    try:
        lines: list[str] = []
        # Fetch recently added and pending in parallel
        added, pending = await asyncio.gather(
            executor.seerr.get_recently_added(take=5),
            executor.seerr.get_pending(),
        )
        if added:
            movies = [r["title"] for r in added if r["mediaType"] == "movie"]
            shows = [r["title"] for r in added if r["mediaType"] == "tv"]
            if movies:
                lines.append("Recently added movies: " + ", ".join(movies))
            if shows:
                lines.append("Recently added shows: " + ", ".join(shows))
        if pending:
            resolved = await asyncio.gather(
                *[resolve_request_title(req, executor.seerr) for req in pending[:5]]
            )
            pending_titles = [t for t in resolved if t != "Unknown"]
            if pending_titles:
                lines.append("Pending requests: " + ", ".join(pending_titles))

        if not lines:
            return "Nothing new right now."

        data = "\n".join(lines)
        await executor.db.add_history(sender_phone, "context", f"[{data}]")
        return await executor._llm_respond(sender_phone, fallback=data)
    except Exception as e:
        log.error("Recent media fetch failed: %s", e)
        return ERROR_GENERIC
