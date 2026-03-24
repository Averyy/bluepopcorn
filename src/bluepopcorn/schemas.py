"""All JSON schemas for LLM calls and MCP tool input schemas."""

# ── Call-1 schema (action decision) ──────────────────────────────────

DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["search", "request", "recent", "recommend", "reply"],
        },
        "query": {"type": "string"},
        "tmdb_id": {"type": "integer"},
        "media_type": {"type": "string", "enum": ["movie", "tv"]},
        "message": {"type": "string"},
        "genre": {"type": "string"},
        "keyword": {"type": "string"},
        "year": {"type": "integer"},
        "year_end": {"type": "integer"},
        "similar_to": {"type": "string"},
        "trending": {"type": "boolean"},
        "upcoming": {"type": "boolean"},
        "seasons": {"type": "array", "items": {"type": "integer"}},
        "collection_id": {"type": "integer"},
        "count": {"type": "integer"},
        "page": {"type": "integer"},
    },
    "required": ["action", "message"],
    "additionalProperties": False,
}

# ── Call-2 schema (response after API results) ───────────────────────

RESPOND_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["reply", "request"],
        },
        "tmdb_id": {"type": "integer"},
        "media_type": {"type": "string", "enum": ["movie", "tv"]},
        "message": {"type": "string"},
        "seasons": {"type": "array", "items": {"type": "integer"}},
        "collection_id": {"type": "integer"},
        "multiple_results": {"type": "boolean"},
    },
    "required": ["action", "message"],
    "additionalProperties": False,
}

# ── Compression schemas ──────────────────────────────────────────────

COMPRESSION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "1-3 sentence summary. Always include specific title names and outcomes.",
        },
        "suggested_preferences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genuine repeated patterns to add as preferences (empty if none)",
        },
        "genres": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genres the user showed interest in (e.g. 'horror', 'sci-fi', 'Korean drama')",
        },
        "avoid_genres": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genres the user said they dislike or want to avoid, with reason in brackets if known (e.g. 'reality TV [finds it trashy]', 'romance')",
        },
        "liked_movies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Movies the user requested or expressed interest in, with year (e.g. 'Sinners (2025)')",
        },
        "liked_shows": {
            "type": "array",
            "items": {"type": "string"},
            "description": "TV shows the user requested or expressed interest in, with year (e.g. 'Severance (2022)')",
        },
        "avoid_titles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Titles the user rejected, disliked, or said to avoid, with year and reason in brackets if known (e.g. 'The Monkey (2025) [too campy]', 'Love Is Blind (2020) [hates reality TV]')",
        },
    },
    "required": ["summary", "suggested_preferences", "genres", "avoid_genres", "liked_movies", "liked_shows", "avoid_titles"],
    "additionalProperties": False,
}

ROLLUP_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

# ── XML tag names (structural contract between prompt builder and system prompt) ─

TAG_MEMORY = "memory"
TAG_CONTEXT = "context"

# ── MCP tool input schemas ──────────────────────────────────────────

MCP_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query (title, optionally with year)",
        },
        "media_type": {
            "type": "string",
            "enum": ["movie", "tv"],
            "description": "Filter results by type (optional)",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

MCP_DETAILS_SCHEMA = {
    "type": "object",
    "properties": {
        "tmdb_id": {
            "type": "integer",
            "minimum": 1,
            "description": "TMDB ID of the title",
        },
        "media_type": {
            "type": "string",
            "enum": ["movie", "tv"],
            "description": "Whether this is a movie or TV show",
        },
    },
    "required": ["tmdb_id", "media_type"],
    "additionalProperties": False,
}

MCP_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "tmdb_id": {
            "type": "integer",
            "minimum": 1,
            "description": "TMDB ID of the title to request",
        },
        "media_type": {
            "type": "string",
            "enum": ["movie", "tv"],
            "description": "Whether this is a movie or TV show",
        },
        "seasons": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Specific season numbers to request (TV only, optional — defaults to all)",
        },
    },
    "required": ["tmdb_id", "media_type"],
    "additionalProperties": False,
}

MCP_RECOMMEND_SCHEMA = {
    "type": "object",
    "properties": {
        "genre": {
            "type": "string",
            "description": "Genre name (e.g. 'sci-fi', 'comedy', 'thriller')",
        },
        "keyword": {
            "type": "string",
            "description": "Thematic keyword (e.g. 'time travel', 'robots', 'heist')",
        },
        "similar_to": {
            "type": "string",
            "description": "Title name to find similar content (e.g. 'Inception')",
        },
        "media_type": {
            "type": "string",
            "enum": ["movie", "tv"],
            "description": "Filter by type (optional)",
        },
        "year": {
            "type": "integer",
            "description": "Year or start of year range",
        },
        "year_end": {
            "type": "integer",
            "description": "End of year range (e.g. year=2020, year_end=2029 for '2020s')",
        },
        "trending": {
            "type": "boolean",
            "description": "Show currently trending titles",
        },
        "upcoming": {
            "type": "boolean",
            "description": "Show upcoming releases",
        },
        "count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "Number of results (default 5, max 10)",
        },
        "exclude_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "TMDB IDs to exclude — use for 'show me more' by passing previously shown tmdb_ids",
        },
    },
    "additionalProperties": False,
}

MCP_RECENT_SCHEMA = {
    "type": "object",
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "Number of items per section (default 10)",
        },
        "page": {
            "type": "integer",
            "minimum": 1,
            "description": "Page number for pagination (default 1)",
        },
    },
    "additionalProperties": False,
}
