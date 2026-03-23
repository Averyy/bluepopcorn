"""Logging helpers. Keep this module minimal — only log-related utilities."""

from __future__ import annotations


def mask_phone(phone: str) -> str:
    """Mask phone number to show only last 4 digits: ***1234."""
    return f"***{phone[-4:]}" if len(phone) >= 4 else "***"
