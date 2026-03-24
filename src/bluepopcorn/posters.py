from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .seerr import TMDB_IMAGE_BASE
from .types import SearchResult

log = logging.getLogger(__name__)


class PosterHandler:
    def __init__(self, settings: Settings) -> None:
        self.poster_dir = settings.resolve_path(settings.poster_dir)
        self.poster_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.AsyncClient(timeout=settings.http_timeout)

    async def download_poster(self, poster_path: str) -> Path | None:
        """Download a poster from TMDB, using cache."""
        if not poster_path:
            return None

        # Cache by poster path (e.g., /abc123.jpg -> abc123.jpg)
        filename = poster_path.lstrip("/")
        local_path = self.poster_dir / filename
        # Prevent path traversal
        if not local_path.resolve().is_relative_to(self.poster_dir.resolve()):
            log.error("Path traversal attempt blocked: %s", poster_path)
            return None
        if local_path.exists():
            return local_path

        url = f"{TMDB_IMAGE_BASE}{poster_path}"
        try:
            resp = await self.client.get(url)
            resp.raise_for_status()
            local_path.write_bytes(resp.content)
            log.info("Downloaded poster: %s", filename)
            return local_path
        except httpx.HTTPError as e:
            log.error("Failed to download poster %s: %s", url, e)
            return None

    async def get_single_poster(self, result: SearchResult) -> Path | None:
        """Get a single poster for a search result."""
        if not result.poster_path:
            return None
        return await self.download_poster(result.poster_path)

    async def download_all(self, results: list[SearchResult]) -> list[tuple[int, Path]]:
        """Download posters for all results concurrently.

        Returns list of (1-indexed position, local path) for successful downloads.
        """
        if not results:
            return []

        poster_paths: list[Path | None] = await asyncio.gather(
            *(self.download_poster(r.poster_path) for r in results)
        )

        return [(i + 1, p) for i, p in enumerate(poster_paths) if p is not None]

    def number_posters(self, raw: list[tuple[int, Path]]) -> list[Path]:
        """Add number overlays to pre-downloaded posters. Returns numbered paths."""
        numbered: list[Path] = []
        for idx, path in raw:
            out = self._add_number_overlay(path, idx)
            if out:
                numbered.append(out)
        self._cleanup_temp_files()
        return numbered

    def _add_number_overlay(self, poster_path: Path, number: int) -> Path | None:
        """Copy a poster with a numbered circle overlay in the top-left corner."""
        try:
            img = Image.open(poster_path).convert("RGB")
            draw = ImageDraw.Draw(img)

            circle_r = max(20, img.width // 10)
            font_size = int(circle_r * 1.6)
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
            except (OSError, IOError):
                font = ImageFont.load_default()

            cx, cy = 15, 15
            draw.ellipse(
                [cx, cy, cx + circle_r * 2, cy + circle_r * 2],
                fill=(0, 0, 0),
            )
            text = str(number)
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(
                (cx + circle_r - tw // 2, cy + circle_r - th // 2),
                text, fill="white", font=font,
            )

            out_path = self.poster_dir / f"numbered_{number}_{int(time.time())}.jpg"
            img.save(out_path, "JPEG", quality=85)
            img.close()
            return out_path
        except Exception as e:
            log.error("Failed to number poster %s: %s", poster_path, e)
            return None

    def _cleanup_temp_files(self, max_age_hours: int = 24) -> None:
        """Delete temporary poster files (collages, numbered) older than max_age_hours."""
        cutoff = time.time() - max_age_hours * 3600
        for pattern in ("collage_*.jpg", "numbered_*.jpg"):
            for path in self.poster_dir.glob(pattern):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        log.debug("Cleaned up: %s", path)
                except OSError as e:
                    log.debug("Failed to clean up %s: %s", path, e)

    async def close(self) -> None:
        await self.client.aclose()
