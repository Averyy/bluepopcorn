from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from . import ActionExecutor

from ..types import LLMDecision
from ..weather import get_weather, get_pollen

log = logging.getLogger(__name__)


async def handle_weather(
    executor: ActionExecutor, decision: LLMDecision, sender_phone: str
) -> str:
    """Fetch weather/pollen data and format directly."""
    # Check if user specifically asked about pollen/allergies
    history = await executor.db.get_history(sender_phone)
    last_user_msg = ""
    for entry in reversed(history):
        if entry.role == "user":
            last_user_msg = entry.content.lower()
            break
    pollen_specific = any(kw in last_user_msg for kw in ("pollen", "allerg"))
    try:
        async with httpx.AsyncClient(timeout=executor.settings.http_timeout) as client:
            weather, pollen = await asyncio.gather(
                get_weather(executor.settings, client),
                get_pollen(executor.settings, client, pollen_specific=pollen_specific),
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
    await executor.db.add_history(sender_phone, "context", f"[Weather data: {data}]")
    return await executor._llm_respond(sender_phone, fallback=data)
