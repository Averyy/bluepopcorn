from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..seerr import SeerrClient, seerr_title
from ..types import LLMDecision, MediaStatus
from ._base import ERROR_GENERIC, format_search_results

log = logging.getLogger(__name__)


async def handle_request(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Execute a request action: add media to Seerr with dedup check."""
    if not decision.tmdb_id or not decision.media_type:
        # LLM chose request but didn't provide the ID — search for what the
        # user likely means and hand results back to the LLM to decide.
        topic = executor._last_topic.get(sender_phone)
        search_term = (topic["title"] if topic else None) or decision.query or decision.message or ""
        if search_term:
            try:
                results = await executor.seerr.search(search_term)
                if results:
                    await executor._enrich_results(results, enrich_downloads=True)
                    context = format_search_results(results, query=search_term)
                    executor._add_context(sender_phone, context)
                    top = results[0]
                    year_str = f" ({top.year})" if top.year else ""
                    executor._last_topic[sender_phone] = {
                        "title": f"{top.title}{year_str}",
                        "tmdb_id": top.tmdb_id,
                        "media_type": top.media_type,
                    }
            except Exception as e:
                log.debug("Fallback search for request failed: %s", e)
        return (await executor._llm_respond(sender_phone, intent="search"))[0]

    # Check if already requested/available before making a duplicate request
    title = "this"
    seasons: list[int] | None = None
    try:
        detail = await executor.seerr.get_media_status(decision.media_type, decision.tmdb_id)
        if detail:
            title = seerr_title(detail, default="this")
            # Pre-extract season numbers for TV to avoid a redundant detail call
            if decision.media_type == "tv":
                seasons = SeerrClient.extract_season_numbers(detail)
            media_info = detail.get("mediaInfo")
            if media_info:
                raw_status = media_info.get("status", 0)
                try:
                    status = MediaStatus(raw_status)
                except ValueError:
                    status = MediaStatus.UNKNOWN

                dedup_context = {
                    MediaStatus.AVAILABLE: f'[Request check: "{title}" is already available in library]',
                    MediaStatus.PROCESSING: f'[Request check: "{title}" is already downloading]',
                    MediaStatus.PENDING: f'[Request check: "{title}" is already requested, waiting on approval]',
                }.get(status)
                if dedup_context:
                    await executor._store_request_context(sender_phone, title, decision)
                    executor._add_context(sender_phone, dedup_context)
                    return (await executor._llm_respond(sender_phone, fallback=f"{title} is already on the server.", intent="dedup"))[0]
    except Exception as e:
        log.debug("Pre-request status check failed (proceeding anyway): %s", e)

    try:
        await executor.seerr.request_media(
            decision.media_type, decision.tmdb_id, seasons=seasons
        )
        await executor._store_request_context(sender_phone, title, decision)
        return decision.message
    except Exception as e:
        log.error("Request failed (type=%s tmdb=%s): %s", decision.media_type, decision.tmdb_id, e)
        return ERROR_GENERIC
