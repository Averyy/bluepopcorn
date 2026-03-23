"""Track which phone number requested which media for targeted notifications."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class RequestTracker:
    """Maps media items to the phone numbers that requested them.

    Persists to a JSON file for survival across restarts.
    Key format: "{media_type}:{tmdb_id}" → list of phone numbers.
    """

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / "request_map.json"
        self._data: dict[str, list[str]] = self._load()
        self._lock = asyncio.Lock()

    def _load(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self._path.read_text())
            if not isinstance(data, dict):
                log.warning("request_map.json has invalid structure, resetting")
                return {}
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._data))
            tmp.rename(self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    async def record(self, media_type: str, tmdb_id: int, phone: str) -> None:
        """Record that a phone number requested a media item."""
        async with self._lock:
            key = f"{media_type}:{tmdb_id}"
            phones = self._data.setdefault(key, [])
            if phone not in phones:
                phones.append(phone)
                self._save()
                log.debug("Tracked request: %s → %s", key, phone[-4:])

    async def lookup(self, media_type: str, tmdb_id: int) -> list[str]:
        """Return phone numbers that requested this media item."""
        async with self._lock:
            key = f"{media_type}:{tmdb_id}"
            return list(self._data.get(key, []))

    async def remove(self, media_type: str, tmdb_id: int) -> None:
        """Remove tracking entry after media becomes available or fails."""
        async with self._lock:
            key = f"{media_type}:{tmdb_id}"
            if key in self._data:
                del self._data[key]
                self._save()
                log.debug("Removed tracking: %s", key)
