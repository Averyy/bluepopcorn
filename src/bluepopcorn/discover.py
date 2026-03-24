"""Shared discovery orchestration: genre resolution, multi-strategy recommend, similar-to."""

from __future__ import annotations

import asyncio
import logging

from .seerr import SeerrClient, SeerrError
from .types import SearchResult

log = logging.getLogger(__name__)


async def resolve_genre_ids(
    seerr: SeerrClient,
    genre_keyword: str,
) -> tuple[int | None, int | None, str | None, list[str] | None]:
    """Resolve a genre keyword to movie and TV genre IDs.

    Returns (movie_genre_id, tv_genre_id, fallback_keyword, available_genres).
    If the genre is not found in either map, fallback_keyword is set
    to the original keyword and available_genres lists valid genre names.
    """
    try:
        movie_genres = await seerr.get_genre_map("movie")
        tv_genres = await seerr.get_genre_map("tv")
    except Exception as e:
        log.error("Genre map fetch failed — genre-based discovery unavailable: %s", e)
        movie_genres = {}
        tv_genres = {}

    movie_genre_id = movie_genres.get(genre_keyword)
    tv_genre_id = tv_genres.get(genre_keyword)

    if not movie_genre_id and not tv_genre_id:
        log.info("Genre '%s' not found in genre maps, will use as keyword", genre_keyword)
        # Collect one name per genre ID (shortest = most natural, e.g. "comedy" not "comedy & drama")
        merged = {**movie_genres, **tv_genres}
        id_to_names: dict[int, list[str]] = {}
        for name, gid in merged.items():
            id_to_names.setdefault(gid, []).append(name)
        available = sorted({min(names, key=len) for names in id_to_names.values()})
        return None, None, genre_keyword, available

    return movie_genre_id, tv_genre_id, None, None


