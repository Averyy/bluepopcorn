from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from typing import Any

import httpx

from .config import Settings
from .types import MediaStatus, SearchResult

log = logging.getLogger(__name__)

ALLOWED_LANGUAGES = {"en", "es", "ja", "ko"}
MIN_YEAR = 2000

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


class SeerrAuthError(SeerrError):
    """Authentication failed after retry."""


class SeerrSearchError(SeerrError):
    """Search query returned an error (e.g. 400 bad query)."""


class SeerrClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.seerr_url.rstrip("/")
        self.email = settings.seerr_email
        self.password = settings.seerr_password
        self.client = httpx.AsyncClient(timeout=settings.http_timeout)
        self._cookie: str | None = None
        self._auth_lock = asyncio.Lock()
        # Dynamic genre maps, loaded lazily
        self._genre_map_movie: dict[str, int] | None = None
        self._genre_map_tv: dict[str, int] | None = None

    async def authenticate(self) -> None:
        """Authenticate with Seerr and store session cookie."""
        log.info("Authenticating with Seerr at %s", self.base_url)
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/v1/auth/local",
                json={"email": self.email, "password": self.password},
            )
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            raise SeerrConnectionError(f"Connection failed: {e}") from e
        resp.raise_for_status()
        cookie = resp.cookies.get("connect.sid")
        if cookie:
            self._cookie = cookie
            self.client.cookies.set("connect.sid", cookie)
            log.info("Seerr authentication successful")
        else:
            raise SeerrAuthError("No session cookie returned from Seerr auth")

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Make an authenticated request, re-auth on 401 or missing session.

        Uses %20 encoding for query params instead of httpx's default +.
        Seerr 3.x rejects + as space encoding.
        """
        # Authenticate lazily if we never got a session cookie
        if not self._cookie:
            async with self._auth_lock:
                if not self._cookie:  # Double-check after acquiring lock
                    await self.authenticate()

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

        if resp.status_code == 401:
            log.warning("401 on %s %s (has_cookie=%s), re-authenticating", method, path, bool(self._cookie))
            async with self._auth_lock:
                try:
                    await self.authenticate()
                except SeerrConnectionError:
                    raise
                except Exception as e:
                    raise SeerrAuthError(f"Re-authentication failed: {e}") from e
            try:
                resp = await self.client.request(method, url, **kwargs)
            except httpx.ConnectError as e:
                log.error("Seerr connect failed after re-auth: %s %s — %s", method, path, e)
                raise SeerrConnectionError(f"Connect failed after re-auth ({method} {path}): {e}") from e
            except httpx.TimeoutException as e:
                log.error("Seerr timeout after re-auth: %s %s — %s", method, path, e)
                raise SeerrConnectionError(f"Timeout after re-auth ({method} {path}): {e}") from e

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

    async def search(self, query: str) -> list[SearchResult]:
        """Search for movies and TV shows."""
        # Extract hints from the original query before cleaning
        query_lower = query.lower()
        want_movie = any(w in query_lower for w in ("movie", "film"))
        want_tv = any(w in query_lower for w in ("tv", "show", "series"))
        # Only strip trailing years — preserves "2001: A Space Odyssey", "1917", "Blade Runner 2049"
        year_match = re.search(r"\s+((?:19|20)\d{2})\s*$", query)
        want_year = int(year_match.group(1)) if year_match else None

        # Strip filler words but keep the year in the search query —
        # the year might be part of the title (e.g. "Blade Runner 2049", "2012")
        cleaned = re.sub(r"\b(movie|film|tv|show|series)\b", "", query, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        search_query = cleaned or query

        log.info("Seerr search: %s", search_query)

        resp = await self._try_search(search_query)
        data = resp.json()
        results = self._parse_results(data.get("results", []))

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
            elif want_year and not results:
                # Year in query produced no results — retry without it
                # (handles "severance 2022" where 2022 is a filter, not part of the title)
                stripped = re.sub(r"\s+(19|20)\d{2}\s*$", "", search_query).strip()
                if stripped and stripped != search_query:
                    log.info("Retrying without year: %s", stripped)
                    resp = await self._try_search(stripped)
                    data = resp.json()
                    results = self._parse_results(data.get("results", []))
                    # Re-apply filters on new results
                    if want_movie and not want_tv:
                        results = [r for r in results if r.media_type == "movie"] or results
                    elif want_tv and not want_movie:
                        results = [r for r in results if r.media_type == "tv"] or results
                    year_filtered = [r for r in results if r.year == want_year]
                    if year_filtered:
                        results = year_filtered

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
        except (SeerrConnectionError, SeerrAuthError):
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

    async def request_media(self, media_type: str, tmdb_id: int) -> dict:
        """Request a movie or TV show on Seerr."""
        log.info("Seerr request: %s tmdb:%d", media_type, tmdb_id)
        payload: dict[str, Any] = {"mediaType": media_type, "mediaId": tmdb_id}
        if media_type == "tv":
            # Explicitly pass season numbers — omitting seasons crashes some shows
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
            return [
                s["seasonNumber"]
                for s in data.get("seasons", [])
                if s.get("seasonNumber", 0) > 0
            ]
        except Exception as e:
            log.warning("Failed to fetch seasons for tv/%d: %s", tmdb_id, e)
            return []

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

    async def get_recent_available(self) -> list[dict]:
        """Get recently available media."""
        resp = await self._request(
            "GET", "/api/v1/request", params={"filter": "available", "take": 5}
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

    async def get_trailer(self, media_type: str, tmdb_id: int) -> str | None:
        """Get a YouTube trailer/teaser URL for a movie or TV show."""
        try:
            resp = await self._request("GET", f"/api/v1/{media_type}/{tmdb_id}")
            data = resp.json()
            for video in data.get("relatedVideos", []):
                if video.get("site") == "YouTube" and video.get("type") in ("Trailer", "Teaser"):
                    # Prefer the full url field if available, fall back to building from key
                    url = video.get("url")
                    if url:
                        return url
                    key = video.get("key")
                    if key:
                        return f"https://youtu.be/{key}"
        except Exception as e:
            log.debug("Trailer lookup failed for %s/%d: %s", media_type, tmdb_id, e)
        return None

    async def get_recently_added(self, take: int = 5) -> list[dict]:
        """Get media recently added to the library with resolved titles (concurrent)."""
        resp = await self._request(
            "GET", "/api/v1/media",
            params={"sort": "mediaAdded", "take": take},
        )
        data = resp.json()
        items = [
            item for item in data.get("results", [])
            if item.get("tmdbId") and item.get("mediaType")
        ]
        if not items:
            return []

        # Resolve titles concurrently
        async def resolve_title(item: dict) -> dict:
            tmdb_id = item["tmdbId"]
            media_type = item["mediaType"]
            added = item.get("mediaAddedAt") or item.get("createdAt", "")
            try:
                detail_resp = await self._request(
                    "GET", f"/api/v1/{media_type}/{tmdb_id}"
                )
                detail = detail_resp.json()
                title = seerr_title(detail)
            except Exception:
                title = item.get("externalServiceSlug", "Unknown").replace("-", " ").title()
            return {
                "title": title,
                "mediaType": media_type,
                "tmdbId": tmdb_id,
                "addedAt": added,
            }

        return await asyncio.gather(*[resolve_title(item) for item in items])

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
                    overview=(item.get("overview") or "")[:200],
                    status=status,
                    poster_path=item.get("posterPath"),
                    rating=rating,
                    download_progress=download_progress,
                )
            )
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
        self, genre_id: int | None = None, year: int | None = None,
        year_end: int | None = None, take: int = 5,
        exclude_ids: set[int] | None = None,
    ) -> list[SearchResult]:
        """Discover movies by genre and/or year."""
        params: dict[str, Any] = {
            "sortBy": "popularity.desc",
            "voteCountGte": 50,
            "language": "en",
        }
        if genre_id is not None:
            params["genre"] = str(genre_id)
        if year is not None:
            params["primaryReleaseDateGte"] = f"{year}-01-01"
            end = year_end or year
            params["primaryReleaseDateLte"] = f"{end}-12-31"
        log.info("Seerr discover movies: genre=%s year=%s", genre_id, year)
        results: list[SearchResult] = []
        for page in range(1, 6):
            params["page"] = page
            try:
                resp = await self._request("GET", "/api/v1/discover/movies", params=params)
            except Exception as e:
                if page == 1:
                    raise
                log.debug("Discover movies page %d failed: %s", page, e)
                break
            data = resp.json()
            batch = self._parse_results(
                data.get("results", []), take - len(results),
                filter_lang_year=True, min_votes=10, exclude_ids=exclude_ids,
            )
            results.extend(batch)
            if len(results) >= take or page >= data.get("totalPages", 1):
                break
        log.info("Seerr discover movies returned %d results", len(results))
        return results[:take]

    async def discover_tv(
        self, genre_id: int | None = None, year: int | None = None,
        year_end: int | None = None, take: int = 5,
        exclude_ids: set[int] | None = None,
    ) -> list[SearchResult]:
        """Discover TV shows by genre and/or year."""
        params: dict[str, Any] = {
            "sortBy": "popularity.desc",
            "voteCountGte": 50,
            "language": "en",
        }
        if genre_id is not None:
            params["genre"] = str(genre_id)
        if year is not None:
            params["firstAirDateGte"] = f"{year}-01-01"
            end = year_end or year
            params["firstAirDateLte"] = f"{end}-12-31"
        log.info("Seerr discover tv: genre=%s year=%s", genre_id, year)
        results: list[SearchResult] = []
        for page in range(1, 6):
            params["page"] = page
            try:
                resp = await self._request("GET", "/api/v1/discover/tv", params=params)
            except Exception as e:
                if page == 1:
                    raise
                log.debug("Discover tv page %d failed: %s", page, e)
                break
            data = resp.json()
            batch = self._parse_results(
                data.get("results", []), take - len(results),
                filter_lang_year=True, min_votes=10, exclude_ids=exclude_ids,
            )
            results.extend(batch)
            if len(results) >= take or page >= data.get("totalPages", 1):
                break
        log.info("Seerr discover tv returned %d results", len(results))
        return results[:take]

    async def close(self) -> None:
        await self.client.aclose()
