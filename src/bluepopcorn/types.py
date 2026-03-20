from __future__ import annotations

import enum
from dataclasses import dataclass


class Action(str, enum.Enum):
    SEARCH = "search"
    REQUEST = "request"
    RECENT = "recent"
    RECOMMEND = "recommend"
    REMEMBER = "remember"
    FORGET = "forget"
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
    fact: str | None = None  # for remember/forget actions
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
            fact=data.get("fact"),
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

    @property
    def status_label(self) -> str:
        labels = {
            MediaStatus.AVAILABLE: "available",
            MediaStatus.PARTIALLY_AVAILABLE: "in your library",
            MediaStatus.PROCESSING: "requested",
            MediaStatus.PENDING: "requested, pending approval",
            MediaStatus.UNKNOWN: "unknown",
            MediaStatus.NOT_TRACKED: "not requested",
            MediaStatus.BLOCKLISTED: "blocklisted",
            MediaStatus.DELETED: "deleted",
        }
        label = labels.get(self.status, "unknown")
        if self.status == MediaStatus.PROCESSING and self.download_progress:
            label = f"downloading ({self.download_progress})"
        return label


@dataclass
class HistoryEntry:
    role: str  # "user", "assistant", or "context"
    content: str
    timestamp: float


# JSON schema for claude -p --json-schema
LLM_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "search", "request",
                "recent", "recommend", "remember", "forget", "reply",
            ],
        },
        "query": {"type": "string"},
        "tmdb_id": {"type": "integer"},
        "media_type": {"type": "string", "enum": ["movie", "tv"]},
        "message": {"type": "string"},
        "fact": {"type": "string"},
        "genre": {"type": "string"},
        "keyword": {"type": "string"},
        "year": {"type": "integer"},
        "year_end": {"type": "integer"},
        "similar_to": {"type": "string"},
        "trending": {"type": "boolean"},
        "count": {"type": "integer"},
        "page": {"type": "integer"},
    },
    "required": ["action", "message"],
    "additionalProperties": False,
}

# Restricted schema for _llm_respond — only reply or request (follow-up "add it")
LLM_RESPOND_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["reply", "request"],
        },
        "tmdb_id": {"type": "integer"},
        "media_type": {"type": "string", "enum": ["movie", "tv"]},
        "message": {"type": "string"},
        "multiple_results": {"type": "boolean"},
    },
    "required": ["action", "message"],
    "additionalProperties": False,
}
