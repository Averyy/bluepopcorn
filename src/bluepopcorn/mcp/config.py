"""MCP server configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    """MCP server configuration."""

    seerr_url: str
    seerr_api_key: str
    http_port: int = 8080
    http_host: str = "127.0.0.1"
    http_timeout: int = 15
    min_rating_votes: int = 50
    api_key: str | None = None  # MCP_API_KEY for Bearer auth


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key, str(default))
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from None


def load_config() -> Config:
    """Load configuration from environment variables."""
    load_dotenv(override=False)

    seerr_url = os.environ.get("SEERR_URL", "")
    seerr_api_key = os.environ.get("SEERR_API_KEY", "")

    missing = []
    if not seerr_url:
        missing.append("SEERR_URL")
    if not seerr_api_key:
        missing.append("SEERR_API_KEY")
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    http_port = _int("HTTP_PORT", 8080)
    if not (1 <= http_port <= 65535):
        raise ValueError(f"HTTP_PORT must be between 1 and 65535, got {http_port}")

    return Config(
        seerr_url=seerr_url,
        seerr_api_key=seerr_api_key,
        http_port=http_port,
        http_host=os.environ.get("HTTP_HOST", "127.0.0.1"),
        http_timeout=_int("HTTP_TIMEOUT", 15),
        min_rating_votes=_int("MIN_RATING_VOTES", 50),
        api_key=os.environ.get("MCP_API_KEY"),
    )
