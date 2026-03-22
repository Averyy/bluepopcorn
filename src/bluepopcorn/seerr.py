from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from typing import Any

import httpx

from .config import Settings
from .types import MediaStatus, SearchResult

log = logging.getLogger(__name__)

ALLOWED_LANGUAGES = {"en", "es", "ja", "ko"}
MIN_YEAR = 2000


def _title_match_rank(item: dict, query_lower: str) -> int:
    """Rank how well an item's title matches the search query. 0=exact, 1=none.

    Checks both localized and original titles so non-English titles
    (e.g. "La oficina") aren't buried by popular English results.
    """
    for key in ("title", "name", "originalTitle", "originalName"):
        val = item.get(key, "")
        if val and val.lower() == query_lower:
            return 0
    return 1

# Common shorthands users type → list of canonical names to try (first match wins)
GENRE_SHORTHANDS: dict[str, list[str]] = {
    "sci-fi": ["science fiction"],
    "scifi": ["science fiction", "sci-fi"],
}


def seerr_title(data: dict, default: str = "Unknown") -> str:
    """Extract title from a Seerr detail/media dict (movies use 'title', TV uses 'name')."""
    return data.get("title") or data.get("name") or default


def parse_download_progress(media_info: dict) -> str | None:
    """Extract download progress string from a Seerr mediaInfo dict.

    Returns e.g. "51%, ETA 00:01:23" or "51%" or None if no download data.
    """
    dl_status = media_info.get("downloadStatus") or []
    if not dl_status or not isinstance(dl_status, list):
        return None
    dl = dl_status[0]
    size = dl.get("size", 0)
    size_left = dl.get("sizeLeft", dl.get("sizeleft", 0))
    if not size or size <= 0:
        return None
    pct = round((size - size_left) / size * 100)
    time_left = dl.get("timeleft") or dl.get("timeLeft") or ""
    if time_left:
        return f"{pct}%, ETA {time_left}"
    return f"{pct}%"


# --- Custom Exceptions ---


class SeerrError(Exception):
    """Base exception for Seerr API errors."""


class SeerrConnectionError(SeerrError):
    """HTTP connection failed or timed out."""


class SeerrSearchError(SeerrError):
    """Search query returned an error (e.g. 400 bad query)."""


class SeerrClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.seerr_url.rstrip("/")
        self.client = httpx.AsyncClient(
            timeout=settings.http_timeout,
            headers={"X-Api-Key": settings.seerr_api_key},
        )
        # Dynamic genre maps, loaded lazily
        self._genre_map_movie: dict[str, int] | None = None
        self._genre_map_tv: dict[str, int] | None = None
        # Detail cache: (media_type, tmdb_id) -> {title, year, overview, poster_path, ts}
        # TMDB metadata never changes — 2-week TTL. Status is NOT cached (comes from list endpoints).
        self._detail_cache: dict[tuple[str, int], dict] = {}
        self._detail_cache_ttl = 14 * 86400  # 2 weeks

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make an API-key-authenticated request.

        Uses %20 encoding for query params instead of httpx's default +.
        Seerr 3.x rejects + as space encoding.
        """
        # Build URL with %20 encoding for params
        url = f"{self.base_url}{path}"
        params = kwargs.pop("params", None)
        if params:
            qs = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            url = f"{url}?{qs}"

        try:
            resp = await self.client.request(method, url, **kwargs)
        except httpx.ConnectError as e:
            log.error("Connection failed: %s %s — %s", method, path, e)
            raise SeerrConnectionError(f"Connect failed ({method} {path}): {e}") from e
        except httpx.TimeoutException as e:
            log.error("Request timeout: %s %s after %ss — %s", method, path, self.client.timeout.connect, e)
            raise SeerrConnectionError(f"Timeout ({method} {path}): {e}") from e

        if resp.status_code >= 400 and resp.status_code != 404:
            log.error("HTTP %d on %s %s: %s", resp.status_code, method, path, resp.text[:200])
        resp.raise_for_status()
        return resp

    # --- Genre Loading ---

    async def _load_genres(self) -> None:
        """Load genre mappings from Seerr API."""
        try:
            movie_resp, tv_resp = await asyncio.gather(
                self._request("GET", "/api/v1/genres/movie", params={"language": "en"}),
                self._request("GET", "/api/v1/genres/tv", params={"language": "en"}),
            )
            self._genre_map_movie = self._build_genre_map(movie_resp.json())
            self._genre_map_tv = self._build_genre_map(tv_resp.json())
            log.info(
                "Loaded %d movie genres, %d TV genres",
                len(self._genre_map_movie),
                len(self._genre_map_tv),
            )
        except Exception as e:
            log.warning("Failed to load genres from API, using empty maps: %s", e)
            self._genre_map_movie = {}
            self._genre_map_tv = {}

    @staticmethod
    def _build_genre_map(genres: list[dict]) -> dict[str, int]:
        """Build a name-to-id lookup from genre list.

        Entries are keyed by full lowercase name. Also adds substring keys
        so "sci-fi" matches "Sci-Fi & Fantasy", "action" matches
        "Action & Adventure", etc. — no hardcoded alias table needed.
        """
        mapping: dict[str, int] = {}
        for g in genres:
            name = g.get("name", "")
            gid = g.get("id")
            if name and gid:
                key = name.lower()
                mapping[key] = gid
                # Split compound names like "Sci-Fi & Fantasy" → "sci-fi", "fantasy"
                if " & " in key:
                    for part in key.split(" & "):
                        part = part.strip()
                        if part and part not in mapping:
                            mapping[part] = gid
        # Apply shorthands (e.g. "scifi" → try "science fiction", then "sci-fi")
        for shorthand, candidates in GENRE_SHORTHANDS.items():
            if shorthand not in mapping:
                for candidate in candidates:
                    if candidate in mapping:
                        mapping[shorthand] = mapping[candidate]
                        break
        return mapping

    async def get_genre_map(self, media_type: str) -> dict[str, int]:
        """Get genre name→id map for movie or tv, loading lazily."""
        if self._genre_map_movie is None or self._genre_map_tv is None:
            await self._load_genres()
        if media_type == "movie":
            return self._genre_map_movie or {}
        return self._genre_map_tv or {}

    # --- Search ---

    async def search(self, query: str, media_type: str | None = None) -> list[SearchResult]:
        """Search for movies and TV shows.

        If media_type is provided ("movie" or "tv"), use it for post-filtering
        instead of keyword parsing. Keyword parsing is the fallback.
        """
        # Explicit media_type from LLM decision takes priority
        if media_type:
            want_movie = media_type == "movie"
            want_tv = media_type == "tv"
        else:
            # Extract hints from the original query before cleaning
            query_lower = query.lower()
            want_movie = any(w in query_lower for w in ("movie", "film"))
            want_tv = any(w in query_lower for w in ("tv", "show", "series"))
        # Only strip trailing years — preserves "2001: A Space Odyssey", "1917", "Blade Runner 2049"
        year_match = re.search(r"\s+((?:19|20)\d{2})\s*$", query)
        want_year = int(year_match.group(1)) if year_match else None

        # Strip filler words and the trailing year from the search query.
        # TMDB treats years as title text ("Alexander 2004" matches a concert film).
        # We search without the year and use it as a post-filter instead.
        # If no results match the year (it's part of the title, e.g. "Blade Runner 2049"),
        # we retry with the full query.
        cleaned = re.sub(r"\b(movie|film|tv|show|series)\b", "", query, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        full_query = cleaned or query  # query with year intact (for retry)
        search_query = full_query
        if want_year:
            stripped = re.sub(r"\s+(19|20)\d{2}\s*$", "", full_query).strip()
            if stripped:  # don't strip if query IS the year (e.g. "2012")
                search_query = stripped

        log.info("Seerr search: %s", search_query)

        resp = await self._try_search(search_query)
        data = resp.json()
        # Sort by title match first (exact matches win), then popularity.
        # Prevents popular-but-wrong results (e.g. The Office) from burying
        # exact matches (e.g. La oficina) that the API returned as relevant.
        raw_results = data.get("results", [])
        q_lower = search_query.lower()
        raw_results.sort(key=lambda x: (
            _title_match_rank(x, q_lower),
            -x.get("popularity", 0),
        ))
        results = self._parse_results(raw_results)

        # If too few results from page 1, try page 2
        if len(results) < 3 and data.get("totalPages", 1) > 1:
            try:
                resp2 = await self._request(
                    "GET", "/api/v1/search",
                    params={"query": search_query, "page": 2, "language": "en"},
                )
                data2 = resp2.json()
                extra = self._parse_results(data2.get("results", []), take=5 - len(results))
                results.extend(extra)
            except Exception as e:
                log.debug("Search page 2 fallback failed: %s", e)

        # Post-filter by media type and year if the user specified them
        if want_movie or want_tv or want_year:
            filtered = results
            if want_movie and not want_tv:
                filtered = [r for r in filtered if r.media_type == "movie"]
            elif want_tv and not want_movie:
                filtered = [r for r in filtered if r.media_type == "tv"]
            if want_year:
                filtered = [r for r in filtered if r.year == want_year]
            if filtered:
                results = filtered
            elif want_year and search_query != full_query:
                # Year filter found nothing — year may be part of the title
                # (e.g. "Blade Runner 2049"). Retry with original query.
                log.info("Year filter empty, retrying with full query: %s", full_query)
                resp = await self._try_search(full_query)
                data = resp.json()
                raw_retry = data.get("results", [])
                q_full = full_query.lower()
                raw_retry.sort(key=lambda x: (
                    _title_match_rank(x, q_full),
                    -x.get("popularity", 0),
                ))
                results = self._parse_results(raw_retry)
                if want_movie and not want_tv:
                    type_filtered = [r for r in results if r.media_type == "movie"]
                    if type_filtered:
                        results = type_filtered
                elif want_tv and not want_movie:
                    type_filtered = [r for r in results if r.media_type == "tv"]
                    if type_filtered:
                        results = type_filtered

        log.info("Seerr search returned %d results", len(results))
        return results

    async def _try_search(self, query: str) -> httpx.Response:
        """Try search with fallback chain on 400 errors."""
        try:
            return await self._request(
                "GET", "/api/v1/search",
                params={"query": query, "language": "en"},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 400:
                raise SeerrSearchError(f"Search failed: {e}") from e
            log.warning("Search returned 400, trying fallback queries")
        except SeerrConnectionError:
            raise

        # Fallback chain for 400 errors: no special chars → first 3 words → first 2 words
        words = query.split()
        # Try without special characters
        no_special = re.sub(r"[^\w\s]", "", query).strip()
        if no_special and no_special != query:
            try:
                return await self._request(
                    "GET", "/api/v1/search",
                    params={"query": no_special, "language": "en"},
                )
            except httpx.HTTPStatusError:
                pass

        for length in (3, 2):
            if len(words) > length:
                short = " ".join(words[:length])
                log.info("Retrying with shorter query: %s", short)
                try:
                    return await self._request(
                        "GET", "/api/v1/search",
                        params={"query": short, "language": "en"},
                    )
                except httpx.HTTPStatusError:
                    continue

        raise SeerrSearchError(f"All search attempts failed for: {query}")

    async def search_keywords(self, query: str) -> list[int]:
        """Search TMDB keywords and return matching keyword IDs.

        Used by recommend handler to discover by thematic keywords
        (e.g. "robots" -> keyword IDs -> discover by keywords).
        """
        if not query.strip():
            return []
        try:
            resp = await self._request(
                "GET", "/api/v1/search/keyword",
                params={"query": query},
            )
            data = resp.json()
            results = data.get("results", [])
            ids = [r["id"] for r in results[:5] if r.get("id")]
            if ids:
                log.info("Keyword search '%s': %d IDs", query, len(ids))
            return ids
        except Exception as e:
            log.debug("Keyword search failed for '%s': %s", query, e)
            return []

    async def request_media(
        self, media_type: str, tmdb_id: int, *, seasons: list[int] | None = None
    ) -> dict:
        """Request a movie or TV show on Seerr.

        For TV, pass pre-fetched ``seasons`` to avoid a redundant detail call.
        If omitted, seasons are fetched automatically.
        """
        log.info("Seerr request: %s tmdb:%d", media_type, tmdb_id)
        payload: dict[str, Any] = {"mediaType": media_type, "mediaId": tmdb_id}
        if media_type == "tv":
            # Explicitly pass season numbers — omitting seasons crashes some shows
            if not seasons:
                seasons = await self._get_season_numbers(tmdb_id)
            if not seasons:
                raise SeerrError(f"Could not fetch season info for tv/{tmdb_id}")
            payload["seasons"] = seasons
        resp = await self._request("POST", "/api/v1/request", json=payload)
        result = resp.json()
        log.info("Seerr request successful: %s", result.get("id"))
        return result

    async def _get_season_numbers(self, tmdb_id: int) -> list[int]:
        """Fetch season numbers for a TV show, excluding specials (season 0)."""
        try:
            resp = await self._request("GET", f"/api/v1/tv/{tmdb_id}")
            data = resp.json()
            return self.extract_season_numbers(data)
        except Exception as e:
            log.warning("Failed to fetch seasons for tv/%d: %s", tmdb_id, e)
            return []

    @staticmethod
    def extract_season_numbers(detail: dict) -> list[int]:
        """Extract season numbers from a TV detail dict, excluding specials."""
        return [
            s["seasonNumber"]
            for s in detail.get("seasons", [])
            if s.get("seasonNumber", 0) > 0
        ]

    async def get_media_status(self, media_type: str, tmdb_id: int) -> dict | None:
        """Get current status of a media item. Returns detail dict or None."""
        try:
            resp = await self._request("GET", f"/api/v1/{media_type}/{tmdb_id}")
            return resp.json()
        except Exception as e:
            log.debug("Media status check failed for %s/%d: %s", media_type, tmdb_id, e)
            return None

    async def get_request_count(self) -> dict:
        """Get request counts (total, pending, approved, processing, available, completed)."""
        resp = await self._request("GET", "/api/v1/request/count")
        return resp.json()

    async def get_pending(self) -> list[dict]:
        """Get pending media requests."""
        log.info("Fetching pending requests")
        resp = await self._request(
            "GET", "/api/v1/request", params={"filter": "pending"}
        )
        data = resp.json()
        return data.get("results", [])

    async def get_processing(self) -> list[dict]:
        """Get approved/processing media requests (downloading)."""
        log.info("Fetching processing requests")
        resp = await self._request(
            "GET", "/api/v1/request", params={"filter": "processing"}
        )
        data = resp.json()
        return data.get("results", [])

    async def get_ratings(self, media_type: str, tmdb_id: int) -> dict[str, str | None]:
        """Get Rotten Tomatoes and IMDB ratings for a movie or TV show.

        Returns dict with keys: rt, rt_audience, rt_freshness, rt_audience_rating,
        imdb, imdb_votes. Values are formatted strings or None if unavailable.
        Movies use /ratingscombined (RT + IMDB), TV uses /ratings (RT only).
        """
        try:
            endpoint = "ratingscombined" if media_type == "movie" else "ratings"
            resp = await self._request(
                "GET", f"/api/v1/{media_type}/{tmdb_id}/{endpoint}"
            )
            data = resp.json()
            log.debug("Raw ratings for %s/%d: %s", media_type, tmdb_id, data)

            ratings: dict[str, str | None] = {
                "rt": None, "rt_audience": None, "rt_freshness": None,
                "rt_audience_rating": None, "imdb": None, "imdb_votes": None,
            }

            # Parse RT rating
            rt_data = data.get("rt")
            if isinstance(rt_data, dict):
                critics = rt_data.get("criticsScore")
                if critics is not None:
                    ratings["rt"] = f"{int(critics)}%"
                audience = rt_data.get("audienceScore")
                if audience is not None:
                    ratings["rt_audience"] = f"{int(audience)}%"
                freshness = rt_data.get("criticsRating")
                if freshness:
                    ratings["rt_freshness"] = freshness
                aud_rating = rt_data.get("audienceRating")
                if aud_rating:
                    ratings["rt_audience_rating"] = aud_rating

            # Parse IMDB rating
            imdb_data = data.get("imdb")
            if isinstance(imdb_data, dict):
                imdb_score = imdb_data.get("criticsScore")
                if imdb_score is not None:
                    ratings["imdb"] = str(imdb_score)
                vote_count = imdb_data.get("criticsScoreCount")
                if vote_count is not None:
                    ratings["imdb_votes"] = str(vote_count)

            return ratings
        except Exception as e:
            log.debug("Ratings lookup failed for %s/%d: %s", media_type, tmdb_id, e)
            return {}

    async def get_detail_extras(
        self, media_type: str, tmdb_id: int
    ) -> dict[str, str | None]:
        """Fetch trailer, air date, and download progress from a single detail call.

        Returns {"trailer": ..., "air_date": ..., "download_progress": ...}
        with None for missing values.
        """
        try:
            resp = await self._request("GET", f"/api/v1/{media_type}/{tmdb_id}")
            data = resp.json()
            media_info = data.get("mediaInfo") or {}
            return {
                "trailer": self._extract_trailer(data),
                "air_date": self._extract_air_date(data, media_type),
                "download_progress": parse_download_progress(media_info),
            }
        except Exception as e:
            log.debug("Detail extras failed for %s/%d: %s", media_type, tmdb_id, e)
            return {"trailer": None, "air_date": None, "download_progress": None}

    @staticmethod
    def _extract_trailer(data: dict) -> str | None:
        for video in data.get("relatedVideos", []):
            if video.get("site") == "YouTube" and video.get("type") in ("Trailer", "Teaser"):
                url = video.get("url")
                if url:
                    return url
                key = video.get("key")
                if key:
                    return f"https://youtu.be/{key}"
        return None

    @staticmethod
    def _extract_air_date(data: dict, media_type: str) -> str | None:
        """Extract air/release date from detail data.

        Returns a human-readable string, e.g.:
          TV airing:  "S2E5 airs 2026-03-20"
          TV ended:   "Ended - last ep S3E10 aired 2025-05-10"
          TV canceled:"Canceled"
          Movie:      "2026-07-04"
        Returns None if no date is available.
        """
        if media_type == "tv":
            next_ep = data.get("nextEpisodeToAir")
            if next_ep and next_ep.get("airDate"):
                season = next_ep.get("seasonNumber", "?")
                ep = next_ep.get("episodeNumber", "?")
                return f"S{season}E{ep} airs {next_ep['airDate']}"
            next_date = data.get("nextAirDate")
            if next_date:
                return f"Next episode airs {next_date}"
            status = data.get("status", "")
            last_ep = data.get("lastEpisodeToAir")
            if status in ("Ended", "Canceled", "Cancelled"):
                if last_ep and last_ep.get("airDate"):
                    s = last_ep.get("seasonNumber", "?")
                    e = last_ep.get("episodeNumber", "?")
                    return f"{status} - last ep S{s}E{e} aired {last_ep['airDate']}"
                return status
            return None
        else:
            release = data.get("releaseDate", "")
            return release or None

    async def _resolve_detail(self, media_type: str, tmdb_id: int) -> dict:
        """Resolve title/year/overview/poster for a media item, using the 2-week detail cache."""
        cache_key = (media_type, tmdb_id)
        cached = self._detail_cache.get(cache_key)
        if cached and time.time() - cached["ts"] < self._detail_cache_ttl:
            return cached

        try:
            resp = await self._request("GET", f"/api/v1/{media_type}/{tmdb_id}")
            detail = resp.json()
            release = detail.get("releaseDate") or detail.get("firstAirDate") or ""
            year = int(release[:4]) if len(release) >= 4 else None
            entry = {
                "title": seerr_title(detail),
                "year": year,
                "overview": detail.get("overview") or "",
                "poster_path": detail.get("posterPath"),
                "ts": time.time(),
            }
            self._detail_cache[cache_key] = entry
            return entry
        except Exception:
            return {"title": "Unknown", "year": None, "overview": "", "poster_path": None, "ts": 0}

    async def get_recently_added(self, take: int = 5) -> list[dict]:
        """Get media recently added to the library with resolved titles (concurrent)."""
        resp = await self._request(
            "GET", "/api/v1/media",
            params={"filter": "allavailable", "sort": "mediaAdded", "take": take},
        )
        data = resp.json()
        items = [
            item for item in data.get("results", [])
            if item.get("tmdbId") and item.get("mediaType")
            and item.get("status") in (MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE)
        ]
        if not items:
            return []

        async def resolve_title(item: dict) -> dict:
            tmdb_id = item["tmdbId"]
            media_type = item["mediaType"]
            added = item.get("mediaAddedAt") or item.get("createdAt", "")
            detail = await self._resolve_detail(media_type, tmdb_id)
            return {
                "title": detail["title"],
                "mediaType": media_type,
                "tmdbId": tmdb_id,
                "addedAt": added,
            }

        return await asyncio.gather(*[resolve_title(item) for item in items])

    async def get_server_state(self, page: int = 1, take: int = 10) -> dict:
        """Fetch available + requested/processing media in parallel.

        Returns {"available": [...], "requested": [...]}, each item with
        title, year, media_type, overview, status, tmdb_id, and a timestamp field.
        """
        skip = (page - 1) * take
        log.info("Fetching server state (page=%d, take=%d)", page, take)

        available_resp, processing_resp, pending_resp = await asyncio.gather(
            self._request("GET", "/api/v1/media", params={
                "filter": "allavailable", "sort": "mediaAdded",
                "take": take, "skip": skip,
            }),
            self._request("GET", "/api/v1/request", params={
                "filter": "processing", "take": take, "skip": skip,
            }),
            self._request("GET", "/api/v1/request", params={
                "filter": "pending", "take": take, "skip": skip,
            }),
        )

        # --- Available items ---
        available_raw = [
            item for item in available_resp.json().get("results", [])
            if item.get("tmdbId") and item.get("mediaType")
            and item.get("status") in (MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE)
        ]

        # --- Requested items (merge processing + pending) ---
        requested_raw: list[dict] = []
        for resp in (processing_resp, pending_resp):
            for req in resp.json().get("results", []):
                media = req.get("media") or {}
                if media.get("tmdbId") and media.get("mediaType"):
                    requested_raw.append(req)

        # Dedupe: if an item is in both available and requested, keep in requested only
        requested_keys: set[tuple[str, int]] = set()
        for req in requested_raw:
            media = req["media"]
            requested_keys.add((media["mediaType"], media["tmdbId"]))
        available_raw = [
            item for item in available_raw
            if (item["mediaType"], item["tmdbId"]) not in requested_keys
        ]

        # Resolve details concurrently for all items
        async def resolve_available(item: dict) -> dict:
            tmdb_id = item["tmdbId"]
            media_type = item["mediaType"]
            detail = await self._resolve_detail(media_type, tmdb_id)
            raw_status = item.get("status", 0)
            try:
                status = MediaStatus(raw_status)
            except ValueError:
                status = MediaStatus.UNKNOWN
            return {
                "title": detail["title"],
                "year": detail["year"],
                "media_type": media_type,
                "overview": detail["overview"],
                "status": status.name.lower(),
                "tmdb_id": tmdb_id,
                "added_at": item.get("mediaAddedAt") or item.get("createdAt", ""),
            }

        async def resolve_requested(req: dict) -> dict:
            media = req["media"]
            tmdb_id = media["tmdbId"]
            media_type = media["mediaType"]
            detail = await self._resolve_detail(media_type, tmdb_id)
            raw_status = media.get("status", 0)
            try:
                status = MediaStatus(raw_status)
            except ValueError:
                status = MediaStatus.UNKNOWN
            return {
                "title": detail["title"],
                "year": detail["year"],
                "media_type": media_type,
                "overview": detail["overview"],
                "status": status.name.lower(),
                "tmdb_id": tmdb_id,
                "requested_at": req.get("createdAt", ""),
            }

        available, requested = await asyncio.gather(
            asyncio.gather(*[resolve_available(item) for item in available_raw]),
            asyncio.gather(*[resolve_requested(req) for req in requested_raw]),
        )

        return {
            "available": list(available),
            "requested": list(requested),
        }

    def _parse_results(
        self,
        items: list[dict],
        take: int = 5,
        *,
        filter_lang_year: bool = False,
        min_votes: int = 0,
        exclude_ids: set[int] | None = None,
    ) -> list[SearchResult]:
        """Parse raw API result items into SearchResult objects.

        When filter_lang_year is True, skip results outside allowed languages
        and older than MIN_YEAR. min_votes filters items with insufficient votes
        (only when filter_lang_year is True).
        """
        results: list[SearchResult] = []
        for item in items:
            if len(results) >= take:
                break

            media_type = item.get("mediaType", "")
            if media_type not in ("movie", "tv"):
                continue

            if exclude_ids and item.get("id") in exclude_ids:
                continue

            release = item.get("releaseDate") or item.get("firstAirDate") or ""
            year = int(release[:4]) if len(release) >= 4 else None

            if filter_lang_year:
                lang = item.get("originalLanguage", "")
                if lang and lang not in ALLOWED_LANGUAGES:
                    continue
                if year is not None and year < MIN_YEAR:
                    continue
                if min_votes > 0:
                    vote_count = item.get("voteCount", 0) or 0
                    if vote_count < min_votes:
                        continue

            media_info = item.get("mediaInfo") or {}
            raw_status = media_info.get("status", 0)
            try:
                status = MediaStatus(raw_status)
            except ValueError:
                status = MediaStatus.UNKNOWN

            # Extract download progress when actively downloading
            download_progress = parse_download_progress(media_info) if status == MediaStatus.PROCESSING else None

            title = seerr_title(item)

            vote_avg = item.get("voteAverage")
            rating = round(vote_avg, 1) if vote_avg else None

            results.append(
                SearchResult(
                    tmdb_id=item["id"],
                    title=title,
                    year=year,
                    media_type=media_type,
                    overview=item.get("overview") or "",
                    status=status,
                    poster_path=item.get("posterPath"),
                    rating=rating,
                    download_progress=download_progress,
                )
            )
        return results

    async def search_person_credits(
        self, name: str, want_type: str | None = None,
        take: int = 7, exclude_ids: set[int] | None = None,
    ) -> list[SearchResult]:
        """Search for a person by name and return their filmography.

        Uses /api/v1/search to find the person, then /api/v1/person/{id}/combined_credits
        to get their movies/shows sorted by popularity.

        Filters out talk show / award show appearances (character starts with "Self")
        and non-directing crew credits (keeps only Director jobs for crew).
        """
        resp = await self._try_search(name)
        data = resp.json()

        # Find the first person result
        person = None
        for item in data.get("results", []):
            if item.get("mediaType") == "person":
                person = item
                break
        if not person:
            return []

        person_id = person["id"]
        person_name = person.get("name", name)
        log.info("Found person: %s (id=%d)", person_name, person_id)

        # Get their combined credits (movies + TV)
        try:
            credits_resp = await self._request(
                "GET", f"/api/v1/person/{person_id}/combined_credits",
                params={"language": "en"},
            )
            credits_data = credits_resp.json()
        except Exception as e:
            log.warning("Person credits lookup failed for %s: %s", person_name, e)
            return []

        # Filter cast: exclude "Self" appearances (talk shows, award shows, interviews)
        seen_ids: set[int] = set()
        filtered: list[dict] = []
        for credit in credits_data.get("cast", []):
            char = (credit.get("character") or "").strip()
            if char.startswith("Self"):
                continue
            cid = credit.get("id")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                filtered.append(credit)

        # Filter crew: only directing credits (skip Thanks, Producer dupes)
        for credit in credits_data.get("crew", []):
            if credit.get("job") != "Director":
                continue
            cid = credit.get("id")
            if cid and cid not in seen_ids:
                seen_ids.add(cid)
                filtered.append(credit)

        filtered.sort(key=lambda x: x.get("popularity", 0), reverse=True)
        results = self._parse_results(
            filtered, take,
            filter_lang_year=True, min_votes=500, exclude_ids=exclude_ids,
        )
        # Tag results so the caller knows they came from person search
        for r in results:
            r.from_person = True
        return results

    async def get_recommendations(self, media_type: str, tmdb_id: int, take: int = 3, exclude_ids: set[int] | None = None) -> list[SearchResult]:
        """Get recommendations based on a specific movie or TV show."""
        log.info("Seerr recommendations: %s/%d", media_type, tmdb_id)
        resp = await self._request(
            "GET", f"/api/v1/{media_type}/{tmdb_id}/recommendations",
            params={"language": "en"},
        )
        data = resp.json()
        results = self._parse_results(data.get("results", []), take, filter_lang_year=True, exclude_ids=exclude_ids)
        log.info("Seerr recommendations returned %d results", len(results))
        return results

    async def get_similar(self, media_type: str, tmdb_id: int, take: int = 3, exclude_ids: set[int] | None = None) -> list[SearchResult]:
        """Get similar titles for a specific movie or TV show (fallback for recommendations)."""
        log.info("Seerr similar: %s/%d", media_type, tmdb_id)
        resp = await self._request(
            "GET", f"/api/v1/{media_type}/{tmdb_id}/similar",
            params={"language": "en"},
        )
        data = resp.json()
        results = self._parse_results(data.get("results", []), take, filter_lang_year=True, exclude_ids=exclude_ids)
        log.info("Seerr similar returned %d results", len(results))
        return results

    async def discover_trending(self, take: int = 5, exclude_ids: set[int] | None = None) -> list[SearchResult]:
        """Get trending movies and TV shows."""
        log.info("Seerr discover: trending")
        resp = await self._request(
            "GET", "/api/v1/discover/trending",
            params={"language": "en"},
        )
        data = resp.json()
        results = self._parse_results(data.get("results", []), take, filter_lang_year=True, exclude_ids=exclude_ids)
        log.info("Seerr trending returned %d results", len(results))
        return results

    async def discover_movies(
        self, genre_id: int | None = None, keyword_ids: list[int] | None = None,
        year: int | None = None, year_end: int | None = None, take: int = 5,
        exclude_ids: set[int] | None = None,
    ) -> list[SearchResult]:
        """Discover movies by genre, keywords, and/or year."""
        date_params = ("primaryReleaseDateGte", "primaryReleaseDateLte")
        return await self._discover_paginated(
            "movies", date_params, genre_id=genre_id, keyword_ids=keyword_ids,
            year=year, year_end=year_end, take=take, exclude_ids=exclude_ids,
        )

    async def discover_tv(
        self, genre_id: int | None = None, keyword_ids: list[int] | None = None,
        year: int | None = None, year_end: int | None = None, take: int = 5,
        exclude_ids: set[int] | None = None,
    ) -> list[SearchResult]:
        """Discover TV shows by genre, keywords, and/or year."""
        date_params = ("firstAirDateGte", "firstAirDateLte")
        return await self._discover_paginated(
            "tv", date_params, genre_id=genre_id, keyword_ids=keyword_ids,
            year=year, year_end=year_end, take=take, exclude_ids=exclude_ids,
        )

    async def _discover_paginated(
        self, media_type: str, date_params: tuple[str, str], *,
        genre_id: int | None = None, keyword_ids: list[int] | None = None,
        year: int | None = None, year_end: int | None = None, take: int = 5,
        exclude_ids: set[int] | None = None,
    ) -> list[SearchResult]:
        """Shared paginated discovery for movies and TV."""
        params: dict[str, Any] = {
            "sortBy": "popularity.desc",
            "voteCountGte": 50,
            "language": "en",
        }
        if genre_id is not None:
            params["genre"] = str(genre_id)
        if keyword_ids:
            # Pipe = OR logic (comma = AND, too restrictive)
            params["keywords"] = "|".join(str(k) for k in keyword_ids)
        if year is not None:
            params[date_params[0]] = f"{year}-01-01"
            end = year_end or year
            params[date_params[1]] = f"{end}-12-31"
        log.info("Seerr discover %s: genre=%s keywords=%s year=%s", media_type, genre_id, bool(keyword_ids), year)
        results: list[SearchResult] = []
        for page in range(1, 6):
            params["page"] = page
            try:
                resp = await self._request(
                    "GET", f"/api/v1/discover/{media_type}", params=params
                )
            except Exception as e:
                if page == 1:
                    raise
                log.debug("Discover %s page %d failed: %s", media_type, page, e)
                break
            data = resp.json()
            batch = self._parse_results(
                data.get("results", []), take - len(results),
                filter_lang_year=True, min_votes=10, exclude_ids=exclude_ids,
            )
            results.extend(batch)
            if len(results) >= take or page >= data.get("totalPages", 1):
                break
        log.info("Seerr discover %s returned %d results", media_type, len(results))
        return results[:take]

    async def close(self) -> None:
        await self.client.aclose()
