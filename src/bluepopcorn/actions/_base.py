"""Constants, dataclasses, and standalone formatters shared across handlers."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..seerr import SeerrClient, seerr_title
from ..types import MediaStatus, SearchResult

log = logging.getLogger(__name__)

ERROR_GENERIC = "Something went wrong, try again in a sec."


@dataclass
class StatusData:
    """Structured result from _fetch_status_data."""
    processing_titles: list[str]
    pending_titles: list[str]
    recently_added: list[str]

    @property
    def has_activity(self) -> bool:
        return bool(self.processing_titles or self.pending_titles or self.recently_added)


def format_search_results(results: list[SearchResult]) -> str:
    """Format search results as context for the LLM."""
    if not results:
        return "[No results found]"

    lines = ["[Search results:"]
    for i, r in enumerate(results, 1):
        year_str = f" ({r.year})" if r.year else ""
        type_str = "TV" if r.media_type == "tv" else "Movie"
        overview = r.overview[:150] if r.overview else "No description"
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
        lines.append(
            f"{i}. {r.title}{year_str} [{type_str}] tmdb:{r.tmdb_id} "
            f"- {overview} (Status: {r.status_label}){rating_str}{trailer_str}"
        )
    lines.append("]")
    return "\n".join(lines)


async def resolve_request_title(req: dict, seerr: SeerrClient) -> str:
    """Resolve a display title from a Seerr request object.

    Request objects have media: MediaInfo which does NOT have a title field.
    Must look up via the detail endpoint using tmdbId.
    """
    media = req.get("media", {})
    tmdb_id = media.get("tmdbId")
    media_type = media.get("mediaType")
    if tmdb_id and media_type:
        try:
            detail = await seerr.get_media_status(media_type, tmdb_id)
            if detail:
                title = seerr_title(detail, default="")
                if title:
                    return title
        except Exception:
            pass
    # Last resort fallback
    slug = media.get("externalServiceSlug", "")
    if slug and not slug.isdigit():
        return slug.replace("-", " ").title()
    return "Unknown"


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


def truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, cutting at the last space."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rfind(" ")
    if cut > max_len // 2:
        return text[:cut]
    return text[:max_len]


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

    # Overview, truncated to ~200 chars
    if r.overview:
        overview = truncate(r.overview, 200).rstrip(".")
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

    # Status-dependent ending
    if r.status == MediaStatus.AVAILABLE:
        parts.append("Already in your library.")
    elif r.status == MediaStatus.PARTIALLY_AVAILABLE:
        parts.append("Some of this is already available. Want me to request the rest?")
    elif r.status == MediaStatus.PROCESSING:
        if r.download_progress:
            parts.append(f"Currently downloading ({r.download_progress}).")
        else:
            parts.append("Already requested, will download when available.")
    elif r.status == MediaStatus.PENDING:
        parts.append("Already requested, waiting on approval.")
    elif r.status == MediaStatus.BLOCKLISTED:
        parts.append("This title is blocklisted.")
    elif r.status == MediaStatus.DELETED:
        parts.append("This was previously deleted. Want me to re-request it?")
    else:
        parts.append("Want me to add it?")

    return " ".join(parts)


def format_multiple_results(results: list[SearchResult]) -> str:
    """Format multiple search results as a numbered list for disambiguation."""
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        year = f" ({r.year})" if r.year else ""
        type_str = "TV" if r.media_type == "tv" else "Movie"

        overview = ""
        if r.overview:
            overview = truncate(r.overview, 100).rstrip(".")
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
            overview = truncate(r.overview, 120).rstrip(".")
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
