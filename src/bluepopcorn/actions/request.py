from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..prompts import (
    CONTEXT_COLLECTION_ALREADY,
    CONTEXT_COLLECTION_EMPTY,
    CONTEXT_COLLECTION_FAILED,
    CONTEXT_COLLECTION_FOOTER,
    CONTEXT_COLLECTION_HEADER,
    CONTEXT_COLLECTION_NONE,
    CONTEXT_COLLECTION_REQUESTED,
    CONTEXT_DEDUP,
    CONTEXT_SEASON_INVALID,
    ERROR_GENERIC,
)
from ..seerr import SeerrClient, seerr_title
from ..types import LLMDecision, MediaStatus, STATUS_LABELS
from ._base import format_search_results

log = logging.getLogger(__name__)

# Max movies to request from a single collection
MAX_COLLECTION_SIZE = 20

# Search/recommend/recent result lines: "N. Title (year) [Movie] tmdb:1234 - ..."
# `[^\n\[\]]*?` keeps the match anchored to the same result line.
_RESULT_MEDIA_TYPE_RE = re.compile(r"\[(Movie|TV)\][^\n\[\]]*?tmdb:(\d+)")
# Last-discussed/topic injection: "[Last discussed title: ... tmdb:1234 movie]"
_TOPIC_MEDIA_TYPE_RE = re.compile(r"tmdb:(\d+) (movie|tv)\b")


def _tmdb_id_backed_by_context(executor: ActionExecutor, sender_phone: str, tmdb_id: int) -> bool:
    """Return True if `tmdb:<id>` appears in anything the LLM has seen.

    Search / recommend / recent results render ids as ``tmdb:<id>`` via
    ``format_result_line`` in ``_base.py``, so a substring check is
    enough to distinguish an id the LLM read from one it invented.
    Checks both the cached call-1 prompt and the in-flight context
    buffer — the latter matters when handle_request is reached
    recursively via ``_llm_respond`` after a fallback search has added
    fresh results to context.
    """
    needle = f"tmdb:{tmdb_id}"
    cached = executor._prompt_cache.get(sender_phone) or ""
    if needle in cached:
        return True
    for _ts, text in executor._context.get(sender_phone, []):
        if needle in text:
            return True
    return False


def _known_media_type_for_tmdb(
    executor: ActionExecutor, sender_phone: str, tmdb_id: int,
) -> str | None:
    """Look up the media_type already paired with ``tmdb_id`` in seen context.

    Sources, in order of authority:
      1. The fresh ``_last_topic`` (set from Seerr search/trending data).
      2. The cached call-1 prompt (includes the topic injection and
         any rendered result lines from prior turns within history).
      3. The in-flight context buffer (this turn's search results).

    Returns ``"movie"`` / ``"tv"`` or None if no pairing is found. Used to
    override an LLM-hallucinated media_type when authoritative data is
    on hand — Haiku sometimes returns ``"tv"`` for a movie tmdb (and
    vice-versa) even after correctly identifying the tmdb_id from a
    digest suggestion, which then fails the Seerr request with a 500.
    """
    topic = executor._last_topic.get(sender_phone)
    if (
        topic
        and topic.get("tmdb_id") == tmdb_id
        and topic.get("media_type") in ("movie", "tv")
    ):
        return topic["media_type"]

    sources: list[str] = []
    cached = executor._prompt_cache.get(sender_phone)
    if cached:
        sources.append(cached)
    for _ts, text in executor._context.get(sender_phone, []):
        sources.append(text)

    for source in sources:
        for m in _RESULT_MEDIA_TYPE_RE.finditer(source):
            if int(m.group(2)) == tmdb_id:
                return "tv" if m.group(1) == "TV" else "movie"
        for m in _TOPIC_MEDIA_TYPE_RE.finditer(source):
            if int(m.group(1)) == tmdb_id:
                return m.group(2)
    return None


