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
    filter_available,
    format_recommendations,
    format_search_results,
)

log = logging.getLogger(__name__)


async def handle_recommend(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Discover movies/shows by genre, year, trending, or similar to a title."""
    query = (decision.query or decision.message or "").lower()

    # Collect tmdb_ids already shown in this conversation to avoid repeats
    history = await executor.db.get_history(sender_phone)
    shown_ids: set[int] = set()
    for entry in history:
        if entry.role == "context":
            for m in re.finditer(r"tmdb:(\d+)", entry.content):
                shown_ids.add(int(m.group(1)))

    # Check for "similar to X" / "something like X" / "like X" / "more like X"
    similar_match = re.match(
        r"(?:similar to|something like|more like|like)\s+(.+)",
        query,
    )
    if similar_match:
        title = similar_match.group(1).strip()
        try:
            search_results = await executor.seerr.search(title)
        except SeerrError as e:
            log.error("Similar-to search failed: %s", e)
            search_results = []

        if search_results:
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

            if results:
                results = filter_available(results, take=3)
                await executor._enrich_results(results)

                results = await executor._send_result_posters(sender_phone, results)

                # Store context and let LLM craft the response
                await executor.db.add_history(
                    sender_phone, "context",
                    f"[Recommendations similar to {base.title}]",
                )
                context = format_search_results(results)
                await executor.db.add_history(sender_phone, "context", context)

                return await executor._llm_respond(
                    sender_phone,
                    fallback=format_recommendations(results, similar_to=base.title),
                )

        # Fall through to genre/trending logic if search or recommendations failed

    # Determine media type from query
    want_movie = any(w in query for w in ("movie", "film"))
    want_tv = any(w in query for w in ("tv", "show", "series"))
    # If neither specified, do both
    want_both = not want_movie and not want_tv

    # Extract year(s) — support ranges like "2024 2025"
    year_matches = re.findall(r"\b(19\d{2}|20\d{2})\b", query)
    if year_matches:
        years = sorted(set(int(y) for y in year_matches))
        year = years[0]
        year_end = years[-1] if len(years) > 1 else None
    else:
        year = None
        year_end = None

    # Find genre keyword using dynamic genre maps
    genre_keyword: str | None = None
    try:
        movie_genres = await executor.seerr.get_genre_map("movie")
        tv_genres = await executor.seerr.get_genre_map("tv")
    except Exception as e:
        log.warning("Genre map fetch failed, recommendations may miss genre filters: %s", e)
        movie_genres = {}
        tv_genres = {}

    all_genre_names = sorted(
        set(list(movie_genres.keys()) + list(tv_genres.keys())),
        key=lambda g: -len(g),
    )
    for genre in all_genre_names:
        if genre in query:
            genre_keyword = genre
            break

    # Determine if this is a trending request
    is_trending = "trending" in query or (not genre_keyword and not year)

    try:
        results: list[SearchResult] = []
        if is_trending and not genre_keyword and not year:
            results = await executor.seerr.discover_trending(take=10, exclude_ids=shown_ids)
        elif want_both:
            movie_genre_id = movie_genres.get(genre_keyword) if genre_keyword else None
            tv_genre_id = tv_genres.get(genre_keyword) if genre_keyword else None
            movie_results, tv_results = await asyncio.gather(
                executor.seerr.discover_movies(
                    genre_id=movie_genre_id, year=year, year_end=year_end,
                    take=6, exclude_ids=shown_ids,
                ),
                executor.seerr.discover_tv(
                    genre_id=tv_genre_id, year=year, year_end=year_end,
                    take=6, exclude_ids=shown_ids,
                ),
            )
            results = movie_results + tv_results
        elif want_movie:
            genre_id = movie_genres.get(genre_keyword) if genre_keyword else None
            results = await executor.seerr.discover_movies(
                genre_id=genre_id, year=year, year_end=year_end,
                take=10, exclude_ids=shown_ids,
            )
        else:  # want_tv
            genre_id = tv_genres.get(genre_keyword) if genre_keyword else None
            results = await executor.seerr.discover_tv(
                genre_id=genre_id, year=year, year_end=year_end,
                take=10, exclude_ids=shown_ids,
            )
    except Exception as e:
        log.error("Discover failed: %s", e)
        return ERROR_GENERIC

    if not results:
        return "Couldn't find any recommendations for that."

    # Prefer results the user doesn't already have
    results = filter_available(results, take=3)

    await executor._enrich_results(results)

    results = await executor._send_result_posters(sender_phone, results)

    # Store context and let LLM craft the response
    context = format_search_results(results)
    await executor.db.add_history(sender_phone, "context", context)

    return await executor._llm_respond(
        sender_phone,
        fallback=format_recommendations(results),
    )
