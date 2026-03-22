"""Actions package — ActionExecutor and handler dispatch."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from zoneinfo import ZoneInfo

from ..config import Settings
from ..llm import LLMClient
from ..memory import UserMemory
from ..monitor import MessageMonitor
from ..posters import PosterHandler
from ..seerr import SeerrClient
from ..sender import MessageSender
from ..types import Action, HistoryEntry, LLM_RESPOND_SCHEMA, LLMDecision, SearchResult
from ._base import ERROR_GENERIC, apply_ratings

# Handler imports
from .search import handle_search
from .request import handle_request
from .recent import handle_recent
from .recommend import handle_recommend
from .memory import handle_remember, handle_forget

log = logging.getLogger(__name__)

# Re-export for public API compatibility
__all__ = ["ActionExecutor", "ERROR_GENERIC"]


def _escape_xml_delimiters(text: str) -> str:
    """Escape angle brackets to prevent prompt injection via fake XML tags."""
    return text.replace("<", "&lt;").replace(">", "&gt;")


class ActionExecutor:
    def __init__(
        self,
        seerr: SeerrClient,
        llm: LLMClient,
        sender: MessageSender | None,
        posters: PosterHandler | None,
        memory: UserMemory,
        monitor: MessageMonitor | None,
        settings: Settings,
    ) -> None:
        self.seerr = seerr
        self.llm = llm
        self.sender = sender
        self.posters = posters
        self.memory = memory
        self.monitor = monitor
        self.settings = settings
        # Track recently sent poster tmdb_ids per phone to avoid re-sending
        self._sent_posters: dict[str, set[int]] = {}
        # In-memory context buffer (search results, API data) per sender
        self._context: dict[str, list[tuple[float, str]]] = {}
        # Most recently discussed title per sender (for pronoun resolution)
        # Values: {"title": str, "tmdb_id": int, "media_type": str}
        self._last_topic: dict[str, dict] = {}
        # Session boundary timestamps (set by "new"/"reset")
        self._session_start: dict[str, float] = {}
        # CLI-mode message history (no chat.db available)
        self._cli_history: dict[str, list[HistoryEntry]] = {}
        # Cached base prompt per sender (within a single handle_message cycle)
        self._prompt_cache: dict[str, str] = {}
        # Context count at cache time (to append only new entries in _llm_respond)
        self._prompt_cache_ctx_count: dict[str, int] = {}
        # Whether the most recent prompt had a conversation gap (stale topic)
        self._has_gap: dict[str, bool] = {}

    # ── Context buffer helpers ───────────────────────────────────

    def _add_context(self, sender: str, text: str) -> None:
        """Add a context entry to the in-memory buffer."""
        entries = self._context.setdefault(sender, [])
        entries.append((time.time(), text))
        if len(entries) > 50:
            self._context[sender] = entries[-50:]

    def _clear_context(self, sender: str) -> None:
        """Clear context buffer for a sender."""
        self._context.pop(sender, None)

    def get_context_entries(self, sender: str) -> list[tuple[float, str]]:
        """Return context entries for a sender (for handler use)."""
        return self._context.get(sender, [])

    # ── CLI history helper ───────────────────────────────────────

    def _track_cli(self, sender: str, role: str, content: str) -> None:
        """Append to CLI-mode history (no-op in daemon mode)."""
        if self.monitor is not None:
            return
        self._cli_history.setdefault(sender, []).append(
            HistoryEntry(role=role, content=content, timestamp=time.time())
        )

    # ── Main entry point ─────────────────────────────────────────

    async def handle_message(
        self,
        sender_phone: str,
        text: str,
    ) -> str:
        """Process a user message through the full LLM -> action -> response loop."""
        self._track_cli(sender_phone, "user", text)

        # Build prompt from conversation history (cache for _llm_respond reuse)
        prompt = await self._build_prompt(sender_phone)
        # Mark the current message with a strong delimiter so the LLM focuses on it
        # (chat.db history can be noisy with old conversations)
        prompt += (
            "\n---\n"
            "[CURRENT MESSAGE — respond to this. Everything above is history for context only.]\n"
            f"<current_user_message>{_escape_xml_delimiters(text)}</current_user_message>"
        )
        # Inject last-discussed topic so the LLM knows what "it" refers to
        # Includes tmdb_id + media_type so the LLM can request directly
        # Skip if a conversation gap was detected (stale topic from old conversation)
        topic = self._last_topic.get(sender_phone)
        if topic and not self._has_gap.get(sender_phone, False):
            safe_title = _escape_xml_delimiters(topic["title"])
            prompt += f"\n[Last discussed title: {safe_title} tmdb:{topic['tmdb_id']} {topic['media_type']}]"
        self._prompt_cache[sender_phone] = prompt
        self._prompt_cache_ctx_count[sender_phone] = len(self._context.get(sender_phone, []))

        # Get LLM decision
        try:
            decision, meta = await self.llm.decide(prompt)
            log.info("LLM action=%s query=%s tmdb_id=%s", decision.action.value, decision.query or "-", decision.tmdb_id or "-")
        except Exception as e:
            log.error("LLM call failed: %s", e)
            self._prompt_cache.pop(sender_phone, None)
            self._prompt_cache_ctx_count.pop(sender_phone, None)
            return "Server error, please try again later."

        # Execute the action
        try:
            response = await self._execute(decision, sender_phone, text)
        finally:
            # Clear prompt cache after message is fully handled (or on error)
            self._prompt_cache.pop(sender_phone, None)
            self._prompt_cache_ctx_count.pop(sender_phone, None)
        self._track_cli(sender_phone, "assistant", response)

        return response

    async def _llm_respond(self, sender_phone: str, fallback: str = "", intent: str | None = None) -> tuple[str, bool]:
        """Build prompt with current context and let the LLM generate a response.

        Reuses the cached base prompt from handle_message when available,
        appending only the new context entries added by the handler.

        ``intent`` controls the LLM instruction style:
        - "search": focus on top result, describe it
        - "disambiguate": present numbered options, ask which one
        - "recommend": present numbered picks, ask if they want to add any
        - "recent": present server state (available + requested/downloading)
        - None: generic summarize
        """
        cached = self._prompt_cache.get(sender_phone)
        if cached is not None:
            # Append only context entries added since the cache was built
            cache_count = self._prompt_cache_ctx_count.get(sender_phone, 0)
            new_ctx = self._context.get(sender_phone, [])[cache_count:]
            extra = "\n".join(f"<context>{text}</context>" for _ts, text in new_ctx)
            prompt = cached + "\n" + extra if extra else cached
        else:
            prompt = await self._build_prompt(sender_phone)
        base = (
            "The results above have already been fetched. "
            "Do NOT search, recommend, or take any action. "
            "Use action=reply. If the user is confirming they want to add a title "
            "shown in the results, you may use action=request with the correct tmdb_id and media_type. "
            "Set multiple_results=true if you are presenting multiple numbered options, "
            "false if focusing on a single title."
        )
        if intent == "search":
            instruction = (
                f"{base} Present the search results to the user. "
                "If there's one clear match for what the user asked, focus on it — "
                "describe it, mention ratings, and note its status. "
                "If there are multiple plausible matches, present them numbered and ask which one. "
                "Use your judgment based on the query and results."
            )
        elif intent == "recommend":
            instruction = (
                f"{base} Present these as numbered picks for the user to browse. "
                "Mention ALL results shown with brief descriptions. "
                "End with asking if they want to add any."
            )
        elif intent == "recent":
            instruction = (
                f"{base} Present the server state. Group by what's available and what's been requested. "
                "Use the exact status from the results. Include brief descriptions."
            )
        elif intent == "dedup":
            instruction = (
                f"{base} The user wanted to add this title but it's already on the server. "
                "Inform them of its current status naturally."
            )
        else:
            instruction = f"{base} Write a reply message summarizing the results."
        prompt += f"\n---\n[INSTRUCTION: {instruction}]"
        try:
            decision, meta = await self.llm.decide(prompt, schema=LLM_RESPOND_SCHEMA)
            log.debug("LLM respond: action=%s message=%s", decision.action.value, (decision.message or "")[:100])
            multi = decision.multiple_results
            # Allow request as a follow-up (user confirms a search result)
            if decision.action == Action.REQUEST and decision.tmdb_id:
                return await handle_request(self, decision, sender_phone), multi
            # Only accept reply — any other action means the LLM is confused
            if decision.action == Action.REPLY and decision.message and len(decision.message.strip()) > 2:
                return decision.message, multi
            log.warning(
                "LLM response returned action=%s message=%r instead of reply, using fallback",
                decision.action.value, (decision.message or "")[:100],
            )
            return fallback or ERROR_GENERIC, False
        except Exception as e:
            log.error("LLM response call failed: %s", e)
            return fallback or ERROR_GENERIC, False

    async def _build_prompt(self, sender_phone: str) -> str:
        """Build the full prompt from memory + chat.db messages + context buffer."""
        parts: list[str] = []

        # Time context
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.datetime.now(tz)
        time_str = now.strftime("%A %B %-d, %Y %-I:%M %p %Z")
        parts.append(f"<context>[Current time: {time_str}]</context>")

        # Per-user memory (markdown file) — run in thread to avoid blocking event loop
        memory_content = await asyncio.to_thread(self.memory.load, sender_phone)
        if memory_content:
            parts.append(f"<memory>\n{memory_content.strip()}\n</memory>")

        # Get messages: chat.db in daemon mode, _cli_history in CLI mode
        if self.monitor is not None:
            messages = await self.monitor.get_recent_messages(
                sender_phone,
                limit=self.settings.history_window,
            )
        else:
            messages = list(self._cli_history.get(sender_phone, []))

        # Filter by session start (messages after "new"/"reset" only)
        session_start = self._session_start.get(sender_phone)
        if session_start is not None:
            messages = [m for m in messages if m.timestamp >= session_start]

        # Merge messages + context buffer by timestamp (evict stale context)
        context_entries = self._context.get(sender_phone, [])
        gap_seconds = self.settings.conversation_gap_hours * 3600
        now = time.time()
        timeline: list[tuple[float, str, str]] = []  # (timestamp, tag, content)
        for m in messages:
            timeline.append((m.timestamp, m.role, m.content))
        for ts, text in context_entries:
            if now - ts > gap_seconds * 2:
                continue  # Evict context entries older than 2x gap threshold
            if session_start is None or ts >= session_start:
                timeline.append((ts, "context", text))
        timeline.sort(key=lambda x: x[0])

        msg_count = sum(1 for _, tag, _ in timeline if tag in ("user", "assistant"))
        ctx_count = sum(1 for _, tag, _ in timeline if tag == "context")
        log.debug(
            "Prompt for %s: %d messages, %d context entries, memory=%s",
            sender_phone, msg_count, ctx_count, bool(memory_content),
        )

        # Render with gap markers for 2+ hour gaps
        prev_ts = 0.0
        has_gap = False
        for ts, tag, content in timeline:
            if prev_ts > 0 and ts - prev_ts >= gap_seconds:
                parts.append("[The above messages are from a previous conversation. Treat the following as a new, separate conversation — do not assume topic continuity.]")
                has_gap = True
            safe = _escape_xml_delimiters(content)
            parts.append(f"<{tag}>{safe}</{tag}>")
            prev_ts = ts

        self._has_gap[sender_phone] = has_gap

        return "\n".join(parts)

    async def _enrich_results(
        self, results: list[SearchResult], *, enrich_downloads: bool = False
    ) -> None:
        """Fetch trailers, ratings, air dates, and optionally download progress for results."""
        top = results
        detail_tasks = [self.seerr.get_detail_extras(r.media_type, r.tmdb_id) for r in top]
        rating_tasks = [self.seerr.get_ratings(r.media_type, r.tmdb_id) for r in top]
        n = len(top)
        all_results = await asyncio.gather(*detail_tasks, *rating_tasks)
        details = all_results[:n]
        ratings = all_results[n:n * 2]
        for i, extras in enumerate(details):
            if i >= len(results):
                break
            if extras.get("trailer"):
                results[i].trailer_url = extras["trailer"]
            if extras.get("air_date"):
                results[i].next_air_date = extras["air_date"]
            if enrich_downloads and extras.get("download_progress"):
                results[i].download_progress = extras["download_progress"]
        for i, rating_dict in enumerate(ratings):
            if rating_dict and i < len(results):
                apply_ratings(results[i], rating_dict)

    async def _execute(
        self, decision: LLMDecision, sender_phone: str, user_text: str = ""
    ) -> str:
        """Execute an LLM decision and return the response text."""
        if decision.action == Action.SEARCH:
            return await handle_search(self, decision, sender_phone, user_text)
        elif decision.action == Action.REQUEST:
            return await handle_request(self, decision, sender_phone)
        elif decision.action == Action.RECENT:
            return await handle_recent(self, decision, sender_phone)
        elif decision.action == Action.RECOMMEND:
            return await handle_recommend(self, decision, sender_phone)
        elif decision.action == Action.REMEMBER:
            return await handle_remember(self, decision, sender_phone)
        elif decision.action == Action.FORGET:
            return await handle_forget(self, decision, sender_phone)
        else:  # REPLY
            if decision.message and decision.message.strip():
                return decision.message.strip()
            executor._add_context(sender_phone, "[Reply action: LLM returned empty message]")
            return (await self._llm_respond(sender_phone, intent=None))[0]

    async def _prepare_poster(self, result: SearchResult):
        """Download a single poster image. Returns local path or None."""
        if not self.posters:
            return None
        try:
            return await self.posters.get_single_poster(result)
        except Exception as e:
            log.error("Failed to download poster: %s", e)
            return None

    async def _send_with_poster(
        self,
        sender_phone: str,
        display_results: list[SearchResult],
        *,
        fallback: str,
        intent: str,
        skip_poster: bool = False,
    ) -> str:
        """Run LLM response in parallel with poster download, then send posters.

        Downloads raw posters in parallel with the LLM call. After the LLM
        responds, filters posters to only those the LLM actually mentioned
        (by title match), renumbered sequentially. This prevents sending
        posters for results the LLM chose to omit.
        """
        sent_ids = self._sent_posters.get(sender_phone, set())
        all_shown = all(r.tmdb_id in sent_ids for r in display_results) if display_results else True

        # Start raw poster downloads in parallel with LLM (no numbering yet)
        download_task = None
        if not skip_poster and not all_shown and self.posters and self.sender and display_results:
            download_task = asyncio.create_task(self.posters.download_all(display_results))

        response, multi = await self._llm_respond(sender_phone, fallback=fallback, intent=intent)

        if download_task:
            try:
                raw_posters = await download_task
            except Exception as e:
                log.error("Poster download failed: %s", e)
                raw_posters = []
            if raw_posters and self.sender:
                # Filter posters to only results the LLM actually mentioned
                raw_posters = self._match_posters_to_response(
                    response, display_results, raw_posters,
                )
                if len(raw_posters) > 1:
                    if multi:
                        # Numbered list — add number overlays
                        numbered = self.posters.number_posters(raw_posters)
                        if numbered:
                            await self.sender.send_images(sender_phone, [str(p) for p in numbered])
                    else:
                        # Multiple titles mentioned but not a numbered list — send unnumbered
                        await self.sender.send_images(
                            sender_phone, [str(path) for _, path in raw_posters],
                        )
                else:
                    # Single result — send without numbering
                    _, first_path = raw_posters[0]
                    await self.sender.send_image(sender_phone, str(first_path))
                await self.sender.start_typing(sender_phone)
                new_sent = self._sent_posters.setdefault(sender_phone, set())
                for r in display_results:
                    new_sent.add(r.tmdb_id)

        return response

    @staticmethod
    def _match_posters_to_response(
        response: str,
        display_results: list[SearchResult],
        raw_posters: list[tuple[int, Path]],
    ) -> list[tuple[int, Path]]:
        """Filter and renumber posters to only those the LLM mentioned.

        Matches display_results titles against the LLM response text, then
        returns only matching posters renumbered sequentially (1, 2, 3...).
        Falls back to the full list if no titles match (safety net).
        """
        response_lower = response.lower()
        poster_by_pos = {pos: path for pos, path in raw_posters}

        # Collect matches with their mention position in the response
        # so posters are ordered the way the LLM presented them
        matches: list[tuple[int, Path]] = []  # (mention_idx, poster_path)
        for i, r in enumerate(display_results):
            pos = i + 1  # raw_posters uses 1-based indexing
            if pos not in poster_by_pos:
                continue
            title_lower = r.title.lower()
            idx = -1
            if r.year:
                idx = response_lower.find(f"{title_lower} ({r.year})")
            if idx < 0:
                # Fall back to title-only (handles LLM rephrasing)
                idx = response_lower.find(title_lower)
            if idx >= 0:
                matches.append((idx, poster_by_pos[pos]))

        if not matches:
            return raw_posters  # fallback: can't determine, send all
        # Sort by mention position so poster order matches LLM's text
        matches.sort(key=lambda x: x[0])
        return [(i + 1, path) for i, (_, path) in enumerate(matches)]

    async def _store_request_context(
        self, sender_phone: str, title: str, decision: LLMDecision
    ) -> None:
        """Store context about the requested/discussed title for future narrowing."""
        ctx = f"[Last discussed: {title} tmdb:{decision.tmdb_id} {decision.media_type}]"
        self._add_context(sender_phone, ctx)
        self._last_topic[sender_phone] = {
            "title": title,
            "tmdb_id": decision.tmdb_id,
            "media_type": decision.media_type,
        }
