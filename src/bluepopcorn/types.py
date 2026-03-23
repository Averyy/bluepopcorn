from __future__ import annotations

import enum
from dataclasses import dataclass


class Action(str, enum.Enum):
    SEARCH = "search"
    REQUEST = "request"
    RECENT = "recent"
    RECOMMEND = "recommend"
    REPLY = "reply"


class MediaStatus(enum.IntEnum):
    NOT_TRACKED = 0  # absent mediaInfo — never requested
    UNKNOWN = 1
    PENDING = 2
    PROCESSING = 3
    PARTIALLY_AVAILABLE = 4
    AVAILABLE = 5
    BLOCKLISTED = 6  # Seerr 3.0+
    DELETED = 7  # Seerr 3.0+


class RequestStatus(enum.IntEnum):
    PENDING_APPROVAL = 1
    APPROVED = 2
    DECLINED = 3
    FAILED = 4  # Seerr 3.0+
    COMPLETED = 5  # Seerr 3.0+


@dataclass
class LLMDecision:
    action: Action
    message: str
    query: str | None = None
    tmdb_id: int | None = None
    media_type: str | None = None  # "movie" or "tv"
    # Structured recommend fields (LLM specifies these instead of dumping into query)
    genre: str | None = None  # genre name, e.g. "sci-fi", "comedy"
    keyword: str | None = None  # thematic keyword, e.g. "robots", "time travel"
    year: int | None = None  # year or start of range
    year_end: int | None = None  # end of year range (e.g. year=2020, year_end=2029 for "2020s")
    similar_to: str | None = None  # title name for "similar to X"
    trending: bool = False  # whether to show trending content
    count: int | None = None  # number of results to return (default 5)
    page: int | None = None  # pagination for recent/server state
    multiple_results: bool = False  # LLM presenting multiple numbered options vs single focus

    @classmethod
    def from_dict(cls, data: dict) -> LLMDecision:
        return cls(
            action=Action(data["action"]),
            message=data.get("message", ""),
            query=data.get("query"),
            tmdb_id=data.get("tmdb_id"),
            media_type=data.get("media_type"),
            genre=data.get("genre"),
            keyword=data.get("keyword"),
            year=data.get("year"),
            year_end=data.get("year_end"),
            similar_to=data.get("similar_to"),
            trending=data.get("trending", False),
            count=data.get("count"),
            page=data.get("page"),
            multiple_results=data.get("multiple_results", False),
        )


@dataclass
class IncomingMessage:
    rowid: int
    sender: str  # phone number or email
    text: str
    timestamp: float  # unix timestamp


@dataclass
class SearchResult:
    tmdb_id: int
    title: str
    year: int | None
    media_type: str  # "movie" or "tv"
    overview: str
    status: MediaStatus
    poster_path: str | None = None
    rating: float | None = None  # TMDB vote average (0-10)
    trailer_url: str | None = None  # YouTube trailer URL
    rt_rating: str | None = None  # Rotten Tomatoes score (e.g. "97%")
    imdb_rating: str | None = None  # IMDB score (e.g. "8.7")
    download_progress: str | None = None  # e.g. "51%" when actively downloading
    next_air_date: str | None = None  # e.g. "S2E5 airs 2026-03-20" or "2026-07-04"
    from_person: bool = False  # True if result came from person search (actor/director credits)


@dataclass
class HistoryEntry:
    role: str  # "user", "assistant", or "context"
    content: str
    timestamp: float


# ── Status labels (domain mapping: enum → human-readable for LLM context) ─

STATUS_LABELS: dict[MediaStatus, str] = {
    MediaStatus.AVAILABLE: "available in library",
    MediaStatus.PARTIALLY_AVAILABLE: "partially available in library",
    MediaStatus.PROCESSING: "requested: waiting for release",
    MediaStatus.PENDING: "requested: waiting for admin approval",
    MediaStatus.UNKNOWN: "not in the library",
    MediaStatus.NOT_TRACKED: "not in the library",
    MediaStatus.BLOCKLISTED: "blocked/unable to download",
    MediaStatus.DELETED: "not in the library",
}


def status_label_for(
    status: MediaStatus, download_progress: str | None = None
) -> str:
    """Human-readable status label, with download progress override."""
    label = STATUS_LABELS.get(status, "not in the library")
    if status == MediaStatus.PROCESSING and download_progress:
        label = f"currently downloading ({download_progress})"
    return label
