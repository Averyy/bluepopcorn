from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..seerr import SeerrError
from ..types import LLMDecision, SearchResult
from ._base import (
    ERROR_GENERIC,
    format_recommendations,
    format_search_results,
)

log = logging.getLogger(__name__)


async def handle_recommend(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Discover movies/shows by genre, keyword, year, trending, or similar."""
    want_type = decision.media_type

    # Collect tmdb_ids already shown in this session to avoid repeats
    shown_ids: set[int] = set()
    for _ts, ctx_text in executor.get_context_entries(sender_phone):
        for m in re.finditer(r"tmdb:(\d+)", ctx_text):
            shown_ids.add(int(m.group(1)))

    # "Similar to X" — LLM specifies the title directly
    if decision.similar_to:
        return await _handle_similar(
            executor, decision.similar_to,
            sender_phone, want_type, shown_ids,
        )

    # Use structured fields from LLM decision
    genre_keyword = decision.genre.lower().strip() if decision.genre else None
    kw_query = decision.keyword.strip() if decision.keyword else None
    year = decision.year
    year_end = decision.year_end
    is_trending = decision.trending
    take = min(decision.count or 5, 10)  # LLM-controlled, capped at 10

    # Resolve genre name → genre IDs for the discover API
    movie_genre_id = None
    tv_genre_id = None
    if genre_keyword:
        try:
            movie_genres = await executor.seerr.get_genre_map("movie")
            tv_genres = await executor.seerr.get_genre_map("tv")
        except Exception as e:
            log.warning("Genre map fetch failed: %s", e)
            movie_genres = {}
            tv_genres = {}
        movie_genre_id = movie_genres.get(genre_keyword)
        tv_genre_id = tv_genres.get(genre_keyword)
        if not movie_genre_id and not tv_genre_id:
            log.info("Genre '%s' not found in genre maps, will use as keyword", genre_keyword)
            # LLM specified a genre we don't recognize — treat as keyword instead
            if not kw_query:
                kw_query = genre_keyword
            genre_keyword = None

    # Phase 1: Keyword search (one fast API call)
    keyword_ids = (
        await executor.seerr.search_keywords(kw_query) if kw_query else []
    )

    # Phase 2: Run discovery strategies in parallel (order = priority)
    coros: dict[str, object] = {}

    # Person search (catches "anything with [actor]", "[director] movies")
    if kw_query and not genre_keyword and not is_trending:
        coros["person"] = executor.seerr.search_person_credits(
            kw_query, want_type=want_type, take=take, exclude_ids=shown_ids,
        )

    # Combined genre+keyword discover (most precise)
    if keyword_ids and genre_keyword:
        if want_type != "tv" and movie_genre_id:
            coros["combined_movie"] = executor.seerr.discover_movies(
                genre_id=movie_genre_id, keyword_ids=keyword_ids,
                year=year, year_end=year_end, take=take, exclude_ids=shown_ids,
            )
        if want_type != "movie" and tv_genre_id:
            coros["combined_tv"] = executor.seerr.discover_tv(
                genre_id=tv_genre_id, keyword_ids=keyword_ids,
                year=year, year_end=year_end, take=take, exclude_ids=shown_ids,
            )

    # Keyword-only discover (broader)
    if keyword_ids:
        if want_type != "tv":
            coros["kw_movie"] = executor.seerr.discover_movies(
                keyword_ids=keyword_ids, year=year,
                take=7, exclude_ids=shown_ids,
            )
        if want_type != "movie":
            coros["kw_tv"] = executor.seerr.discover_tv(
                keyword_ids=keyword_ids, year=year,
                take=7, exclude_ids=shown_ids,
            )

    # Genre-only discover
    if genre_keyword:
        if want_type != "tv" and movie_genre_id:
            coros["genre_movie"] = executor.seerr.discover_movies(
                genre_id=movie_genre_id, year=year,
                take=7, exclude_ids=shown_ids,
            )
        if want_type != "movie" and tv_genre_id:
            coros["genre_tv"] = executor.seerr.discover_tv(
                genre_id=tv_genre_id, year=year,
                take=7, exclude_ids=shown_ids,
            )

    if is_trending:
        coros["trending"] = executor.seerr.discover_trending(
            take=take, exclude_ids=shown_ids,
        )

    # Plain search as parallel strategy (catches title/keyword text matches)
    search_query = decision.query or decision.keyword or decision.genre or ""
    label = genre_keyword or kw_query or ("trending" if is_trending else search_query)
    if search_query:
        coros["search"] = executor.seerr.search(search_query, media_type=want_type)

    if not coros:
        executor._add_context(sender_phone, "[Recommendations: no search criteria provided]")
        return (await executor._llm_respond(sender_phone, fallback="Couldn't find any recommendations for that.", intent="recommend"))[0]

    try:
        keys = list(coros.keys())
        values = await asyncio.gather(*coros.values(), return_exceptions=True)
    except Exception as e:
        log.error("Discover failed: %s", e)
        return ERROR_GENERIC

    # Combine & dedupe by tmdb_id, preserving priority order
    seen_ids: set[int] = set(shown_ids)
    combined: list[SearchResult] = []
    for key, val in zip(keys, values):
        if isinstance(val, Exception):
            log.warning("Recommend strategy '%s' failed: %s", key, val)
            continue
        for r in val:
            if r.tmdb_id not in seen_ids:
                seen_ids.add(r.tmdb_id)
                combined.append(r)

    # Post-filter by media type for mixed sources (trending, search)
    if want_type:
        typed = [r for r in combined if r.media_type == want_type]
        if typed:
            combined = typed

    if not combined:
        executor._add_context(sender_phone, f'[Recommendations for "{label}": no results found]')
        return (await executor._llm_respond(sender_phone, fallback="Couldn't find any recommendations for that.", intent="recommend"))[0]

    results = combined[:take]

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
        sender_phone, display_results,
        fallback=format_recommendations(display_results),
        intent="recommend",
    )


async def _handle_similar(
    executor: ActionExecutor, title: str, sender_phone: str,
    want_type: str | None, shown_ids: set[int],
) -> str:
    """Handle 'similar to X' / 'something like X' recommendations."""
    try:
        search_results = await executor.seerr.search(title)
        # If no results, try progressively shorter queries
        # (handles "Inception mind-bending sci-fi thriller" → "Inception")
        if not search_results:
            words = title.split()
            for n in range(min(len(words) - 1, 4), 0, -1):
                shorter = " ".join(words[:n])
                search_results = await executor.seerr.search(shorter)
                if search_results:
                    log.info("Similar-to fallback matched on: %s", shorter)
                    break
    except SeerrError as e:
        log.error("Similar-to search failed: %s", e)
        search_results = []

    if not search_results:
        executor._add_context(sender_phone, f"[Similar to \"{title}\": couldn't find the base title]")
        return (await executor._llm_respond(sender_phone, fallback=f"Couldn't find \"{title}\" to base recommendations on.", intent="recommend"))[0]

    base = search_results[0]
    try:
        results = await executor.seerr.get_recommendations(
            base.media_type, base.tmdb_id, take=10, exclude_ids=shown_ids,
        )
        if not results:
            results = await executor.seerr.get_similar(
                base.media_type, base.tmdb_id, take=10, exclude_ids=shown_ids,
            )
    except SeerrError as e:
        log.error("Recommendations/similar lookup failed: %s", e)
        results = []

    if not results:
        executor._add_context(sender_phone, f"[Similar to \"{base.title}\": no recommendations found]")
        return (await executor._llm_respond(sender_phone, fallback=f"Couldn't find recommendations similar to {base.title}.", intent="recommend"))[0]

    results = results[:7]
    await executor._enrich_results(results)

    # Pre-filter to results with poster images
    with_posters = [r for r in results if r.poster_path]
    display_results = with_posters if with_posters else results

    # Store context
    executor._add_context(sender_phone, f"[Recommendations similar to {base.title}]")
    context = format_search_results(display_results, query=f"similar to {base.title}")
    executor._add_context(sender_phone, context)

    return await executor._send_with_poster(
        sender_phone, display_results,
        fallback=format_recommendations(display_results, similar_to=base.title),
        intent="recommend",
    )
