from __future__ import annotations

import asyncio
import datetime
import logging
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .db import BotDatabase
from .llm import LLMClient
from .posters import PosterHandler
from .seerr import (
    SeerrClient,
    SeerrError,
    SeerrSearchError,
)
from .sender import MessageSender
from .types import Action, LLMDecision, MediaStatus, RequestStatus, SearchResult
from .weather import get_weather, get_pollen

log = logging.getLogger(__name__)


def format_search_results(results: list[SearchResult]) -> str:
    """Format search results as context for the LLM."""
    if not results:
        return "[No results found]"

    lines = ["[Search results:"]
    for i, r in enumerate(results, 1):
        year_str = f" ({r.year})" if r.year else ""
        type_str = "TV" if r.media_type == "tv" else "Movie"
        overview = r.overview[:150] if r.overview else "No description"
        rating_str = f" Rating: {r.rating}/10" if r.rating else ""
        # Append RT and IMDB ratings when available
        ext_ratings: list[str] = []
        if r.rt_rating:
            ext_ratings.append(f"RT: {r.rt_rating}")
        if r.imdb_rating:
            ext_ratings.append(f"IMDB: {r.imdb_rating}")
        ext_rating_str = " | ".join(ext_ratings)
        if ext_rating_str:
            rating_str += f" | {ext_rating_str}" if rating_str else f" {ext_rating_str}"
        trailer_str = f" Trailer: {r.trailer_url}" if r.trailer_url else ""
        lines.append(
            f"{i}. {r.title}{year_str} [{type_str}] tmdb:{r.tmdb_id} "
            f"- {overview} (Status: {r.status_label}){rating_str}{trailer_str}"
        )
    lines.append("]")
    return "\n".join(lines)


async def _resolve_request_title(req: dict, seerr: SeerrClient) -> str:
    """Resolve a display title from a Seerr request object.

    Request objects have media: MediaInfo which does NOT have a title field.
    Must look up via the detail endpoint using tmdbId.
    """
    media = req.get("media", {})
    tmdb_id = media.get("tmdbId")
    media_type = media.get("mediaType")
    if tmdb_id and media_type:
        try:
            detail = await seerr.get_media_status(media_type, tmdb_id)
            if detail:
                title = detail.get("title") or detail.get("name")
                if title:
                    return title
        except Exception:
            pass
    # Last resort fallback
    slug = media.get("externalServiceSlug", "")
    if slug and not slug.isdigit():
        return slug.replace("-", " ").title()
    return "Unknown"


