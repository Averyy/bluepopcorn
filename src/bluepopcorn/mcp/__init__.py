"""BluePopcorn MCP server package."""

from __future__ import annotations

import sys
from datetime import UTC, datetime


def _log(msg: str) -> None:
    """Log with timestamp to stderr."""
    print(f"[{datetime.now(UTC).isoformat()}] {msg}", file=sys.stderr)
