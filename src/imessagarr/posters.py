from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from .config import Settings
from .types import SearchResult

log = logging.getLogger(__name__)

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


class PosterHandler:
    def __init__(self, settings: Settings) -> None:
        self.poster_dir = settings.resolve_path(settings.poster_dir)
        self.poster_dir.mkdir(parents=True, exist_ok=True)
        self.client = httpx.AsyncClient(timeout=15)

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

    async def create_collage(self, results: list[SearchResult]) -> Path | None:
        """Create a numbered collage of posters for disambiguation.

        Downloads posters, resizes to same height, stitches side-by-side,
        and adds number overlays.
        """
        if not results:
            return None

        # Download all posters concurrently
        async def _download_or_none(result: SearchResult) -> Path | None:
            if result.poster_path:
                return await self.download_poster(result.poster_path)
            return None

        poster_paths: list[Path | None] = await asyncio.gather(
            *(_download_or_none(r) for r in results)
        )

        # Filter to posters that downloaded successfully (sequential numbering)
        valid: list[Path] = [p for p in poster_paths if p is not None]
        if not valid:
            return None

        if len(valid) == 1:
            return valid[0]

        # Open images and resize to same height
        target_height = 750
        images: list[tuple[int, Image.Image]] = []
        opened_images: list[Image.Image] = []
        for idx, path in enumerate(valid):
            raw_img = Image.open(path)
            opened_images.append(raw_img)
            img = raw_img.convert("RGB")
            if img is not raw_img:
                opened_images.append(img)
            ratio = target_height / img.height
            new_width = int(img.width * ratio)
            resized = img.resize((new_width, target_height), Image.LANCZOS)
            opened_images.append(resized)
            images.append((idx, resized))

        try:
            # Stitch side-by-side with small gap
            gap = 10
            total_width = sum(img.width for _, img in images) + gap * (len(images) - 1)
            collage = Image.new("RGB", (total_width, target_height), (30, 30, 30))

            x_offset = 0
            draw = ImageDraw.Draw(collage)

            # Try to load a font, fall back to default
            try:
                font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 48)
            except (OSError, IOError):
                font = ImageFont.load_default()

            for idx, img in images:
                collage.paste(img, (x_offset, 0))

                # Draw number overlay
                number = str(idx + 1)
                # Black circle background
                circle_x = x_offset + 20
                circle_y = 20
                circle_r = 30
                draw.ellipse(
                    [circle_x, circle_y, circle_x + circle_r * 2, circle_y + circle_r * 2],
                    fill=(0, 0, 0),
                )
                # White number
                bbox = draw.textbbox((0, 0), number, font=font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
                draw.text(
                    (circle_x + circle_r - text_w // 2, circle_y + circle_r - text_h // 2),
                    number,
                    fill="white",
                    font=font,
                )

                x_offset += img.width + gap

            # Save collage
            collage_path = self.poster_dir / f"collage_{int(time.time())}.jpg"
            collage.save(collage_path, "JPEG", quality=85)
            log.info("Created collage: %s", collage_path)

            # Close collage image
            collage.close()

            # Clean up old collage files (older than 24 hours)
            self._cleanup_old_collages()

            return collage_path
        finally:
            # Close all opened images
            for img in opened_images:
                img.close()

    def _cleanup_old_collages(self, max_age_hours: int = 24) -> None:
        """Delete collage files older than max_age_hours."""
        cutoff = time.time() - max_age_hours * 3600
        for path in self.poster_dir.glob("collage_*.jpg"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    log.debug("Cleaned up old collage: %s", path)
            except OSError as e:
                log.debug("Failed to clean up collage %s: %s", path, e)

    async def close(self) -> None:
        await self.client.aclose()
