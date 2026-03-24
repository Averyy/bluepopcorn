from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..discover import discover_recommendations, find_similar
from ..prompts import (
    CONTEXT_RECOMMEND_EMPTY,
    CONTEXT_RECOMMEND_NO_CRITERIA,
    CONTEXT_SIMILAR_EMPTY,
    CONTEXT_SIMILAR_HEADER,
    CONTEXT_SIMILAR_NOT_FOUND,
    ERROR_GENERIC,
)
from ..types import LLMDecision
from ._base import format_search_results

log = logging.getLogger(__name__)

_TMDB_RE = re.compile(r"tmdb:(\d+)")


async def handle_recommend(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Discover movies/shows by genre, keyword, year, trending, or similar."""
    want_type = decision.media_type

    # Collect tmdb_ids already shown in this session to avoid repeats
    shown_ids: set[int] = set()
    for _ts, ctx_text in executor.get_context_entries(sender_phone):
        for m in _TMDB_RE.finditer(ctx_text):
            shown_ids.add(int(m.group(1)))

    # "Similar to X" — LLM specifies the title directly
    if decision.similar_to:
        return await _handle_similar(
            executor, decision.similar_to,
            sender_phone, want_type, shown_ids,
        )

    take = min(decision.count or 5, 10)  # LLM-controlled, capped at 10
    label = (
        (decision.genre.lower().strip() if decision.genre else None)
        or (decision.keyword.strip() if decision.keyword else None)
        or ("upcoming" if decision.upcoming else "trending" if decision.trending else (decision.query or ""))
    )

    try:
        results, _available = await discover_recommendations(
            executor.seerr,
            genre=decision.genre,
            keyword=decision.keyword,
            media_type=want_type,
            year=decision.year,
            year_end=decision.year_end,
            trending=decision.trending,
            upcoming=decision.upcoming,
            query=decision.query or decision.keyword or decision.genre or "",
            take=take,
            exclude_ids=shown_ids,
        )
    except Exception as e:
        log.error("Discover failed: %s", e)
        return ERROR_GENERIC

    if not results and not (decision.genre or decision.keyword or decision.trending or decision.upcoming or decision.query):
        executor._add_context(sender_phone, CONTEXT_RECOMMEND_NO_CRITERIA)
        return (await executor._llm_respond(sender_phone, scenario="recommend_no_criteria"))[0]

    if not results:
        executor._add_context(sender_phone, CONTEXT_RECOMMEND_EMPTY.format(label=label))
        return (await executor._llm_respond(sender_phone, scenario="recommend_empty"))[0]

    await executor._enrich_results(results)

    # Pre-filter to results with poster images (collage numbering consistency)
    with_posters = [r for r in results if r.poster_path]
    display_results = with_posters if with_posters else results

    # Store context using display_results (matches collage numbering)
    context = format_search_results(display_results, query=f"recommend {label}")
    executor._add_context(sender_phone, context)

    # Track the top result for pronoun resolution ("add the first one")
    if display_results:
        top = display_results[0]
        year_str = f" ({top.year})" if top.year else ""
        executor._last_topic[sender_phone] = {
            "title": f"{top.title}{year_str}",
            "tmdb_id": top.tmdb_id,
            "media_type": top.media_type,
        }

    return await executor._send_with_poster(
        sender_phone, display_results, scenario="recommend_results",
    )


async def _handle_similar(
    executor: ActionExecutor, title: str, sender_phone: str,
    want_type: str | None, shown_ids: set[int],
) -> str:
    """Handle 'similar to X' / 'something like X' recommendations."""
    results, base_title = await find_similar(
        executor.seerr, title,
        media_type=want_type, exclude_ids=shown_ids,
    )

    if base_title is None:
        executor._add_context(sender_phone, CONTEXT_SIMILAR_NOT_FOUND.format(title=title))
        return (await executor._llm_respond(sender_phone, scenario="similar_not_found"))[0]

    if not results:
        executor._add_context(sender_phone, CONTEXT_SIMILAR_EMPTY.format(title=base_title))
        return (await executor._llm_respond(sender_phone, scenario="similar_empty"))[0]

    await executor._enrich_results(results)

    # Pre-filter to results with poster images
    with_posters = [r for r in results if r.poster_path]
    display_results = with_posters if with_posters else results

    # Store context
    executor._add_context(sender_phone, CONTEXT_SIMILAR_HEADER.format(title=base_title))
    context = format_search_results(display_results, query=f"similar to {base_title}")
    executor._add_context(sender_phone, context)

    return await executor._send_with_poster(
        sender_phone, display_results, scenario="similar_results",
    )
