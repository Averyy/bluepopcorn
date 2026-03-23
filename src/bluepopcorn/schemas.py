"""All JSON schemas for LLM calls."""

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
