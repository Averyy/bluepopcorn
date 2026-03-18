"""Actions package — ActionExecutor and handler dispatch."""

from __future__ import annotations

import asyncio
import datetime
import logging
from zoneinfo import ZoneInfo

from ..config import Settings
from ..db import BotDatabase
from ..llm import LLMClient
from ..posters import PosterHandler
from ..seerr import SeerrClient, parse_download_progress
from ..sender import MessageSender
from ..types import Action, LLMDecision, MediaStatus, SearchResult
from ._base import ERROR_GENERIC, StatusData, resolve_request_title, apply_ratings

# Handler imports
from .search import handle_search
from .request import handle_request
from .status import handle_check_status
from .weather import handle_weather
from .recent import handle_recent
from .recommend import handle_recommend
from .memory import handle_remember, handle_forget

log = logging.getLogger(__name__)

# Re-export for public API compatibility
__all__ = ["ActionExecutor", "ERROR_GENERIC"]


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
        # Track recently sent poster tmdb_ids per phone to avoid re-sending
        self._sent_posters: dict[str, set[int]] = {}

    async def handle_message(
        self,
        sender_phone: str,
        text: str,
    ) -> str:
        """Process a user message through the full LLM -> action -> response loop."""
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
                data = await self._fetch_status_data()
                if not data.has_activity:
                    return "No pending or in-progress requests."

                lines: list[str] = []
                if data.processing_titles:
                    lines.append("Downloading:")
                    for title in data.processing_titles:
                        lines.append(f"- {title}")
                if data.pending_titles:
                    lines.append("Waiting for approval:")
                    for title in data.pending_titles:
                        lines.append(f"- {title}")
                if data.recently_added:
                    lines.append("Recently added:")
                    for title in data.recently_added:
                        lines.append(f"- {title}")

                return "\n".join(lines) if lines else "No pending or in-progress requests."
            except Exception as e:
                log.error("Status check failed: %s", e)
                return ERROR_GENERIC

        if command == "new":
            if sender_phone:
                await self.db.clear_history(sender_phone)
                self._sent_posters.pop(sender_phone, None)
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

    async def _fetch_status_data(self) -> StatusData:
        """Fetch processing/pending/recently-added data from Seerr."""
        counts = await self.seerr.get_request_count()
        fetch_processing = counts.get("processing", 0) > 0
        fetch_pending = counts.get("pending", 0) > 0

        # Gather only the requests that exist
        coros = {}
        if fetch_processing:
            coros["processing"] = self.seerr.get_processing()
        if fetch_pending:
            coros["pending"] = self.seerr.get_pending()
        coros["recent"] = self.seerr.get_recently_added(take=3)

        keys = list(coros.keys())
        results = await asyncio.gather(*coros.values())
        fetched = dict(zip(keys, results))

        processing = fetched.get("processing", [])
        pending = fetched.get("pending", [])
        recently_added = fetched.get("recent", [])

        # Resolve titles concurrently
        all_reqs = list(processing[:5]) + list(pending[:5])
        if all_reqs:
            titles = await asyncio.gather(
                *[resolve_request_title(req, self.seerr) for req in all_reqs]
            )
        else:
            titles = []
        proc_count = min(len(processing), 5)

        proc_titles = [t for t in titles[:proc_count] if t != "Unknown"]
        pend_titles = [t for t in titles[proc_count:] if t != "Unknown"]
        added_titles = [item["title"] for item in recently_added[:3]]

        return StatusData(
            processing_titles=proc_titles,
            pending_titles=pend_titles,
            recently_added=added_titles,
        )

    async def _llm_respond(self, sender_phone: str, fallback: str = "") -> str:
        """Build prompt with current history and let the LLM generate a response."""
        prompt = await self._build_prompt(sender_phone)
        prompt += "\n<context>[The results above have already been fetched and sent. Your job now is only to write the reply message. Respond with action=reply.]</context>"
        try:
            decision, meta = await self.llm.decide(prompt)
            log.debug("LLM respond: action=%s message=%s", decision.action.value, (decision.message or "")[:100])
            # Allow request as a follow-up (user confirms a search result)
            if decision.action == Action.REQUEST and decision.tmdb_id:
                return await handle_request(self, decision, sender_phone)
            # Only accept reply — any other action means the LLM is confused
            if decision.action == Action.REPLY and decision.message and len(decision.message.strip()) > 2:
                return decision.message
            log.warning(
                "LLM response returned action=%s message=%r instead of reply, using fallback",
                decision.action.value, (decision.message or "")[:100],
            )
            return fallback or ERROR_GENERIC
        except Exception as e:
            log.error("LLM response call failed: %s", e)
            return fallback or ERROR_GENERIC

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

    async def _enrich_results(
        self, results: list[SearchResult], *, enrich_downloads: bool = False
    ) -> None:
        """Fetch trailers, ratings, and optionally download progress for top results."""
        top = results[:3]
        trailer_tasks = [self.seerr.get_trailer(r.media_type, r.tmdb_id) for r in top]
        rating_tasks = [self.seerr.get_ratings(r.media_type, r.tmdb_id) for r in top]
        progress_tasks = []
        if enrich_downloads:
            progress_tasks = [
                self._enrich_download_progress(r) for r in top
                if r.status == MediaStatus.PROCESSING and not r.download_progress
            ]
        all_results = await asyncio.gather(*trailer_tasks, *rating_tasks, *progress_tasks)
        trailers = all_results[:len(top)]
        ratings = all_results[len(top):len(top) * 2]
        for i, trailer_url in enumerate(trailers):
            if trailer_url and i < len(results):
                results[i].trailer_url = trailer_url
        for i, rating_dict in enumerate(ratings):
            if rating_dict and i < len(results):
                apply_ratings(results[i], rating_dict)

    async def _execute(self, decision: LLMDecision, sender_phone: str) -> str:
        """Execute an LLM decision and return the response text."""
        if decision.action == Action.SEARCH:
            return await handle_search(self, decision, sender_phone)
        elif decision.action == Action.REQUEST:
            return await handle_request(self, decision, sender_phone)
        elif decision.action == Action.CHECK_STATUS:
            return await handle_check_status(self, decision, sender_phone)
        elif decision.action == Action.WEATHER:
            return await handle_weather(self, decision, sender_phone)
        elif decision.action == Action.RECENT:
            return await handle_recent(self, decision, sender_phone)
        elif decision.action == Action.RECOMMEND:
            return await handle_recommend(self, decision, sender_phone)
        elif decision.action == Action.REMEMBER:
            return await handle_remember(self, decision, sender_phone)
        elif decision.action == Action.FORGET:
            return await handle_forget(self, decision, sender_phone)
        else:  # REPLY
            return decision.message

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
                self._sent_posters.setdefault(phone, set()).add(result.tmdb_id)
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
            # Track all tmdb_ids that were shown
            sent = self._sent_posters.setdefault(phone, set())
            for r in results:
                sent.add(r.tmdb_id)
        except Exception as e:
            log.error("Failed to send poster: %s", e)

    async def _send_result_posters(
        self, phone: str, results: list[SearchResult]
    ) -> list[SearchResult]:
        """Send poster(s) for results and restart typing indicator."""
        if not self.sender or not self.posters:
            return results
        if len(results) > 1:
            results_with_posters = [r for r in results if r.poster_path]
            if results_with_posters:
                results = results_with_posters
            await self._send_posters(phone, results)
        else:
            await self._send_single_poster(phone, results[0])
        await self.sender.start_typing(phone)
        return results

    async def _enrich_download_progress(self, result: SearchResult) -> None:
        """Fetch download progress from the detail endpoint for a PROCESSING result."""
        try:
            detail = await self.seerr.get_media_status(result.media_type, result.tmdb_id)
            if not detail:
                return
            media_info = detail.get("mediaInfo") or {}
            progress = parse_download_progress(media_info)
            if progress:
                result.download_progress = progress
        except Exception as e:
            log.debug("Download progress enrichment failed for %s/%d: %s",
                      result.media_type, result.tmdb_id, e)

    async def _store_request_context(
        self, sender_phone: str, title: str, decision: LLMDecision
    ) -> None:
        """Store context about the requested/discussed title for future narrowing."""
        ctx = f"[Last discussed: {title} tmdb:{decision.tmdb_id} {decision.media_type}]"
        await self.db.add_history(sender_phone, "context", ctx)