class ActionExecutor:
    def __init__(
        self,
        seerr: SeerrClient,
        llm: LLMClient,
        sender: MessageSender | None,
        posters: PosterHandler | None,
        db: BotDatabase,
        settings: Settings,
    ) -> None:
        self.seerr = seerr
        self.llm = llm
        self.sender = sender
        self.posters = posters
        self.db = db
        self.settings = settings

    async def handle_message(
        self,
        sender_phone: str,
        text: str,
    ) -> str:
        """Process a user message through the full LLM -> action -> response loop.

        Returns the final response text to send to the user.
        """
        # Check bypass commands first
        bypass = self._check_bypass(text)
        if bypass is not None:
            await self.db.add_history(sender_phone, "user", text)
            response = await self._handle_bypass(bypass, sender_phone)
            await self.db.add_history(sender_phone, "assistant", response)
            return response

        # Save user message to history
        await self.db.add_history(sender_phone, "user", text)

        # Build prompt from conversation history
        prompt = await self._build_prompt(sender_phone)

        # Get LLM decision
        try:
            decision, meta = await self.llm.decide(prompt)
            log.info("LLM action=%s query=%s", decision.action.value, decision.query or "-")
        except Exception as e:
            log.error("LLM call failed: %s", e)
            return "Something went wrong on my end, try again in a sec."

        # Execute the action
        response = await self._execute(decision, sender_phone)

        # Save assistant response to history
        await self.db.add_history(sender_phone, "assistant", response)

        return response

    def _check_bypass(self, text: str) -> str | None:
        """Check if the message is a bypass command."""
        lower = text.strip().lower()
        if lower in ("status", "pending"):
            return "status"
        if lower in ("new", "reset", "clear"):
            return "new"
        if lower == "help":
            return "help"
        return None

    async def _handle_bypass(self, command: str, sender_phone: str = "") -> str:
        """Handle a bypass command directly (no LLM)."""
        if command == "status":
            try:
                # Use request/count for quick overview
                counts = await self.seerr.get_request_count()
                has_activity = (
                    counts.get("pending", 0)
                    or counts.get("processing", 0)
                    or counts.get("available", 0)
                )

                if not has_activity:
                    return "No pending or in-progress requests."

                # Fetch details for active categories concurrently
                tasks = []
                fetch_processing = counts.get("processing", 0) > 0
                fetch_pending = counts.get("pending", 0) > 0

                if fetch_processing:
                    tasks.append(self.seerr.get_processing())
                if fetch_pending:
                    tasks.append(self.seerr.get_pending())
                # Always get recently added for context
                tasks.append(self.seerr.get_recently_added(take=3))

                results = await asyncio.gather(*tasks)
                idx = 0

                processing = results[idx] if fetch_processing else []
                if fetch_processing:
                    idx += 1
                pending = results[idx] if fetch_pending else []
                if fetch_pending:
                    idx += 1
                recently_added = results[idx]

                # Resolve titles concurrently for all requests
                all_reqs = list(processing[:5]) + list(pending[:5])
                if all_reqs:
                    titles = await asyncio.gather(
                        *[_resolve_request_title(req, self.seerr) for req in all_reqs]
                    )
                else:
                    titles = []
                proc_count = min(len(processing), 5)

                lines: list[str] = []
                if processing:
                    lines.append("Downloading:")
                    for title in titles[:proc_count]:
                        lines.append(f"- {title}")
                if pending:
                    lines.append("Waiting for approval:")
                    for title in titles[proc_count:]:
                        lines.append(f"- {title}")
                if recently_added:
                    lines.append("Recently added:")
                    for item in recently_added[:3]:
                        lines.append(f"- {item['title']}")

                return "\n".join(lines) if lines else "No pending or in-progress requests."
            except Exception as e:
                log.error("Status check failed: %s", e)
                return "Something went wrong, try again in a sec."

        if command == "new":
            if sender_phone:
                await self.db.clear_history(sender_phone)
            return "Fresh start. What's up?"

        if command == "help":
            return (
                "Things I can do:\n"
                "- Add movies/shows (e.g. 'add severance')\n"
                "- Tell you about a title ('what's Bugonia about?')\n"
                "- Weather & pollen ('what's the weather like?')\n"
                "- What's new on the server ('what's been added?')\n"
                "- Remember things ('remember I like sci-fi')\n"
                "- 'status' - check pending requests\n"
                "- 'new' or 'reset' - fresh conversation\n"
                "- 'help' - this message"
            )

        return "Unknown command."

    async def _llm_respond(self, sender_phone: str, fallback: str = "") -> str:
        """Build prompt with current history and let the LLM generate a response.

        Called after storing API data as context — the LLM sees the data
        in conversation history and crafts a contextual response.
        """
        prompt = await self._build_prompt(sender_phone)
        try:
            decision, meta = await self.llm.decide(prompt)
            log.debug("LLM respond: action=%s message=%s", decision.action.value, (decision.message or "")[:100])
            # Allow request as a follow-up (user confirms a search result)
            if decision.action == Action.REQUEST and decision.tmdb_id:
                return await self._handle_request(decision, sender_phone)
            # Only accept reply — any other action means the LLM is confused
            # (e.g., returning "recommend" with filler instead of describing results)
            if decision.action == Action.REPLY and decision.message and len(decision.message.strip()) > 2:
                return decision.message
            log.warning(
                "LLM response returned action=%s message=%r instead of reply, using fallback",
                decision.action.value, (decision.message or "")[:100],
            )
            return fallback or "Something went wrong, try again in a sec."
        except Exception as e:
            log.error("LLM response call failed: %s", e)
            return fallback or "Something went wrong, try again in a sec."

    async def _build_prompt(self, sender_phone: str) -> str:
        """Build the full prompt from conversation history."""
        parts: list[str] = []

        # Time context
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.datetime.now(tz)
        time_str = now.strftime("%A %B %-d, %Y %-I:%M %p %Z")
        parts.append(f"<context>[Current time: {time_str}]</context>")

        # User memory (stored facts)
        facts = await self.db.get_facts(sender_phone)
        if facts:
            memory_lines = "\n".join(f"- {f}" for f in facts)
            parts.append(f"<memory>\n{memory_lines}\n</memory>")

        # Conversation history
        history = await self.db.get_history(sender_phone)
        for entry in history:
            if entry.role == "user":
                parts.append(f"<user>{entry.content}</user>")
            elif entry.role == "assistant":
                parts.append(f"<assistant>{entry.content}</assistant>")
            elif entry.role == "context":
                parts.append(f"<context>{entry.content}</context>")

        return "\n".join(parts)

    async def _execute(self, decision: LLMDecision, sender_phone: str) -> str:
        """Execute an LLM decision and return the response text."""
        if decision.action == Action.SEARCH:
            return await self._handle_search(decision, sender_phone)
        elif decision.action == Action.REQUEST:
            return await self._handle_request(decision, sender_phone)
        elif decision.action == Action.CHECK_STATUS:
            return await self._handle_check_status(decision, sender_phone)
        elif decision.action == Action.WEATHER:
            return await self._handle_weather(decision, sender_phone)
        elif decision.action == Action.RECENT:
            return await self._handle_recent(decision, sender_phone)
        elif decision.action == Action.RECOMMEND:
            return await self._handle_recommend(decision, sender_phone)
        elif decision.action == Action.REMEMBER:
            return await self._handle_remember(decision, sender_phone)
        elif decision.action == Action.FORGET:
            return await self._handle_forget(decision, sender_phone)
        else:  # REPLY
            return decision.message

    async def _handle_search(
        self, decision: LLMDecision, sender_phone: str
    ) -> str:
        """Execute a search action: search Seerr, send poster, format results."""
        query = decision.query or decision.message
        try:
            results = await self.seerr.search(query)
        except SeerrSearchError:
            return f"Couldn't find anything for \"{query}\"."
        except Exception as e:
            log.error("Search failed for '%s': %s", query, e)
            return "Something went wrong, try again in a sec."

        if not results:
            await self.db.add_history(sender_phone, "context", "[No results found]")
            return f"Couldn't find anything for \"{query}\"."

        # Fetch trailers and ratings for top results (concurrently)
        top = results[:3]
        trailer_tasks = [
            self.seerr.get_trailer(r.media_type, r.tmdb_id)
            for r in top
        ]
        rating_tasks = [
            self.seerr.get_ratings(r.media_type, r.tmdb_id)
            for r in top
        ]
        all_results = await asyncio.gather(*trailer_tasks, *rating_tasks)
        trailers = all_results[:len(top)]
        ratings = all_results[len(top):]
        for i, trailer_url in enumerate(trailers):
            if trailer_url and i < len(results):
                results[i].trailer_url = trailer_url
        for i, rating_dict in enumerate(ratings):
            if rating_dict and i < len(results):
                self._apply_ratings(results[i], rating_dict)

        # Fetch history once for poster logic and narrowing
        history = await self.db.get_history(sender_phone) if (self.sender and self.posters) or len(results) > 1 else []

        # If multiple results but one matches a recently discussed title, narrow to it
        if len(results) > 1:
            recent_tmdb_ids: set[int] = set()
            for entry in reversed(history):
                if entry.role == "context":
                    for r in results:
                        if f"tmdb:{r.tmdb_id}" in entry.content:
                            recent_tmdb_ids.add(r.tmdb_id)
                # Only look back through recent exchanges
                if entry.role == "user" and len(recent_tmdb_ids) > 0:
                    break
            if recent_tmdb_ids:
                matched = [r for r in results if r.tmdb_id in recent_tmdb_ids]
                if len(matched) == 1:
                    results = matched

        # Send poster — collage for add/request disambiguation,
        # single poster for info queries, skip for status checks
        if self.sender and self.posters:
            last_user_msg = ""
            for entry in reversed(history):
                if entry.role == "user":
                    last_user_msg = entry.content.lower()
                    break
            adding = any(w in last_user_msg for w in ("add", "request", "get", "download"))
            checking_status = any(w in last_user_msg for w in (
                "status", "done", "ready", "downloading", "is it",
                "update", "progress", "where is", "how is",
            ))
            # Check if top result was already shown in recent context
            already_shown = False
            for entry in reversed(history):
                if entry.role == "context" and results and f"tmdb:{results[0].tmdb_id}" in entry.content:
                    already_shown = True
                    break
                if entry.role == "user":
                    break  # Only check context from current exchange
            if adding and len(results) > 1:
                # Filter to results with posters so collage indices match text list
                results_with_posters = [r for r in results if r.poster_path]
                if results_with_posters:
                    results = results_with_posters
                await self._send_posters(sender_phone, results)
            elif not checking_status and not already_shown and results:
                await self._send_single_poster(sender_phone, results[0])

        # Store results as context, then let the LLM craft the response.
        # The LLM sees search results + conversation history and can:
        # - Describe a single result naturally based on what the user asked
        # - Disambiguate multiple results using prior context
        # - Offer to request, check status, etc. as appropriate
        context = format_search_results(results)
        await self.db.add_history(sender_phone, "context", context)

        fallback = (
            self._format_single_result(results[0])
            if len(results) == 1
            else self._format_multiple_results(results)
        )
        return await self._llm_respond(sender_phone, fallback=fallback)

    async def _send_single_poster(
        self, phone: str, result: SearchResult
    ) -> None:
        """Send a single poster for the top result."""
        assert self.sender is not None
        assert self.posters is not None
        try:
            poster = await self.posters.get_single_poster(result)
            if poster:
                await self.sender.send_image(phone, str(poster))
        except Exception as e:
            log.error("Failed to send single poster: %s", e)

    async def _send_posters(
        self, phone: str, results: list[SearchResult]
    ) -> None:
        """Download and send poster image(s)."""
        assert self.sender is not None
        assert self.posters is not None

        try:
            if len(results) == 1:
                poster = await self.posters.get_single_poster(results[0])
                if poster:
                    await self.sender.send_image(phone, str(poster))
            else:
                collage = await self.posters.create_collage(results)
                if collage:
                    await self.sender.send_image(phone, str(collage))
        except Exception as e:
            log.error("Failed to send poster: %s", e)

    def _format_results_plain(self, results: list[SearchResult]) -> str:
        """Plain text fallback for search results."""
        lines: list[str] = []
        for i, r in enumerate(results, 1):
            year = f" ({r.year})" if r.year else ""
            status = f" [{r.status_label}]" if r.status != MediaStatus.NOT_TRACKED else ""
            lines.append(f"{i}. {r.title}{year}{status}")
        lines.append("\nWhich one?")
        return "\n".join(lines)

    @staticmethod
    def _apply_ratings(result: SearchResult, rating_dict: dict) -> None:
        """Apply enriched ratings from get_ratings() to a SearchResult."""
        rt = rating_dict.get("rt")
        freshness = rating_dict.get("rt_freshness")
        if rt and freshness:
            result.rt_rating = f"{rt} {freshness}"
        elif rt:
            result.rt_rating = rt
        result.imdb_rating = rating_dict.get("imdb")

    @staticmethod
    def _format_rating_str(r: SearchResult) -> str:
        """Build a compact rating string from all available sources."""
        parts: list[str] = []
        if r.rt_rating:
            parts.append(f"{r.rt_rating} on RT")
        if r.imdb_rating:
            parts.append(f"{r.imdb_rating} on IMDB")
        if r.rating:
            parts.append(f"{r.rating}/10 on TMDB")
        if not parts:
            return ""
        return ", ".join(parts) + "."

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        """Truncate text to max_len, cutting at the last space."""
        if len(text) <= max_len:
            return text
        cut = text[:max_len].rfind(" ")
        if cut > max_len // 2:
            return text[:cut]
        return text[:max_len]

    @staticmethod
    def _format_single_result(r: SearchResult) -> str:
        """Format a single search result as a casual text message."""
        year = f" ({r.year})" if r.year else ""
        title = f"{r.title}{year}"

        parts: list[str] = []

        # Overview, truncated to ~200 chars
        if r.overview:
            overview = ActionExecutor._truncate(r.overview, 200).rstrip(".")
            parts.append(f"{title} — {overview}.")
        else:
            parts.append(f"{title}.")

        # Ratings
        rating_str = ActionExecutor._format_rating_str(r)
        if rating_str:
            parts.append(rating_str)

        # Trailer
        if r.trailer_url:
            parts.append(f"Trailer: {r.trailer_url}")

        # Status-dependent ending
        if r.status == MediaStatus.AVAILABLE:
            parts.append("Already in your library.")
        elif r.status == MediaStatus.PARTIALLY_AVAILABLE:
            parts.append("Some of this is already available. Want me to request the rest?")
        elif r.status == MediaStatus.PROCESSING:
            parts.append("Already requested, it's on its way.")
        elif r.status == MediaStatus.PENDING:
            parts.append("Already requested, waiting on approval.")
        elif r.status == MediaStatus.BLOCKLISTED:
            parts.append("This title is blocklisted.")
        elif r.status == MediaStatus.DELETED:
            parts.append("This was previously deleted. Want me to re-request it?")
        else:
            parts.append("Want me to add it?")

        return " ".join(parts)

    @staticmethod
    def _format_multiple_results(results: list[SearchResult]) -> str:
        """Format multiple search results as a numbered list for disambiguation."""
        lines: list[str] = []
        for i, r in enumerate(results, 1):
            year = f" ({r.year})" if r.year else ""
            type_str = "TV" if r.media_type == "tv" else "Movie"

            overview = ""
            if r.overview:
                overview = ActionExecutor._truncate(r.overview, 100).rstrip(".")
                overview = f" — {overview}."

            entry = f"{i}. {r.title}{year} [{type_str}]{overview}"

            rating_str = ActionExecutor._format_rating_str(r)
            if rating_str:
                entry += f" {rating_str}"

            if r.status == MediaStatus.AVAILABLE:
                entry += " (already in library)"
            elif r.status in (MediaStatus.PROCESSING, MediaStatus.PENDING):
                entry += " (already requested)"

            lines.append(entry)

        lines.append("\nWhich one?")
        return "\n".join(lines)

    @staticmethod
    def _format_recommendations(
        results: list[SearchResult], similar_to: str | None = None
    ) -> str:
        """Format recommendation results as a casual text message."""
        if similar_to:
            header = f"If you liked {similar_to}, check these out:"
        else:
            header = "Here are some picks:"
        lines = [header]

        for i, r in enumerate(results, 1):
            year = f" ({r.year})" if r.year else ""
            type_str = "TV" if r.media_type == "tv" else "Movie"

            overview = ""
            if r.overview:
                overview = ActionExecutor._truncate(r.overview, 120).rstrip(".")
                overview = f" — {overview}."

            entry = f"{i}. {r.title}{year} [{type_str}]{overview}"

            rating_str = ActionExecutor._format_rating_str(r)
            if rating_str:
                entry += f" {rating_str}"

            if r.trailer_url:
                entry += f" Trailer: {r.trailer_url}"

            if r.status == MediaStatus.AVAILABLE:
                entry += " (already in library)"

            lines.append(entry)

        lines.append("\nWant me to add any of these?")
        return "\n".join(lines)

    async def _handle_weather(self, decision: LLMDecision, sender_phone: str) -> str:
        """Fetch weather/pollen data and format directly."""
        # Check if user specifically asked about pollen/allergies
        history = await self.db.get_history(sender_phone)
        last_user_msg = ""
        for entry in reversed(history):
            if entry.role == "user":
                last_user_msg = entry.content.lower()
                break
        pollen_specific = any(kw in last_user_msg for kw in ("pollen", "allerg"))
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                weather, pollen = await asyncio.gather(
                    get_weather(self.settings, client),
                    get_pollen(self.settings, client, pollen_specific=pollen_specific),
                )
        except Exception as e:
            log.error("Weather fetch failed: %s", e)
            return "Couldn't get weather data right now."

        if not weather and not pollen:
            return "Couldn't get weather data right now."

        parts: list[str] = []
        if weather:
            parts.append(weather)
        if pollen:
            parts.append(pollen)

        data = "\n".join(parts)
        await self.db.add_history(sender_phone, "context", f"[Weather data: {data}]")
        return await self._llm_respond(sender_phone, fallback=data)

    async def _handle_check_status(self, decision: LLMDecision, sender_phone: str) -> str:
        """Check pending/processing requests, store as context, let LLM respond."""
        try:
            counts = await self.seerr.get_request_count()
            has_activity = (
                counts.get("pending", 0)
                or counts.get("processing", 0)
                or counts.get("available", 0)
            )

            if not has_activity:
                return "No pending or in-progress requests."

            tasks = []
            fetch_processing = counts.get("processing", 0) > 0
            fetch_pending = counts.get("pending", 0) > 0

            if fetch_processing:
                tasks.append(self.seerr.get_processing())
            if fetch_pending:
                tasks.append(self.seerr.get_pending())
            tasks.append(self.seerr.get_recently_added(take=3))

            results = await asyncio.gather(*tasks)
            idx = 0

            processing = results[idx] if fetch_processing else []
            if fetch_processing:
                idx += 1
            pending = results[idx] if fetch_pending else []
            if fetch_pending:
                idx += 1
            recently_added = results[idx]

            # Resolve titles concurrently
            all_reqs = list(processing[:5]) + list(pending[:5])
            if all_reqs:
                titles = await asyncio.gather(
                    *[_resolve_request_title(req, self.seerr) for req in all_reqs]
                )
            else:
                titles = []
            proc_count = min(len(processing), 5)

            lines: list[str] = []
            if processing:
                proc_titles = [t for t in titles[:proc_count] if t != "Unknown"]
                if proc_titles:
                    lines.append("Downloading: " + ", ".join(proc_titles))
            if pending:
                pend_titles = [t for t in titles[proc_count:] if t != "Unknown"]
                if pend_titles:
                    lines.append("Waiting for approval: " + ", ".join(pend_titles))
            if recently_added:
                added_titles = [item["title"] for item in recently_added[:3]]
                lines.append("Recently added: " + ", ".join(added_titles))

            if not lines:
                return "No pending or in-progress requests."

            data = "\n".join(lines)
            await self.db.add_history(sender_phone, "context", f"[Request status: {data}]")
            return await self._llm_respond(sender_phone, fallback=data)
        except Exception as e:
            log.error("Status check failed: %s", e)
            return "Something went wrong, try again in a sec."

    async def _handle_recent(self, decision: LLMDecision, sender_phone: str) -> str:
        """Check recently added media and pending requests."""
        try:
            lines: list[str] = []
            # Recently added to library
            added = await self.seerr.get_recently_added(take=5)
            if added:
                movies = [r["title"] for r in added if r["mediaType"] == "movie"]
                shows = [r["title"] for r in added if r["mediaType"] == "tv"]
                if movies:
                    lines.append("Recently added movies: " + ", ".join(movies))
                if shows:
                    lines.append("Recently added shows: " + ", ".join(shows))
            # Pending requests
            pending = await self.seerr.get_pending()
            if pending:
                resolved = await asyncio.gather(
                    *[_resolve_request_title(req, self.seerr) for req in pending[:5]]
                )
                pending_titles = [t for t in resolved if t != "Unknown"]
                if pending_titles:
                    lines.append("Pending requests: " + ", ".join(pending_titles))

            if not lines:
                return "Nothing new right now."

            data = "\n".join(lines)
            await self.db.add_history(sender_phone, "context", f"[{data}]")
            return await self._llm_respond(sender_phone, fallback=data)
        except Exception as e:
            log.error("Recent media fetch failed: %s", e)
            return "Something went wrong, try again in a sec."

    async def _handle_recommend(self, decision: LLMDecision, sender_phone: str) -> str:
        """Discover movies/shows by genre, year, trending, or similar to a title."""
        import re

        query = (decision.query or decision.message or "").lower()

        # Check for "similar to X" / "something like X" / "like X" / "more like X"
        similar_match = re.match(
            r"(?:similar to|something like|more like|like)\s+(.+)",
            query,
        )
        if similar_match:
            title = similar_match.group(1).strip()
            try:
                search_results = await self.seerr.search(title)
            except SeerrError as e:
                log.error("Similar-to search failed: %s", e)
                search_results = []

            if search_results:
                base = search_results[0]
                try:
                    results = await self.seerr.get_recommendations(base.media_type, base.tmdb_id)
                    if not results:
                        results = await self.seerr.get_similar(base.media_type, base.tmdb_id)
                except SeerrError as e:
                    log.error("Recommendations/similar lookup failed: %s", e)
                    results = []

                if results:
                    # Fetch trailers and ratings for results (concurrently)
                    top = results[:3]
                    trailer_tasks = [
                        self.seerr.get_trailer(r.media_type, r.tmdb_id)
                        for r in top
                    ]
                    rating_tasks = [
                        self.seerr.get_ratings(r.media_type, r.tmdb_id)
                        for r in top
                    ]
                    all_fetched = await asyncio.gather(*trailer_tasks, *rating_tasks)
                    trailers = all_fetched[:len(top)]
                    ratings = all_fetched[len(top):]
                    for i, trailer_url in enumerate(trailers):
                        if trailer_url and i < len(results):
                            results[i].trailer_url = trailer_url
                    for i, rating_dict in enumerate(ratings):
                        if rating_dict and i < len(results):
                            self._apply_ratings(results[i], rating_dict)

                    # Send poster(s)
                    if self.sender and self.posters:
                        if len(results) > 1:
                            results_with_posters = [r for r in results if r.poster_path]
                            if results_with_posters:
                                results = results_with_posters
                            await self._send_posters(sender_phone, results)
                        else:
                            await self._send_single_poster(sender_phone, results[0])

                    # Store context and let LLM craft the response
                    await self.db.add_history(
                        sender_phone, "context",
                        f"[Recommendations similar to {base.title}]",
                    )
                    context = format_search_results(results)
                    await self.db.add_history(sender_phone, "context", context)

                    return await self._llm_respond(
                        sender_phone,
                        fallback=self._format_recommendations(results, similar_to=base.title),
                    )

            # Fall through to genre/trending logic if search or recommendations failed

        # Determine media type from query
        want_movie = any(w in query for w in ("movie", "film"))
        want_tv = any(w in query for w in ("tv", "show", "series"))
        # If neither specified, do both
        want_both = not want_movie and not want_tv

        # Extract year (4-digit number)
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", query)
        year = int(year_match.group(1)) if year_match else None

        # Find genre keyword using dynamic genre maps
        genre_keyword: str | None = None
        try:
            movie_genres = await self.seerr.get_genre_map("movie")
            tv_genres = await self.seerr.get_genre_map("tv")
        except Exception:
            movie_genres = {}
            tv_genres = {}

        all_genre_names = sorted(
            set(list(movie_genres.keys()) + list(tv_genres.keys())),
            key=lambda g: -len(g),
        )
        for genre in all_genre_names:
            if genre in query:
                genre_keyword = genre
                break

        # Determine if this is a trending request
        is_trending = "trending" in query or (not genre_keyword and not year)

        try:
            results: list[SearchResult] = []
            if is_trending and not genre_keyword and not year:
                results = await self.seerr.discover_trending(take=3)
            elif want_both:
                movie_genre_id = movie_genres.get(genre_keyword) if genre_keyword else None
                tv_genre_id = tv_genres.get(genre_keyword) if genre_keyword else None
                movie_results, tv_results = await asyncio.gather(
                    self.seerr.discover_movies(genre_id=movie_genre_id, year=year, take=2),
                    self.seerr.discover_tv(genre_id=tv_genre_id, year=year, take=2),
                )
                results = (movie_results + tv_results)[:3]
            elif want_movie:
                genre_id = movie_genres.get(genre_keyword) if genre_keyword else None
                results = await self.seerr.discover_movies(genre_id=genre_id, year=year, take=3)
            else:  # want_tv
                genre_id = tv_genres.get(genre_keyword) if genre_keyword else None
                results = await self.seerr.discover_tv(genre_id=genre_id, year=year, take=3)
        except Exception as e:
            log.error("Discover failed: %s", e)
            return "Something went wrong, try again in a sec."

        if not results:
            return "Couldn't find any recommendations for that."

        # Fetch trailers and ratings for top results (concurrently)
        top = results[:3]
        trailer_tasks = [
            self.seerr.get_trailer(r.media_type, r.tmdb_id)
            for r in top
        ]
        rating_tasks = [
            self.seerr.get_ratings(r.media_type, r.tmdb_id)
            for r in top
        ]
        all_results = await asyncio.gather(*trailer_tasks, *rating_tasks)
        trailers = all_results[:len(top)]
        ratings = all_results[len(top):]
        for i, trailer_url in enumerate(trailers):
            if trailer_url and i < len(results):
                results[i].trailer_url = trailer_url
        for i, rating_dict in enumerate(ratings):
            if rating_dict and i < len(results):
                self._apply_ratings(results[i], rating_dict)

        # Send poster(s)
        if self.sender and self.posters:
            if len(results) > 1:
                # Filter to results with posters so collage indices match text list
                results_with_posters = [r for r in results if r.poster_path]
                if results_with_posters:
                    results = results_with_posters
                await self._send_posters(sender_phone, results)
            else:
                await self._send_single_poster(sender_phone, results[0])

        # Store context and let LLM craft the response
        context = format_search_results(results)
        await self.db.add_history(sender_phone, "context", context)

        return await self._llm_respond(
            sender_phone,
            fallback=self._format_recommendations(results),
        )

    async def _handle_remember(self, decision: LLMDecision, sender_phone: str) -> str:
        """Store a user fact/preference."""
        fact = decision.fact or decision.message
        if not fact:
            return "What should I remember?"
        await self.db.add_fact(sender_phone, fact)
        return decision.message or f"Got it, I'll remember that."

    async def _handle_forget(self, decision: LLMDecision, sender_phone: str) -> str:
        """Remove a stored user fact/preference."""
        keyword = decision.fact or decision.message
        if not keyword:
            return "What should I forget?"
        removed = await self.db.remove_fact(sender_phone, keyword)
        if removed:
            return decision.message or "Done, forgot it."
        return decision.message or "I don't have anything like that saved."

    async def _handle_request(self, decision: LLMDecision, sender_phone: str) -> str:
        """Execute a request action: add media to Seerr with dedup check."""
        if not decision.tmdb_id or not decision.media_type:
            return "I need to know which title to request. Can you search first?"

        # Check if already requested/available before making a duplicate request
        title = "this"
        try:
            detail = await self.seerr.get_media_status(decision.media_type, decision.tmdb_id)
            if detail:
                title = detail.get("title") or detail.get("name", "this")
                media_info = detail.get("mediaInfo")
                if media_info:
                    raw_status = media_info.get("status", 0)
                    try:
                        status = MediaStatus(raw_status)
                    except ValueError:
                        status = MediaStatus.UNKNOWN

                    if status == MediaStatus.AVAILABLE:
                        await self._store_request_context(sender_phone, title, decision)
                        return f"{title} is already in your library."
                    elif status == MediaStatus.PROCESSING:
                        await self._store_request_context(sender_phone, title, decision)
                        return f"{title} is already requested and on its way."
                    elif status == MediaStatus.PENDING:
                        await self._store_request_context(sender_phone, title, decision)
                        return f"{title} is already requested, waiting on approval."
        except Exception as e:
            log.debug("Pre-request status check failed (proceeding anyway): %s", e)

        try:
            await self.seerr.request_media(decision.media_type, decision.tmdb_id)
            await self._store_request_context(sender_phone, title, decision)
            return decision.message
        except Exception as e:
            log.error("Request failed (type=%s tmdb=%s): %s", decision.media_type, decision.tmdb_id, e)
            return "Something went wrong, try again in a sec."

    async def _store_request_context(
        self, sender_phone: str, title: str, decision: LLMDecision
    ) -> None:
        """Store context about the requested/discussed title for future narrowing."""
        ctx = f"[Last discussed: {title} tmdb:{decision.tmdb_id} {decision.media_type}]"
        await self.db.add_history(sender_phone, "context", ctx)
