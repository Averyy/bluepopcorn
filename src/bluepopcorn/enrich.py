"""Shared enrichment: ratings, trailers, air dates, download progress."""

from __future__ import annotations

import asyncio
import logging

from .seerr import SeerrClient
from .types import SearchResult

log = logging.getLogger(__name__)


def _apply_ratings(result: SearchResult, rating_dict: dict) -> None:
    """Apply enriched ratings from get_ratings() to a SearchResult."""
    rt = rating_dict.get("rt")
    freshness = rating_dict.get("rt_freshness")
    if rt and freshness:
        result.rt_rating = f"{rt} {freshness}"
    elif rt:
        result.rt_rating = rt
    result.imdb_rating = rating_dict.get("imdb")


async def enrich_results(
    seerr: SeerrClient,
    results: list[SearchResult],
    *,
    enrich_downloads: bool = False,
) -> None:
    """Fetch trailers, ratings, air dates, and optionally download progress for results."""
    if not results:
        return
    detail_tasks = [seerr.get_detail_extras(r.media_type, r.tmdb_id) for r in results]
    rating_tasks = [seerr.get_ratings(r.media_type, r.tmdb_id) for r in results]
    n = len(results)
    all_results = await asyncio.gather(*detail_tasks, *rating_tasks, return_exceptions=True)
    details = all_results[:n]
    ratings = all_results[n : n * 2]
    for i, extras in enumerate(details):
        if i >= len(results):
            break
        if isinstance(extras, Exception):
            log.debug("Detail enrichment failed for %s/%d: %s", results[i].media_type, results[i].tmdb_id, extras)
            continue
        if extras.get("trailer"):
            results[i].trailer_url = extras["trailer"]
        if extras.get("air_date"):
            results[i].next_air_date = extras["air_date"]
        if enrich_downloads and extras.get("download_progress"):
            results[i].download_progress = extras["download_progress"]
        if extras.get("collection_id"):
            results[i].collection_id = extras["collection_id"]
            results[i].collection_name = extras.get("collection_name")
        if extras.get("season_count"):
            results[i].season_count = extras["season_count"]
    for i, rating_dict in enumerate(ratings):
        if isinstance(rating_dict, Exception):
            log.debug("Rating enrichment failed for %s/%d: %s", results[i].media_type, results[i].tmdb_id, rating_dict)
            continue
        if rating_dict and i < len(results):
            _apply_ratings(results[i], rating_dict)
