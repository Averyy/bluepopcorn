from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..prompts import CONTEXT_SEARCH_EMPTY, CONTEXT_SEARCH_ERROR, ERROR_GENERIC
from ..seerr import SeerrSearchError
from ..types import LLMDecision
from ._base import format_search_results

log = logging.getLogger(__name__)


async def handle_search(
    executor: ActionExecutor,
    decision: LLMDecision,
    sender_phone: str,
    user_text: str = "",
) -> str:
    """Execute a search action: search Seerr, LLM responds, THEN send poster."""
    query = decision.query or decision.message
    try:
        results = await executor.seerr.search(query, media_type=decision.media_type)
    except SeerrSearchError:
        executor._add_context(sender_phone, CONTEXT_SEARCH_ERROR.format(query=query))
        return (await executor._llm_respond(sender_phone, scenario="search_error"))[0]
    except Exception as e:
        log.error("Search failed for '%s': %s", query, e)
        return ERROR_GENERIC

    if not results:
        executor._add_context(sender_phone, CONTEXT_SEARCH_EMPTY.format(query=query))
        return (await executor._llm_respond(sender_phone, scenario="search_empty"))[0]

    await executor._enrich_results(results, enrich_downloads=True)

    # Send all results to the LLM — let it decide what to present
    display_results = results

    # Store context with all results so the LLM can make an informed choice
    context = format_search_results(display_results, query=query)
    executor._add_context(sender_phone, context)

    # Skip posters if the top result matches the last discussed title (follow-up)
    top = results[0]
    topic = executor._last_topic.get(sender_phone)
    skip_poster = bool(topic and topic["tmdb_id"] == top.tmdb_id)

    # Track the most recently discussed title
    year_str = f" ({top.year})" if top.year else ""
    executor._last_topic[sender_phone] = {
        "title": f"{top.title}{year_str}",
        "tmdb_id": top.tmdb_id,
        "media_type": top.media_type,
    }

    return await executor._send_with_poster(
        sender_phone, display_results, scenario="search_results",
        skip_poster=skip_poster,
    )
