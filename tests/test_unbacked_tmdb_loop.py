"""End-to-end test for the 2026-06-21 16:59 recursive loop fix.

Reproduces the exact failure: the LLM picks ``action=search`` and the
search returns 1 result, then on the next LLM call the LLM hallucinates
an ``action=request`` with a wrong ``tmdb_id``. Before the fix this
looped 6 times and emitted ``ERROR_GENERIC``. After the fix it should
substitute the in-turn topic's tmdb and request the correct title once.

Uses the real ``ActionExecutor`` against real Seerr search (read-only)
but stubs ``LLMClient.decide`` and ``seerr.request_media`` — per the
project rule that tests must never trigger live Seerr requests.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bluepopcorn.actions import ActionExecutor
from bluepopcorn.config import load_settings
from bluepopcorn.memory import UserMemory
from bluepopcorn.seerr import SeerrClient
from bluepopcorn.types import Action, LLMDecision

TEST_SENDER = "+10000000000"
HALLUCINATED_TMDB = 999999


async def main() -> int:
    settings = load_settings()
    seerr = SeerrClient(settings=settings)
    memory = UserMemory(settings)

    captured_requests: list[tuple[str, int]] = []

    async def fake_request_media(media_type, tmdb_id, *, seasons=None, **kwargs):
        captured_requests.append((media_type, tmdb_id))
        return {"id": 100, "media": {"id": 100}}

    seerr.request_media = fake_request_media  # type: ignore[assignment]

    # Pretend the title hasn't been requested before so the dedup branch
    # doesn't fire (which would legitimately trigger a 3rd LLM call).
    async def fake_get_media_status(media_type, tmdb_id):
        return {
            "id": tmdb_id,
            "title": "The Best Offer",
            "releaseDate": "2013-01-01",
            "mediaInfo": None,
        }

    seerr.get_media_status = fake_get_media_status  # type: ignore[assignment]

    llm = MagicMock()
    llm.fallback_model = "haiku"
    llm.model = "haiku"

    call_count = {"n": 0}

    async def fake_decide(prompt, model=None, schema=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMDecision(
                action=Action.SEARCH,
                message="Searching for The Best Offer.",
                query="The Best Offer 2013",
                media_type="movie",
            ), {"model": "haiku", "duration_s": 0.1}
        if call_count["n"] == 2:
            return LLMDecision(
                action=Action.REQUEST,
                message="The Best Offer (2013) added to your queue.",
                tmdb_id=HALLUCINATED_TMDB,
                media_type="movie",
            ), {"model": "haiku", "duration_s": 0.1}
        raise RuntimeError(
            f"LLM was called {call_count['n']} times — the loop guard "
            "should have stopped re-entries after the substitution."
        )

    llm.decide = AsyncMock(side_effect=fake_decide)

    executor = ActionExecutor(
        seerr=seerr,
        llm=llm,
        sender=None,
        posters=None,
        memory=memory,
        monitor=None,
        settings=settings,
        request_tracker=None,
    )

    response = await executor.handle_message(TEST_SENDER, "the best offer, 2013 movie")

    failures: list[str] = []

    if "something went wrong" in response.lower():
        failures.append(f"Got ERROR_GENERIC response: {response!r}")

    if not captured_requests:
        failures.append("seerr.request_media was never called")
    elif len(captured_requests) != 1:
        failures.append(f"expected 1 request, got {len(captured_requests)}: {captured_requests}")
    else:
        media_type, tmdb_id = captured_requests[0]
        if tmdb_id == HALLUCINATED_TMDB:
            failures.append(
                f"Requested the hallucinated tmdb {HALLUCINATED_TMDB} — substitution didn't fire"
            )
        if tmdb_id != 152742:
            failures.append(
                f"Expected tmdb_id=152742 (The Best Offer 2013); got {tmdb_id}"
            )
        if media_type != "movie":
            failures.append(f"Expected media_type=movie, got {media_type!r}")

    if call_count["n"] > 2:
        failures.append(
            f"LLM was called {call_count['n']} times — substitution should "
            "have skipped the fallback-search re-entry"
        )

    print(f"LLM calls: {call_count['n']}")
    print(f"Captured requests: {captured_requests}")
    print(f"Response: {response[:120]!r}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS — loop fixed end-to-end")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