async def handle_request(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Execute a request action: add media to Seerr with dedup check."""
    # Collection request — batch-add all movies in a collection
    if decision.collection_id:
        return await _handle_collection_request(executor, decision, sender_phone)

    # Reject tmdb_ids the LLM invented (not found in any <context> block it
    # was shown). Haiku sometimes hallucinates ids when confirming titles
    # that only appeared in a digest or memory; trusting those leads to
    # requesting the wrong Seerr record and lying about it to the user.
    # When a handler in this turn just set _last_topic from a real Seerr
    # result, prefer that tmdb over re-searching — the topic is
    # authoritative and re-searching with the paren'd title often
    # returns 0 and loops. Otherwise null out and fall through to the
    # missing-id search fallback.
    if decision.tmdb_id and not _tmdb_id_backed_by_context(
        executor, sender_phone, decision.tmdb_id,
    ):
        log.info(
            "Rejecting unbacked tmdb_id=%d (not in prompt <context>)",
            decision.tmdb_id,
        )
        turn_start = executor._turn_start_ts.get(sender_phone, float("inf"))
        topic = executor._last_topic.get(sender_phone)
        if (
            topic
            and topic.get("set_ts", 0) >= turn_start
            and topic.get("tmdb_id")
            and topic.get("media_type") in ("movie", "tv")
        ):
            log.info(
                "Substituting this-turn topic tmdb_id=%d media_type=%s",
                topic["tmdb_id"], topic["media_type"],
            )
            decision.tmdb_id = topic["tmdb_id"]
            decision.media_type = topic["media_type"]
        else:
            log.info("No this-turn topic; forcing search fallback")
            decision.tmdb_id = None
            decision.media_type = None

    # Override an LLM-hallucinated media_type when seen context disagrees.
    # Haiku can return the right tmdb_id with the wrong type (movie vs tv),
    # which then fails Seerr with a 500 from the wrong endpoint. See the
    # 2026-05-18 Punisher: One Last Kill (tmdb:1439930) incident.
    if decision.tmdb_id and decision.media_type:
        known = _known_media_type_for_tmdb(
            executor, sender_phone, decision.tmdb_id,
        )
        if known and known != decision.media_type:
            log.info(
                "Correcting media_type for tmdb:%d: %s -> %s (from seen context)",
                decision.tmdb_id, decision.media_type, known,
            )
            decision.media_type = known

    if not decision.tmdb_id or not decision.media_type:
        # LLM chose request but didn't provide the ID — search for what the
        # user likely means and hand results back to the LLM to decide.
        # Only use the stored topic if it's still fresh; an older topic from
        # before a conversation gap shouldn't override what the user is
        # actually replying to right now.
        topic = executor._last_topic.get(sender_phone) if executor._topic_is_fresh(sender_phone) else None
        search_term = (topic["title"] if topic else None) or decision.query or decision.message or ""
        # Strip trailing "(YYYY)" — Seerr's year-stripping regex requires
        # whitespace before the year and won't parse parens, so "Title (2013)"
        # returns 0 results even when "Title" works.
        search_term = re.sub(r"\s*\(\d{4}\)\s*$", "", search_term).strip() or search_term
        if search_term:
            try:
                results = await executor.seerr.search(search_term)
                if results:
                    await executor._enrich_results(results, enrich_downloads=True)
                    context = format_search_results(results, query=search_term)
                    executor._add_context(sender_phone, context)
                    top = results[0]
                    year_str = f" ({top.year})" if top.year else ""
                    executor.set_topic(
                        sender_phone, f"{top.title}{year_str}", top.tmdb_id, top.media_type,
                    )
            except Exception as e:
                log.debug("Fallback search for request failed: %s", e)
        return (await executor._llm_respond(sender_phone, scenario="search_results"))[0]

    # Check if already requested/available before making a duplicate request
    title = "this"
    seasons: list[int] | None = None
    try:
        detail = await executor.seerr.get_media_status(decision.media_type, decision.tmdb_id)
        if detail:
            raw_title = seerr_title(detail, default="this")
            year_raw = detail.get("releaseDate") or detail.get("firstAirDate") or ""
            year = year_raw[:4] if len(year_raw) >= 4 else ""
            title = f"{raw_title} ({year})" if year else raw_title
            # Pre-extract season numbers for TV to avoid a redundant detail call
            if decision.media_type == "tv":
                all_seasons = SeerrClient.extract_season_numbers(detail)
                # Apply LLM-specified season selection
                if decision.seasons:
                    valid = [s for s in decision.seasons if s in all_seasons]
                    if valid:
                        seasons = valid
                    else:
                        # LLM specified seasons that don't exist — inform, don't silently request all
                        executor._add_context(
                            sender_phone,
                            CONTEXT_SEASON_INVALID.format(
                                requested=decision.seasons, available=all_seasons,
                            ),
                        )
                        return (await executor._llm_respond(sender_phone, scenario="search_results"))[0]
                else:
                    seasons = all_seasons
            media_info = detail.get("mediaInfo")
            if media_info:
                raw_status = media_info.get("status", 0)
                try:
                    status = MediaStatus(raw_status)
                except ValueError:
                    status = MediaStatus.UNKNOWN

                if status in (MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE, MediaStatus.PROCESSING, MediaStatus.PENDING):
                    dedup_context = CONTEXT_DEDUP.format(
                        title=title, tmdb_id=decision.tmdb_id, status=STATUS_LABELS[status],
                    )
                    await executor._store_request_context(sender_phone, title, decision)
                    executor._add_context(sender_phone, dedup_context)
                    return (await executor._llm_respond(sender_phone, scenario="dedup"))[0]
    except Exception as e:
        log.debug("Pre-request status check failed (proceeding anyway): %s", e)

    try:
        await executor.seerr.request_media(
            decision.media_type, decision.tmdb_id, seasons=seasons
        )
        await executor._store_request_context(sender_phone, title, decision)
        # Track request for targeted notifications
        if executor.request_tracker:
            await executor.request_tracker.record(decision.media_type, decision.tmdb_id, sender_phone)
        return decision.message
    except Exception as e:
        log.error("Request failed (type=%s tmdb=%s): %s", decision.media_type, decision.tmdb_id, e)
        return ERROR_GENERIC


async def _handle_collection_request(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Request all movies in a TMDB collection."""
    collection = await executor.seerr.get_collection(decision.collection_id)
    if not collection:
        log.warning("Collection %d not found", decision.collection_id)
        return ERROR_GENERIC

    parts = collection.get("parts", [])
    if not parts:
        executor._add_context(sender_phone, CONTEXT_COLLECTION_EMPTY)
        return (await executor._llm_respond(sender_phone, scenario="collection_results"))[0]

    # Cap to prevent runaway requests
    parts = parts[:MAX_COLLECTION_SIZE]
    collection_name = collection.get("name", "collection")

    # Check status of each movie in parallel
    async def check_status(movie: dict) -> dict:
        tmdb_id = movie.get("id")
        title = movie.get("title", "Unknown")
        release = movie.get("releaseDate") or ""
        year = release[:4] if len(release) >= 4 else ""
        display = f"{title} ({year})" if year else title
        if not tmdb_id:
            return {"title": display, "tmdb_id": None, "status": "skip"}
        try:
            detail = await executor.seerr.get_media_status("movie", tmdb_id)
            if detail:
                media_info = detail.get("mediaInfo")
                if media_info:
                    raw = media_info.get("status", 0)
                    try:
                        s = MediaStatus(raw)
                    except ValueError:
                        s = MediaStatus.UNKNOWN
                    if s in (MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE,
                             MediaStatus.PROCESSING, MediaStatus.PENDING):
                        return {"title": display, "tmdb_id": tmdb_id, "status": STATUS_LABELS[s]}
        except Exception as e:
            log.debug("Status check failed for %s (proceeding to request): %s", display, e)
        return {"title": display, "tmdb_id": tmdb_id, "status": "requestable"}

    statuses = await asyncio.gather(*[check_status(m) for m in parts])

    # Separate requestable items from already-tracked
    requestable = []
    already = []
    for info in statuses:
        if info["status"] == "skip":
            continue
        if info["status"] == "requestable":
            requestable.append(info)
        else:
            already.append(f"{info['title']} ({info['status']})")

    # Request all requestable movies in parallel
    requested = []
    failed = []

    async def do_request(info: dict) -> None:
        try:
            await executor.seerr.request_media("movie", info["tmdb_id"])
            if executor.request_tracker:
                await executor.request_tracker.record("movie", info["tmdb_id"], sender_phone)
            requested.append(info["title"])
        except Exception as e:
            log.warning("Collection request failed for %s: %s", info["title"], e)
            failed.append(info["title"])

    if requestable:
        await asyncio.gather(*[do_request(info) for info in requestable])

    # Build context for LLM response
    ctx_parts = [CONTEXT_COLLECTION_HEADER.format(name=collection_name)]
    if requested:
        ctx_parts.append(CONTEXT_COLLECTION_REQUESTED.format(titles=", ".join(requested)))
    if already:
        ctx_parts.append(CONTEXT_COLLECTION_ALREADY.format(titles=", ".join(already)))
    if failed:
        ctx_parts.append(CONTEXT_COLLECTION_FAILED.format(titles=", ".join(failed)))
    if not requested and not already and not failed:
        ctx_parts.append(CONTEXT_COLLECTION_NONE)
    ctx_parts.append(CONTEXT_COLLECTION_FOOTER)
    executor._add_context(sender_phone, "\n".join(ctx_parts))

    return (await executor._llm_respond(sender_phone, scenario="collection_results"))[0]
