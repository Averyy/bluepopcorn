"""Actions package — ActionExecutor and handler dispatch."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import Settings
from ..llm import LLMAuthError, LLMClient
from ..memory import UserMemory
from ..monitor import MessageMonitor
from ..posters import PosterHandler
from ..request_tracker import RequestTracker
from ..seerr import SeerrClient
from ..sender import MessageSender
from ..prompts import (
    CONTEXT_EMPTY_REPLY,
    CONTEXT_SEARCH_BUDGET,
    CONTEXT_SEARCH_REPEAT,
    CONVERSATION_GAP,
    CURRENT_MESSAGE_DELIMITER,
    ERROR_AUTH,
    ERROR_GENERIC,
    INSTRUCTION,
    LAST_DISCUSSED_TITLE,
    TIME_CONTEXT,
)
from ..schemas import RESPOND_SCHEMA, TAG_CONTEXT, TAG_MEMORY
from ..types import Action, HistoryEntry, LLMDecision, SearchResult
from ..utils import mask_phone, neutralize_brackets, normalize_search_query

# Handler imports
from ._base import format_search_results
from .search import handle_search
from .request import handle_request
from .recent import handle_recent
from .recommend import handle_recommend

log = logging.getLogger(__name__)

__all__ = ["ActionExecutor"]


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
        request_tracker: RequestTracker | None = None,
    ) -> None:
        self.seerr = seerr
        self.llm = llm
        self.sender = sender
        self.posters = posters
        self.memory = memory
        self.monitor = monitor
        self.settings = settings
        self.request_tracker = request_tracker
        # Recently sent poster tmdb_ids per phone: {tmdb_id: sent_ts}.
        # Timestamped so suppression expires with the conversation gap —
        # a permanent set means asking about the same movie weeks later
        # never shows its poster again until a daemon restart.
        self._sent_posters: dict[str, dict[int, float]] = {}
        # In-memory context buffer (search results, API data) per sender
        self._context: dict[str, list[tuple[float, str]]] = {}
        # Most recently discussed title per sender (for pronoun resolution)
        # Values: {"title": str, "tmdb_id": int, "media_type": str, "set_ts": float}
        self._last_topic: dict[str, dict] = {}
        # CLI-mode message history (no chat.db available)
        self._cli_history: dict[str, list[HistoryEntry]] = {}
        # Cached base prompt per sender (within a single handle_message cycle)
        self._prompt_cache: dict[str, str] = {}
        # Wall-clock time the cached prompt was built. _llm_respond appends
        # only context entries with ts >= this. A timestamp threshold (not a
        # list index) — _add_context trims the buffer to 50 entries, which
        # shifts indices and made an index-based slice silently drop
        # this-turn results for senders with a full buffer.
        self._prompt_cache_ctx_ts: dict[str, float] = {}
        # Hard ceiling on LLM calls per handle_message cycle. Caps the
        # damage from any future recursive bug — without this, a bad
        # validation/fallback loop can spin until rate-limited.
        self._llm_calls_this_turn: dict[str, int] = {}
        # Wall-clock start of the current turn. Lets handlers tell whether
        # _last_topic was set during this turn (authoritative — came from
        # a real Seerr result just now) vs carried over from a prior turn.
        self._turn_start_ts: dict[str, float] = {}
        # Normalized queries already searched this turn. Re-running an
        # identical search adds no information — the results are already
        # in context — and identical prompts make the LLM repeat the same
        # decision until MAX_LLM_CALLS_PER_TURN aborts the turn.
        self._searched_this_turn: dict[str, set[str]] = {}
        # Search ATTEMPTS this turn (including failed ones). Failed queries
        # are deliberately kept out of _searched_this_turn (a retry after a
        # transient error is legitimate), so the budget must count attempts
        # or a persistently failing query loops to the LLM-call cap.
        self._search_attempts_this_turn: dict[str, int] = {}

    # ── Context buffer helpers ───────────────────────────────────

    def _add_context(self, sender: str, text: str) -> None:
        """Add a context entry to the in-memory buffer.

        Content is stored raw. XML escaping happens at render time in
        _build_prompt (timeline) and _llm_respond (new context entries).
        """
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

    # ── Cost / loop safeguards ────────────────────────────────────
    # Multiple independent safety nets so a single bug can't burn the
    # API budget. Real turns never approach these caps.
    #
    # MAX_LLM_CALLS_PER_TURN: hard ceiling on `llm.decide` calls inside
    # one handle_message cycle. Decide → fallback search → respond →
    # maybe one follow-up request = 4 calls in the worst legitimate
    # case. We allow 6 for headroom.
    #
    # MAX_TURN_WALL_SECONDS: independent wall-clock limit. Even if the
    # call counter is bypassed somehow, the whole cycle aborts. Default
    # llm_timeout is 30s × 6 calls = 3 min worst case; cap at 90s so
    # a single slow call can't run forever and a loop of fast calls
    # also can't sneak past.
    MAX_LLM_CALLS_PER_TURN = 6
    MAX_TURN_WALL_SECONDS = 90.0
    # Distinct Seerr searches allowed per turn. A legitimate turn needs at
    # most two (initial + one genuine refinement); the cap stops loops that
    # dodge the exact-query dedup with endless query variants.
    MAX_SEARCHES_PER_TURN = 3

    def _clear_turn_state(self, sender_phone: str) -> None:
        """Drop all per-turn state after a turn completes or aborts."""
        self._prompt_cache.pop(sender_phone, None)
        self._prompt_cache_ctx_ts.pop(sender_phone, None)
        self._llm_calls_this_turn.pop(sender_phone, None)
        self._turn_start_ts.pop(sender_phone, None)
        self._searched_this_turn.pop(sender_phone, None)
        self._search_attempts_this_turn.pop(sender_phone, None)

    async def handle_message(
        self,
        sender_phone: str,
        text: str,
    ) -> str:
        """Process a user message through the full LLM -> action -> response loop.

        Wrapped in :func:`asyncio.wait_for` so the entire turn aborts at
        ``MAX_TURN_WALL_SECONDS`` regardless of how many LLM calls or
        Seerr requests are in flight.
        """
        try:
            return await asyncio.wait_for(
                self._handle_message_inner(sender_phone, text),
                timeout=self.MAX_TURN_WALL_SECONDS,
            )
        except asyncio.TimeoutError:
            log.error(
                "handle_message timed out after %ss for %s "
                "(message=%r, llm_calls=%d) — likely a recursive bug",
                self.MAX_TURN_WALL_SECONDS,
                mask_phone(sender_phone),
                text[:80],
                self._llm_calls_this_turn.get(sender_phone, 0),
            )
            self._clear_turn_state(sender_phone)
            return ERROR_GENERIC

    async def _handle_message_inner(
        self,
        sender_phone: str,
        text: str,
    ) -> str:
        """Inner body of handle_message — wrapped by the wall-clock guard."""
        self._track_cli(sender_phone, "user", text)
        self._llm_calls_this_turn[sender_phone] = 0
        self._turn_start_ts[sender_phone] = time.time()
        self._searched_this_turn[sender_phone] = set()
        self._search_attempts_this_turn[sender_phone] = 0

        # Build prompt from conversation history (cache for _llm_respond reuse)
        prompt = await self._build_prompt(sender_phone)
        # Mark the current message with a strong delimiter so the LLM focuses on it
        # (chat.db history can be noisy with old conversations). Brackets are
        # neutralized so the message can't forge [INSTRUCTION]-style markers.
        prompt += CURRENT_MESSAGE_DELIMITER.format(
            text=_escape_xml_delimiters(neutralize_brackets(text))
        )
        # Inject last-discussed topic so the LLM knows what "it" refers to.
        # Skip if the topic itself is stale (older than the gap threshold) —
        # the chat.db gap marker handles older history independently.
        if self._topic_is_fresh(sender_phone):
            topic = self._last_topic[sender_phone]
            safe_title = _escape_xml_delimiters(neutralize_brackets(topic["title"]))
            prompt += "\n" + LAST_DISCUSSED_TITLE.format(
                title=safe_title, tmdb_id=topic["tmdb_id"], media_type=topic["media_type"],
            )
        self._prompt_cache[sender_phone] = prompt
        self._prompt_cache_ctx_ts[sender_phone] = time.time()

        # Get LLM decision
        try:
            self._llm_calls_this_turn[sender_phone] += 1
            decision, meta = await self.llm.decide(prompt)
            log.info("LLM action=%s query=%s tmdb_id=%s", decision.action.value, decision.query or "-", decision.tmdb_id or "-")
        except LLMAuthError as e:
            log.error("LLM auth failed: %s", e)
            self._clear_turn_state(sender_phone)
            return ERROR_AUTH
        except Exception as e:
            log.error("LLM call failed: %s", e)
            self._clear_turn_state(sender_phone)
            return ERROR_GENERIC

        # Execute the action
        try:
            response = await self._execute(decision, sender_phone, text)
        finally:
            # Clear per-turn state after message is fully handled (or on error)
            self._clear_turn_state(sender_phone)
        self._track_cli(sender_phone, "assistant", response)

        return response

    async def _llm_respond(self, sender_phone: str, scenario: str = "empty_reply") -> tuple[str, bool]:
        """Build prompt with current context and let the LLM generate a response.

        Reuses the cached base prompt from handle_message when available,
        appending only the new context entries added by the handler.

        ``scenario`` selects the instruction from INSTRUCTION dict in prompts.py.
        """
        # Hard cap before another LLM call — guards against any bug that
        # would otherwise loop fallback → respond → request → fallback
        # against the live API. Real turns never approach this cap.
        calls = self._llm_calls_this_turn.get(sender_phone, 0)
        # Early-warning rail: real turns max out at ~4. If we hit 4+,
        # something is probably looping — log a warning so it's visible
        # in the daemon log before we actually trip the hard cap.
        if calls >= 4:
            log.warning(
                "_llm_respond: %d LLM calls already this turn for %s "
                "(scenario=%s) — approaching MAX_LLM_CALLS_PER_TURN=%d. "
                "Investigate if this happens often.",
                calls, mask_phone(sender_phone), scenario,
                self.MAX_LLM_CALLS_PER_TURN,
            )
        if calls >= self.MAX_LLM_CALLS_PER_TURN:
            log.error(
                "Aborting _llm_respond: hit MAX_LLM_CALLS_PER_TURN=%d for %s "
                "(scenario=%s). Likely a recursive bug — check action chain.",
                self.MAX_LLM_CALLS_PER_TURN, mask_phone(sender_phone), scenario,
            )
            return ERROR_GENERIC, False

        cached = self._prompt_cache.get(sender_phone)
        if cached is not None:
            # Append only context entries added since the cache was built
            cache_ts = self._prompt_cache_ctx_ts.get(sender_phone, 0.0)
            new_ctx = [
                (ts, text)
                for ts, text in self._context.get(sender_phone, [])
                if ts >= cache_ts
            ]
            extra = "\n".join(f"<{TAG_CONTEXT}>{_escape_xml_delimiters(text)}</{TAG_CONTEXT}>" for _ts, text in new_ctx)
            prompt = cached + "\n" + extra if extra else cached
        else:
            prompt = await self._build_prompt(sender_phone)
        instruction = INSTRUCTION.get(scenario)
        if instruction is None:
            log.error("Unknown scenario key %r in _llm_respond", scenario)
            return ERROR_GENERIC, False
        prompt += f"\n---\n[INSTRUCTION: {instruction}]"
        try:
            self._llm_calls_this_turn[sender_phone] = calls + 1
            decision, meta = await self.llm.decide(prompt, schema=RESPOND_SCHEMA)
            log.debug("LLM respond: action=%s message=%s", decision.action.value, (decision.message or "")[:100])
            multi = decision.multiple_results
            # forced_reply is terminal: it exists to break decision loops,
            # so ONLY a reply is accepted — honoring a request/search here
            # would re-enter the very loop it was invoked to end.
            if scenario == "forced_reply" and decision.action != Action.REPLY:
                log.error(
                    "forced_reply returned action=%s — giving up",
                    decision.action.value,
                )
                return ERROR_GENERIC, False
            # Allow request as a follow-up (user confirms a search result)
            if decision.action == Action.REQUEST and (decision.tmdb_id or decision.collection_id):
                return await handle_request(self, decision, sender_phone), multi
            # Allow the LLM to refine a search after seeing partial / empty
            # results. RESPOND_SCHEMA enumerates only reply/request but
            # tool_use enums aren't strictly enforced, and Haiku has been
            # seen to pick action=search when 0 results came back. Honor
            # it ONLY for queries not already searched this turn and while
            # the per-turn search budget lasts — an identical search returns
            # identical results, so the LLM makes the same decision again
            # and loops until the call cap aborts the turn (the 2026-07-05/06
            # "Hidden Figures"/"Green Book" incidents), and endless query
            # variants ("Green Book movie", "Green Book 2018") do the same.
            if decision.action == Action.SEARCH and (decision.query or decision.message):
                query = decision.query or decision.message
                searched = self._searched_this_turn.get(sender_phone, set())
                repeat = normalize_search_query(query, decision.media_type) in searched
                # Budget counts ATTEMPTS — failed searches don't enter the
                # repeat set, so counting completions alone would let a
                # persistently failing query loop to the LLM-call cap
                exhausted = (
                    self._search_attempts_this_turn.get(sender_phone, 0)
                    >= self.MAX_SEARCHES_PER_TURN
                )
                if repeat or exhausted:
                    log.warning(
                        "LLM picked action=search in respond (scenario=%s) but %r "
                        "%s — forcing reply from context",
                        scenario, query,
                        "was already searched this turn" if repeat
                        else f"exceeds MAX_SEARCHES_PER_TURN={self.MAX_SEARCHES_PER_TURN}",
                    )
                    note = (
                        CONTEXT_SEARCH_REPEAT.format(query=query)
                        if repeat else CONTEXT_SEARCH_BUDGET
                    )
                    self._add_context(sender_phone, note)
                    return await self._llm_respond(sender_phone, scenario="forced_reply")
                log.info(
                    "LLM picked action=search in respond (scenario=%s); "
                    "executing refined search",
                    scenario,
                )
                return await handle_search(self, decision, sender_phone), multi
            # Only accept reply — any other action means the LLM is confused
            if decision.action == Action.REPLY and decision.message and len(decision.message.strip()) >= 2:
                return decision.message, multi
            # Confused output (e.g. request without a tmdb_id): retry once
            # with a reply-only instruction instead of erroring at the user.
            log.warning(
                "LLM response returned action=%s message=%r instead of reply "
                "(scenario=%s)",
                decision.action.value, (decision.message or "")[:100], scenario,
            )
            if scenario == "forced_reply":
                log.error("forced_reply still did not produce a usable reply — giving up")
                return ERROR_GENERIC, False
            return await self._llm_respond(sender_phone, scenario="forced_reply")
        except LLMAuthError as e:
            log.error("LLM response auth failed: %s", e)
            return ERROR_AUTH, False
        except Exception as e:
            log.error("LLM response call failed: %s", e)
            return ERROR_GENERIC, False

    async def _build_prompt(self, sender_phone: str) -> str:
        """Build the full prompt from memory + chat.db messages + context buffer."""
        parts: list[str] = []

        # Time context
        tz = ZoneInfo(self.settings.timezone)
        now = datetime.datetime.now(tz)
        time_str = now.strftime("%A %B %-d, %Y %-I:%M %p %Z")
        parts.append(TIME_CONTEXT.format(time=time_str))

        # Per-user memory (markdown file) — run in thread to avoid blocking event loop
        memory_content = await asyncio.to_thread(self.memory.load, sender_phone)
        if memory_content:
            # Memory is LLM-compressed from user conversations — treat as
            # untrusted for control markers, same as user text
            safe_memory = _escape_xml_delimiters(neutralize_brackets(memory_content.strip()))
            parts.append(f"<{TAG_MEMORY}>\n{safe_memory}\n</{TAG_MEMORY}>")

        # Get messages: chat.db in daemon mode, _cli_history in CLI mode
        if self.monitor is not None:
            messages = await self.monitor.get_recent_messages(
                sender_phone,
                limit=self.settings.history_window,
            )
        else:
            messages = list(self._cli_history.get(sender_phone, []))

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
            timeline.append((ts, TAG_CONTEXT, text))
        timeline.sort(key=lambda x: x[0])

        msg_count = sum(1 for _, tag, _ in timeline if tag in ("user", "assistant"))
        ctx_count = sum(1 for _, tag, _ in timeline if tag == TAG_CONTEXT)
        log.debug(
            "Prompt for %s: %d messages, %d context entries, memory=%s",
            mask_phone(sender_phone), msg_count, ctx_count, bool(memory_content),
        )

        # Render with gap markers for 2+ hour gaps. User/assistant lines are
        # untrusted (chat.db text) — neutralize brackets so history can't
        # forge control markers. Context entries contain OUR markers and are
        # already bracket-safe internally (third-party fields neutralized at
        # ingestion in _base.py).
        prev_ts = 0.0
        for ts, tag, content in timeline:
            if prev_ts > 0 and ts - prev_ts >= gap_seconds:
                parts.append(CONVERSATION_GAP)
            if tag != TAG_CONTEXT:
                content = neutralize_brackets(content)
            safe = _escape_xml_delimiters(content)
            parts.append(f"<{tag}>{safe}</{tag}>")
            prev_ts = ts

        return "\n".join(parts)

    async def _enrich_results(
        self, results: list[SearchResult], *, enrich_downloads: bool = False
    ) -> None:
        """Fetch trailers, ratings, air dates, and optionally download progress for results."""
        from ..enrich import enrich_results
        await enrich_results(self.seerr, results, enrich_downloads=enrich_downloads)

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
        else:  # REPLY
            if decision.message and decision.message.strip():
                reply = decision.message.strip()
                probed = await self._probe_title_before_clarifying(
                    sender_phone, user_text, reply,
                )
                if probed is not None:
                    return probed
                return reply
            self._add_context(sender_phone, CONTEXT_EMPTY_REPLY)
            return (await self._llm_respond(sender_phone, scenario="empty_reply"))[0]

    # Messages that are pure conversation — never probe-search these.
    # Routing heuristic, not LLM-facing text.
    _CONVERSATIONAL_TOKENS = {
        "hi", "hello", "hey", "yo", "sup",
        "thanks", "thank you", "thx", "ty",
        "ok", "okay", "k", "kk", "cool", "nice", "great", "perfect", "awesome",
        "yes", "yeah", "yep", "yup", "no", "nope", "nah", "sure", "maybe",
        "good morning", "good night", "goodnight", "gm", "gn",
        "lol", "haha", "lmao", "wow",
        "never mind", "nevermind", "nvm", "stop", "cancel",
    }

    async def _probe_title_before_clarifying(
        self, sender_phone: str, user_text: str, reply: str
    ) -> str | None:
        """Probe-search a short message before sending a clarifying question.

        Short imperatives ("Analyze this", "Get out") are usually titles,
        but call 1 sometimes reads them as instructions and asks what the
        user means. When the reply is a question and the message is short
        and title-plausible, search the exact text first; only if nothing
        matches does the clarification go out (2026-07-07 "Analyze this"
        incident). Returns None to send the original reply unchanged.
        """
        if "?" not in reply:
            return None  # not a clarification — normal conversational reply
        text = user_text.strip()
        if not text or "?" in text:
            return None  # user asked a question; the reply answers it
        if not 1 <= len(text.split()) <= 6:
            return None
        if text.lower().rstrip(".!") in self._CONVERSATIONAL_TOKENS:
            return None
        self._search_attempts_this_turn[sender_phone] = (
            self._search_attempts_this_turn.get(sender_phone, 0) + 1
        )
        try:
            results = await self.seerr.search(text)
        except Exception as e:
            log.debug("Title probe search failed for %r: %s", text, e)
            return None
        if not results:
            log.info("Title probe for %r found nothing — sending clarification", text)
            return None
        log.info(
            "Reply-with-question for short message %r — title probe found %d "
            "result(s); presenting instead of clarifying", text, len(results),
        )
        self._searched_this_turn.setdefault(sender_phone, set()).add(
            normalize_search_query(text)
        )
        await self._enrich_results(results, enrich_downloads=True)
        self._add_context(sender_phone, format_search_results(results, query=text))
        # No set_topic / no posters here: the LLM may still judge the message
        # conversational and ignore the results — a wrong topic or stray
        # poster would outlive that judgment.
        return (await self._llm_respond(sender_phone, scenario="clarify_probe"))[0]

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
        scenario: str,
        skip_poster: bool = False,
    ) -> str:
        """Run LLM response in parallel with poster download, then send posters.

        Downloads raw posters in parallel with the LLM call. After the LLM
        responds, filters posters to only those the LLM actually mentioned
        (by title match), renumbered sequentially. This prevents sending
        posters for results the LLM chose to omit.
        """
        # Prune expired suppressions (conversation-gap TTL)
        gap_seconds = self.settings.conversation_gap_hours * 3600
        now = time.time()
        sent_map = {
            tid: ts
            for tid, ts in self._sent_posters.get(sender_phone, {}).items()
            if now - ts < gap_seconds
        }
        self._sent_posters[sender_phone] = sent_map
        all_shown = all(r.tmdb_id in sent_map for r in display_results) if display_results else True

        # Start raw poster downloads in parallel with LLM (no numbering yet)
        download_task = None
        if not skip_poster and not all_shown and self.posters and self.sender and display_results:
            download_task = asyncio.create_task(self.posters.download_all(display_results))

        response, multi = await self._llm_respond(sender_phone, scenario=scenario)

        if download_task:
            try:
                raw_posters = await download_task
            except Exception as e:
                log.error("Poster download failed: %s", e)
                raw_posters = []
            if raw_posters and self.sender:
                # Filter posters to only results the LLM actually mentioned
                raw_posters, sent_positions = self._match_posters_to_response(
                    response, display_results, raw_posters,
                )
                if len(raw_posters) > 1:
                    if multi:
                        # Numbered list — add number overlays (PIL work in a
                        # thread; it stalls the event loop for 100s of ms)
                        numbered = await asyncio.to_thread(
                            self.posters.number_posters, raw_posters
                        )
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
                # Mark only the posters actually sent — marking everything
                # in display_results suppresses posters the LLM omitted
                # this turn but might present next turn
                new_sent = self._sent_posters.setdefault(sender_phone, {})
                for pos in sent_positions:
                    if 1 <= pos <= len(display_results):
                        new_sent[display_results[pos - 1].tmdb_id] = time.time()

        return response

    @staticmethod
    def _match_posters_to_response(
        response: str,
        display_results: list[SearchResult],
        raw_posters: list[tuple[int, Path]],
    ) -> tuple[list[tuple[int, Path]], set[int]]:
        """Filter and renumber posters to only those the LLM mentioned.

        Matches display_results titles against the LLM response text, then
        returns (posters renumbered sequentially, original 1-indexed
        display_results positions that were kept). Falls back to the full
        list if no titles match (safety net).
        """
        response_lower = response.lower()
        poster_by_pos = {pos: path for pos, path in raw_posters}

        # Collect matches with their mention position in the response
        # so posters are ordered the way the LLM presented them.
        # Two passes: first find title+year matches (precise), then fill in
        # title-only matches for titles that had no title+year hit. This
        # prevents "Pressure (2026)" from matching all 5 results named "Pressure".
        title_year_matched: set[str] = set()
        matches: list[tuple[int, int, Path]] = []  # (mention_idx, orig_pos, path)
        # Pass 1: title+year (precise)
        for i, r in enumerate(display_results):
            pos = i + 1
            if pos not in poster_by_pos:
                continue
            if r.year:
                idx = response_lower.find(f"{r.title.lower()} ({r.year})")
                if idx >= 0:
                    title_year_matched.add(r.title.lower())
                    matches.append((idx, pos, poster_by_pos[pos]))
        # Pass 2: title-only fallback (only for titles not already matched by year)
        for i, r in enumerate(display_results):
            pos = i + 1
            if pos not in poster_by_pos:
                continue
            title_lower = r.title.lower()
            if title_lower in title_year_matched:
                continue  # already handled precisely by pass 1
            idx = response_lower.find(title_lower)
            if idx >= 0:
                matches.append((idx, pos, poster_by_pos[pos]))

        if not matches:
            # fallback: can't determine, send all
            return raw_posters, {pos for pos, _ in raw_posters}
        # Sort by mention position so poster order matches LLM's text
        matches.sort(key=lambda x: x[0])
        renumbered = [(i + 1, path) for i, (_, _, path) in enumerate(matches)]
        return renumbered, {pos for _, pos, _ in matches}

    async def _store_request_context(
        self, sender_phone: str, title: str, decision: LLMDecision
    ) -> None:
        """Store context about the requested/discussed title for future narrowing."""
        ctx = LAST_DISCUSSED_TITLE.format(
            title=neutralize_brackets(title),
            tmdb_id=decision.tmdb_id, media_type=decision.media_type,
        )
        self._add_context(sender_phone, ctx)
        self.set_topic(sender_phone, title, decision.tmdb_id, decision.media_type)

    def set_topic(
        self, sender_phone: str, title: str, tmdb_id: int, media_type: str,
    ) -> None:
        """Record the most recently discussed title for pronoun resolution.

        Stamped with the current time so freshness can be evaluated later
        (e.g. a digest-suggested title remains valid for a few hours even
        though older chat.db history has gaps before it).
        """
        self._last_topic[sender_phone] = {
            # Titles come from Seerr/TMDB — neutralize so a title can't
            # forge bracket markers when injected into the prompt
            "title": neutralize_brackets(title),
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "set_ts": time.time(),
        }

    def _topic_is_fresh(self, sender_phone: str) -> bool:
        """Return True if the stored topic is recent enough to inject."""
        topic = self._last_topic.get(sender_phone)
        if not topic:
            return False
        gap_seconds = self.settings.conversation_gap_hours * 3600
        return (time.time() - topic.get("set_ts", 0)) < gap_seconds
