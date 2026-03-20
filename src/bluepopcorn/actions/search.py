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
    executor: ActionExecutor,
    decision: LLMDecision,
    sender_phone: str,
    user_text: str = "",
) -> str:
    """Execute a search action: search Seerr, LLM responds, THEN send poster."""
    from ._base import ERROR_GENERIC

    query = decision.query or decision.message
    try:
        results = await executor.seerr.search(query, media_type=decision.media_type)
    except SeerrSearchError:
        executor._add_context(sender_phone, f'[Search for "{query}": search failed]')
        return (await executor._llm_respond(sender_phone, fallback=f"Couldn't find anything for \"{query}\".", intent="search"))[0]
    except Exception as e:
        log.error("Search failed for '%s': %s", query, e)
        return ERROR_GENERIC

    if not results:
        executor._add_context(sender_phone, f'[Search for "{query}": no results found]')
        return (await executor._llm_respond(sender_phone, fallback=f"Couldn't find anything for \"{query}\".", intent="search"))[0]

    await executor._enrich_results(results, enrich_downloads=True)

    # Send all results to the LLM — let it decide what to present
    display_results = results

    # Store context with all results so the LLM can make an informed choice
    context = format_search_results(display_results, query=query)
    executor._add_context(sender_phone, context)

    # Track the most recently discussed title
    top = results[0]
    year_str = f" ({top.year})" if top.year else ""
    executor._last_topic[sender_phone] = {
        "title": f"{top.title}{year_str}",
        "tmdb_id": top.tmdb_id,
        "media_type": top.media_type,
    }

    fallback = (
        format_multiple_results(display_results)
        if len(display_results) > 1
        else format_single_result(results[0])
    )
    return await executor._send_with_poster(
        sender_phone, display_results,
        fallback=fallback, intent="search",
    )
