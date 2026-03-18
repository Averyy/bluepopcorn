from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import ActionExecutor

from ..types import LLMDecision


async def handle_remember(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Store a user fact/preference."""
    fact = decision.fact or decision.message
    if not fact:
        return "What should I remember?"
    await executor.db.add_fact(sender_phone, fact)
    return decision.message or "Got it, I'll remember that."


async def handle_forget(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Remove a stored user fact/preference."""
    keyword = decision.fact or decision.message
    if not keyword:
        return "What should I forget?"
    removed = await executor.db.remove_fact(sender_phone, keyword)
    if removed:
        return decision.message or "Done, forgot it."
    return decision.message or "I don't have anything like that saved."
