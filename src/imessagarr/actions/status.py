from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..types import LLMDecision
from ._base import ERROR_GENERIC

log = logging.getLogger(__name__)


async def handle_check_status(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Check pending/processing requests, store as context, let LLM respond."""
    try:
        status = await executor._fetch_status_data()
        if not status.has_activity:
            return "No pending or in-progress requests."

        lines: list[str] = []
        if status.processing_titles:
            lines.append("Downloading: " + ", ".join(status.processing_titles))
        if status.pending_titles:
            lines.append("Waiting for approval: " + ", ".join(status.pending_titles))
        if status.recently_added:
            lines.append("Recently added: " + ", ".join(status.recently_added))

        if not lines:
            return "No pending or in-progress requests."

        data = "\n".join(lines)
        await executor.db.add_history(sender_phone, "context", f"[Request status: {data}]")
        return await executor._llm_respond(sender_phone, fallback=data)
    except Exception as e:
        log.error("Status check failed: %s", e)
        return ERROR_GENERIC
