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
        executor._add_context(sender_phone, "[Remember action: no fact was provided]")
        return (await executor._llm_respond(sender_phone, intent=None))[0]
    async with executor.memory.get_lock(sender_phone):
        added = executor.memory.add_preference(sender_phone, fact)
    if not added:
        return decision.message
    return decision.message


async def handle_forget(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Remove a stored user fact/preference."""
    keyword = decision.fact or decision.message
    if not keyword:
        executor._add_context(sender_phone, "[Forget action: no keyword was provided]")
        return (await executor._llm_respond(sender_phone, intent=None))[0]
    async with executor.memory.get_lock(sender_phone):
        removed = executor.memory.remove_preference(sender_phone, keyword)
    return decision.message
