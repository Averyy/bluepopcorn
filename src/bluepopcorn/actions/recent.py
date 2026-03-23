from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..prompts import (
    CONTEXT_RECENT_AVAILABLE,
    CONTEXT_RECENT_EMPTY,
    CONTEXT_RECENT_FOOTER,
    CONTEXT_RECENT_HEADER,
    CONTEXT_RECENT_REQUESTED,
    ERROR_GENERIC,
)
from ..types import LLMDecision, status_label_for
from ._base import format_result_line

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
            executor._add_context(sender_phone, CONTEXT_RECENT_EMPTY)
            return (await executor._llm_respond(sender_phone, scenario="recent_empty"))[0]

        lines: list[str] = [CONTEXT_RECENT_HEADER.format(page=page)]

        if available:
            lines.append(CONTEXT_RECENT_AVAILABLE)
            for i, item in enumerate(available, 1):
                lines.append(format_result_line(
                    i, item["title"], item.get("year"), item["media_type"],
                    item["tmdb_id"], item.get("overview", ""),
                    status_label_for(item["status"]),
                ))

        if requested:
            lines.append(CONTEXT_RECENT_REQUESTED)
            for i, item in enumerate(requested, 1):
                lines.append(format_result_line(
                    i, item["title"], item.get("year"), item["media_type"],
                    item["tmdb_id"], item.get("overview", ""),
                    status_label_for(item["status"]),
                ))

        lines.append(CONTEXT_RECENT_FOOTER)
        context = "\n".join(lines)
        executor._add_context(sender_phone, context)

        # Track most recent item for pronoun resolution
        first = available[0] if available else (requested[0] if requested else None)
        if first:
            year_str = f" ({first['year']})" if first.get("year") else ""
            executor._last_topic[sender_phone] = {
                "title": f"{first['title']}{year_str}",
                "tmdb_id": first["tmdb_id"],
                "media_type": first["media_type"],
            }

        return (await executor._llm_respond(sender_phone, scenario="recent_results"))[0]
    except Exception as e:
        log.error("Server state fetch failed: %s", e)
        return ERROR_GENERIC
