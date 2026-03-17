from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..seerr import SeerrSearchError
from ..types import LLMDecision, MediaStatus
from ._base import format_search_results, format_single_result, format_multiple_results

log = logging.getLogger(__name__)


async def handle_search(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Execute a search action: search Seerr, send poster, format results."""
    from ._base import ERROR_GENERIC

    query = decision.query or decision.message
    try:
        results = await executor.seerr.search(query)
    except SeerrSearchError:
        return f"Couldn't find anything for \"{query}\"."
    except Exception as e:
        log.error("Search failed for '%s': %s", query, e)
        return ERROR_GENERIC

    if not results:
        await executor.db.add_history(sender_phone, "context", "[No results found]")
        return f"Couldn't find anything for \"{query}\"."

    await executor._enrich_results(results, enrich_downloads=True)

    # Fetch history once for poster logic and narrowing
    history = await executor.db.get_history(sender_phone)

    # If multiple results but one matches a recently discussed title, narrow to it
    if len(results) > 1:
        recent_tmdb_ids: set[int] = set()
        for entry in reversed(history):
            if entry.role == "context":
                for r in results:
                    if f"tmdb:{r.tmdb_id}" in entry.content:
                        recent_tmdb_ids.add(r.tmdb_id)
            # Only look back through recent exchanges
            if entry.role == "user" and len(recent_tmdb_ids) > 0:
                break
        if recent_tmdb_ids:
            matched = [r for r in results if r.tmdb_id in recent_tmdb_ids]
            if len(matched) == 1:
                results = matched

    # Send poster — collage for add/request disambiguation,
    # single poster for info queries, skip for status checks
    if executor.sender and executor.posters:
        last_user_msg = ""
        for entry in reversed(history):
            if entry.role == "user":
                last_user_msg = entry.content.lower()
                break
        adding = any(w in last_user_msg for w in ("add", "request", "get", "download"))
        checking_status = any(w in last_user_msg for w in (
            "status", "done", "ready", "downloading", "is it",
            "update", "progress", "where is", "how is",
        ))
        # Skip poster if it was already sent (e.g. in a collage from recommend)
        already_shown = results[0].tmdb_id in executor._sent_posters.get(sender_phone, set())
        if adding and len(results) > 1:
            results = await executor._send_result_posters(sender_phone, results)
        elif not checking_status and not already_shown and results:
            await executor._send_single_poster(sender_phone, results[0])
            await executor.sender.start_typing(sender_phone)

    # Store results as context, then let the LLM craft the response.
    context = format_search_results(results)
    await executor.db.add_history(sender_phone, "context", context)

    fallback = (
        format_single_result(results[0])
        if len(results) == 1
        else format_multiple_results(results)
    )
    return await executor._llm_respond(sender_phone, fallback=fallback)
