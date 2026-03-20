"""Constants, dataclasses, and standalone formatters shared across handlers."""

from __future__ import annotations

import logging

from ..types import MediaStatus, SearchResult

log = logging.getLogger(__name__)

ERROR_GENERIC = "Server error, please try again later."


def format_search_results(results: list[SearchResult], query: str | None = None) -> str:
    """Format search results as context for the LLM."""
    if not results:
        return "[No results found]"

    header = f"[Search results for '{query}':" if query else "[Search results:"
    lines = [header]
    for i, r in enumerate(results, 1):
        year_str = f" ({r.year})" if r.year else ""
        type_str = "TV" if r.media_type == "tv" else "Movie"
        overview = r.overview if r.overview else "No description"
        rating_str = f" Rating: {r.rating}/10" if r.rating else ""
        # Append RT and IMDB ratings when available
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
        lines.append(
            f"{i}. {r.title}{year_str} [{type_str}] tmdb:{r.tmdb_id} "
            f"- {overview} (Status: {r.status_label}){rating_str}{air_date_str}{trailer_str}"
        )
    lines.append("]")
    return "\n".join(lines)


def apply_ratings(result: SearchResult, rating_dict: dict) -> None:
    """Apply enriched ratings from get_ratings() to a SearchResult."""
    rt = rating_dict.get("rt")
    freshness = rating_dict.get("rt_freshness")
    if rt and freshness:
        result.rt_rating = f"{rt} {freshness}"
    elif rt:
        result.rt_rating = rt
    result.imdb_rating = rating_dict.get("imdb")


def format_rating_str(r: SearchResult) -> str:
    """Build a compact rating string from all available sources."""
    parts: list[str] = []
    if r.rt_rating:
        parts.append(f"{r.rt_rating} on RT")
    if r.imdb_rating:
        parts.append(f"{r.imdb_rating} on IMDB")
    if r.rating:
        parts.append(f"{r.rating}/10 on TMDB")
    if not parts:
        return ""
    return ", ".join(parts) + "."


def filter_available(results: list[SearchResult], take: int = 3) -> list[SearchResult]:
    """Prefer results the user doesn't already have for recommendations."""
    new = [r for r in results if r.status not in (
        MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE,
    )]
    if new:
        return new[:take]
    return results[:take]


def format_single_result(r: SearchResult) -> str:
    """Format a single search result as a casual text message."""
    year = f" ({r.year})" if r.year else ""
    title = f"{r.title}{year}"

    parts: list[str] = []

    if r.overview:
        overview = r.overview.rstrip(".")
        parts.append(f"{title} — {overview}.")
    else:
        parts.append(f"{title}.")

    # Ratings
    rating_str = format_rating_str(r)
    if rating_str:
        parts.append(rating_str)

    # Trailer
    if r.trailer_url:
        parts.append(f"Trailer: {r.trailer_url}")

    # Status
    parts.append(f"{r.status_label.capitalize()}.")

    return " ".join(parts)


def format_multiple_results(results: list[SearchResult]) -> str:
    """Format multiple search results as a numbered list for disambiguation."""
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        year = f" ({r.year})" if r.year else ""
        type_str = "TV" if r.media_type == "tv" else "Movie"

        overview = ""
        if r.overview:
            overview = r.overview.rstrip(".")
            overview = f" — {overview}."

        entry = f"{i}. {r.title}{year} [{type_str}]{overview}"

        rating_str = format_rating_str(r)
        if rating_str:
            entry += f" {rating_str}"

        if r.status == MediaStatus.AVAILABLE:
            entry += " (already in library)"
        elif r.status in (MediaStatus.PROCESSING, MediaStatus.PENDING):
            entry += " (already requested)"

        lines.append(entry)

    lines.append("\nWhich one?")
    return "\n".join(lines)


def format_recommendations(
    results: list[SearchResult], similar_to: str | None = None
) -> str:
    """Format recommendation results as a casual text message."""
    if similar_to:
        header = f"If you liked {similar_to}, check these out:"
    else:
        header = "Here are some picks:"
    lines = [header]

    for i, r in enumerate(results, 1):
        year = f" ({r.year})" if r.year else ""
        type_str = "TV" if r.media_type == "tv" else "Movie"

        overview = ""
        if r.overview:
            overview = r.overview.rstrip(".")
            overview = f" — {overview}."

        entry = f"{i}. {r.title}{year} [{type_str}]{overview}"

        rating_str = format_rating_str(r)
        if rating_str:
            entry += f" {rating_str}"

        if r.trailer_url:
            entry += f" Trailer: {r.trailer_url}"

        if r.status == MediaStatus.AVAILABLE:
            entry += " (already in library)"

        lines.append(entry)

    lines.append("\nWant me to add any of these?")
    return "\n".join(lines)
