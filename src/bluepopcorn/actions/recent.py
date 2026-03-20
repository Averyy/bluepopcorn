from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..types import LLMDecision
from ._base import ERROR_GENERIC

log = logging.getLogger(__name__)


async def handle_recent(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Fetch server state (available + requested) and let the LLM present it."""
    try:
        page = decision.page or 1
        data = await executor.seerr.get_server_state(page=page)

        available = data.get("available", [])
        requested = data.get("requested", [])

        if not available and not requested:
            executor._add_context(sender_phone, "[Server state: no available or requested items found]")
            return (await executor._llm_respond(sender_phone, fallback="Nothing on the server right now.", intent="recent"))[0]

        lines: list[str] = [f"[Server state (page {page}):"]

        if available:
            lines.append("Available on server:")
            for i, item in enumerate(available, 1):
                year = f" ({item['year']})" if item.get("year") else ""
                media_label = "TV" if item["media_type"] == "tv" else "Movie"
                overview = item.get("overview", "")
                lines.append(
                    f"{i}. {item['title']}{year} [{media_label}] tmdb:{item['tmdb_id']}"
                    f" - {overview} (Status: {item['status']})"
                )

        if requested:
            lines.append("Requested:")
            for i, item in enumerate(requested, 1):
                year = f" ({item['year']})" if item.get("year") else ""
                media_label = "TV" if item["media_type"] == "tv" else "Movie"
                overview = item.get("overview", "")
                lines.append(
                    f"{i}. {item['title']}{year} [{media_label}] tmdb:{item['tmdb_id']}"
                    f" - {overview} (Status: {item['status']})"
                )

        lines.append("]")
        context = "\n".join(lines)
        executor._add_context(sender_phone, context)

        # Track most recent item for pronoun resolution
        first = available[0] if available else (requested[0] if requested else None)
        if first:
            executor._last_topic[sender_phone] = {
                "title": first["title"],
                "tmdb_id": first["tmdb_id"],
                "media_type": first["media_type"],
            }

        return (await executor._llm_respond(sender_phone, fallback=context, intent="recent"))[0]
    except Exception as e:
        log.error("Server state fetch failed: %s", e)
        return ERROR_GENERIC
