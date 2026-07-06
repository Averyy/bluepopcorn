"""Shared utilities — logging, phone sanitisation, safe file paths."""

from __future__ import annotations

import re
from pathlib import Path

# Trailing "(YYYY)" — "Title (2013)" and "Title" are the same search for
# dedup purposes (the request fallback strips the year before querying).
_TRAILING_YEAR_RE = re.compile(r"\s*\(\d{4}\)\s*$")


def mask_phone(phone: str) -> str:
    """Mask phone number to show only last 4 digits: ***1234."""
    return f"***{phone[-4:]}" if len(phone) >= 4 else "***"


def normalize_search_query(query: str, media_type: str | None = None) -> str:
    """Normalize a search query for same-turn dedup.

    Strips a trailing "(YYYY)", collapses whitespace, casefolds. When
    ``media_type`` is given it is folded into the key so a legitimate
    movie-vs-tv refinement of the same title is not treated as a repeat.
    """
    stripped = _TRAILING_YEAR_RE.sub("", query or "").strip() or (query or "")
    norm = " ".join(stripped.split()).casefold()
    return f"{media_type}:{norm}" if media_type else norm


def atomic_tmp_path(path: Path) -> Path:
    """Sibling temp path for atomic writes.

    NOT ``with_suffix(".tmp")`` — that strips everything after the last
    dot, so email-derived filenames ("digest_a@x.com" / "digest_a@x.org")
    would collide on the same temp file.
    """
    return path.parent / (path.name + ".tmp")


def safe_sender_filename(sender: str) -> str:
    """Sanitise a sender (phone number) into a safe filename component."""
    return sender.lstrip("+").replace("/", "_")


def safe_data_path(data_dir: Path, prefix: str, sender: str) -> Path:
    """Build a data file path for a sender with path-traversal guard.

    Returns ``data_dir / "{prefix}_{safe_sender}"``.
    Raises ``ValueError`` if the resolved path escapes *data_dir*.
    """
    path = data_dir / f"{prefix}_{safe_sender_filename(sender)}"
    if not path.resolve().is_relative_to(data_dir.resolve()):
        raise ValueError(f"Invalid sender for {prefix} path: {sender!r}")
    return path
