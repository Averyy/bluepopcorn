"""Shared utilities — logging, phone sanitisation, safe file paths."""

from __future__ import annotations

from pathlib import Path


def mask_phone(phone: str) -> str:
    """Mask phone number to show only last 4 digits: ***1234."""
    return f"***{phone[-4:]}" if len(phone) >= 4 else "***"


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
