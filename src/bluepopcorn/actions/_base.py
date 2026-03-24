"""Shared formatters for action handlers."""

from __future__ import annotations

import logging

from ..types import MediaStatus, SearchResult, status_label_for

log = logging.getLogger(__name__)


def format_result_line(
    index: int,
    title: str,
    year: int | str | None,
    media_type: str,
    tmdb_id: int,
    overview: str,
    status_str: str,
    *,
    extras: str = "",
) -> str:
    """Format a single result line in the shared context format."""
    year_str = f" ({year})" if year else ""
    type_str = "TV" if media_type == "tv" else "Movie"
    overview = overview or "No description"
    return (
        f"{index}. {title}{year_str} [{type_str}] tmdb:{tmdb_id} "
        f"- {overview} (Status: {status_str}){extras}"
    )


def format_search_results(results: list[SearchResult], query: str | None = None) -> str:
    """Format search results as context for the LLM.

    Callers must check for empty results before calling — all call sites
    short-circuit to a specific CONTEXT_*_EMPTY string before reaching here.
    """
    header = f"[Search results for '{query}':" if query else "[Search results:"
    lines = [header]
    for i, r in enumerate(results, 1):
        # Build extras suffix (ratings, air date, trailer)
        rating_str = f" Rating: {r.rating}/10" if r.rating else ""
        ext_ratings: list[str] = []
        if r.rt_rating:
            ext_ratings.append(f"RT: {r.rt_rating}")
        if r.imdb_rating:
            ext_ratings.append(f"IMDB: {r.imdb_rating}")
        ext_rating_str = " | ".join(ext_ratings)
        if ext_rating_str:
            rating_str += f" | {ext_rating_str}" if rating_str else f" {ext_rating_str}"
        trailer_str = f" Trailer: {r.trailer_url}" if r.trailer_url else ""
        air_date_str = f" | Air date: {r.next_air_date}" if r.next_air_date else ""
        season_str = f" | {r.season_count} season{'s' if r.season_count != 1 else ''}" if r.season_count else ""
        collection_str = f" | Collection: {r.collection_name} (id: {r.collection_id})" if r.collection_name else ""
        lines.append(format_result_line(
            i, r.title, r.year, r.media_type, r.tmdb_id, r.overview,
            status_label_for(r.status, r.download_progress),
            extras=f"{rating_str}{air_date_str}{season_str}{collection_str}{trailer_str}",
        ))
    lines.append("]")
    return "\n".join(lines)


def filter_available(results: list[SearchResult], take: int = 3) -> list[SearchResult]:
    """Prefer results the user doesn't already have for recommendations."""
    new = [r for r in results if r.status not in (
        MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE,
    )]
    if new:
        return new[:take]
    return results[:take]
