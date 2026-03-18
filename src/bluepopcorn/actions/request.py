from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..seerr import SeerrClient, seerr_title
from ..types import LLMDecision, MediaStatus
from ._base import ERROR_GENERIC

log = logging.getLogger(__name__)


async def handle_request(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Execute a request action: add media to Seerr with dedup check."""
    if not decision.tmdb_id or not decision.media_type:
        return "I need to know which title to request. Can you search first?"

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

                dedup_msg = {
                    MediaStatus.AVAILABLE: f"{title} is already in your library.",
                    MediaStatus.PROCESSING: f"{title} is already downloading.",
                    MediaStatus.PENDING: f"{title} is already requested, waiting on approval.",
                }.get(status)
                if dedup_msg:
                    await executor._store_request_context(sender_phone, title, decision)
                    return dedup_msg
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