async def discover_recommendations(
    seerr: SeerrClient,
    *,
    genre: str | None = None,
    keyword: str | None = None,
    media_type: str | None = None,
    year: int | None = None,
    year_end: int | None = None,
    trending: bool = False,
    upcoming: bool = False,
    query: str | None = None,
    take: int = 5,
    exclude_ids: set[int] | None = None,
) -> tuple[list[SearchResult], list[str] | None]:
    """Run multi-strategy discovery and return combined, deduped results.

    Strategies run in parallel: person search, combined genre+keyword discover,
    keyword-only discover, genre-only discover, trending, upcoming, plain search.
    Results are combined by priority order and deduped by tmdb_id.

    Returns (results, available_genres). available_genres is set when a genre
    argument was not recognized, so the caller can surface valid options.
    When query is None, the plain-search strategy is disabled (MCP path).
    """
    exclude = exclude_ids or set()
    genre_keyword = genre.lower().strip() if genre else None
    kw_query = keyword.strip() if keyword else None

    # Resolve genre name -> genre IDs
    movie_genre_id = None
    tv_genre_id = None
    available_genres: list[str] | None = None
    if genre_keyword:
        movie_genre_id, tv_genre_id, fallback, available_genres = await resolve_genre_ids(seerr, genre_keyword)
        if fallback:
            if not kw_query:
                kw_query = fallback
            genre_keyword = None

    # Phase 1: Keyword search (one fast API call)
    keyword_ids = await seerr.search_keywords(kw_query) if kw_query else []

    # Phase 2: Run discovery strategies in parallel (order = priority)
    coros: dict[str, object] = {}

    # Person search (catches "anything with [actor]", "[director] movies")
    if kw_query and not genre_keyword and not trending:
        coros["person"] = seerr.search_person_credits(
            kw_query, want_type=media_type, take=take, exclude_ids=exclude,
        )

    # Combined genre+keyword discover (most precise)
    if keyword_ids and genre_keyword:
        if media_type != "tv" and movie_genre_id:
            coros["combined_movie"] = seerr.discover_movies(
                genre_id=movie_genre_id, keyword_ids=keyword_ids,
                year=year, year_end=year_end, take=take, exclude_ids=exclude,
            )
        if media_type != "movie" and tv_genre_id:
            coros["combined_tv"] = seerr.discover_tv(
                genre_id=tv_genre_id, keyword_ids=keyword_ids,
                year=year, year_end=year_end, take=take, exclude_ids=exclude,
            )

    # Keyword-only discover (broader)
    if keyword_ids:
        if media_type != "tv":
            coros["kw_movie"] = seerr.discover_movies(
                keyword_ids=keyword_ids, year=year,
                take=7, exclude_ids=exclude,
            )
        if media_type != "movie":
            coros["kw_tv"] = seerr.discover_tv(
                keyword_ids=keyword_ids, year=year,
                take=7, exclude_ids=exclude,
            )

    # Genre-only discover
    if genre_keyword:
        if media_type != "tv" and movie_genre_id:
            coros["genre_movie"] = seerr.discover_movies(
                genre_id=movie_genre_id, year=year,
                take=7, exclude_ids=exclude,
            )
        if media_type != "movie" and tv_genre_id:
            coros["genre_tv"] = seerr.discover_tv(
                genre_id=tv_genre_id, year=year,
                take=7, exclude_ids=exclude,
            )

    if trending:
        coros["trending"] = seerr.discover_trending(take=take, exclude_ids=exclude)

    if upcoming:
        if media_type != "tv":
            coros["upcoming_movies"] = seerr.discover_upcoming_movies(
                take=take, exclude_ids=exclude,
            )
        if media_type != "movie":
            coros["upcoming_tv"] = seerr.discover_upcoming_tv(
                take=take, exclude_ids=exclude,
            )

    # Plain search as parallel strategy
    search_query = query or keyword or genre or ""
    if search_query:
        coros["search"] = seerr.search(search_query, media_type=media_type)

    if not coros:
        return [], available_genres

    keys = list(coros.keys())
    values = await asyncio.gather(*coros.values(), return_exceptions=True)

    # Combine & dedupe by tmdb_id, preserving priority order
    seen_ids: set[int] = set(exclude)
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
    if media_type:
        typed = [r for r in combined if r.media_type == media_type]
        if typed:
            combined = typed

    return combined[:take], available_genres


async def find_similar(
    seerr: SeerrClient,
    title: str,
    *,
    media_type: str | None = None,
    exclude_ids: set[int] | None = None,
    take: int = 7,
) -> tuple[list[SearchResult], str | None]:
    """Find titles similar to the given title.

    Returns (results, base_title). base_title is the title of the item
    used as the similarity base, or None if the search found nothing.
    """
    exclude = exclude_ids or set()

    try:
        search_results = await seerr.search(title)
        if not search_results:
            words = title.split()
            for n in range(min(len(words) - 1, 4), 0, -1):
                shorter = " ".join(words[:n])
                search_results = await seerr.search(shorter)
                if search_results:
                    log.info("Similar-to fallback matched on: %s", shorter)
                    break
    except SeerrError as e:
        log.error("Similar-to search failed: %s", e)
        search_results = []

    if not search_results:
        return [], None

    base = search_results[0]
    try:
        recs, similar = await asyncio.gather(
            seerr.get_recommendations(base.media_type, base.tmdb_id, take=10, exclude_ids=exclude),
            seerr.get_similar(base.media_type, base.tmdb_id, take=10, exclude_ids=exclude),
            return_exceptions=True,
        )
        results = []
        if not isinstance(recs, Exception) and recs:
            results = recs
        elif not isinstance(similar, Exception) and similar:
            results = similar
        # Log any errors
        for label, val in [("recommendations", recs), ("similar", similar)]:
            if isinstance(val, Exception):
                log.warning("Similar-to %s lookup failed: %s", label, val)
    except SeerrError as e:
        log.error("Recommendations/similar lookup failed: %s", e)
        results = []

    return results[:take], base.title
