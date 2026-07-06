"""Regression tests for the LLM decision-loop guards (fully mocked, no live APIs).

Covers the incidents that ended in "Something went wrong, try again in a sec":

- 2026-06-21 16:59: request with a hallucinated tmdb_id → substitution of
  the this-turn topic instead of a fallback-search loop.
- 2026-07-05/06: respond-phase re-search of an identical query looping
  until MAX_LLM_CALLS_PER_TURN — now refused after one search, with a
  forced_reply retry.
- forced_reply is terminal: only action=reply is honored inside it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bluepopcorn.actions import ActionExecutor
from bluepopcorn.prompts import ERROR_GENERIC
from bluepopcorn.types import Action, LLMDecision, MediaStatus, SearchResult

SENDER = "+10000000000"
HALLUCINATED_TMDB = 999999
REAL_TMDB = 152742


def _result(tmdb_id: int = REAL_TMDB, title: str = "The Best Offer") -> SearchResult:
    return SearchResult(
        tmdb_id=tmdb_id, title=title, year=2013, media_type="movie",
        overview="An art auctioneer", status=MediaStatus.NOT_TRACKED,
    )


def _executor(llm: MagicMock, seerr: MagicMock) -> ActionExecutor:
    settings = MagicMock()
    settings.timezone = "America/Toronto"
    settings.conversation_gap_hours = 2
    settings.history_window = 20
    memory = MagicMock()
    memory.load = MagicMock(return_value="")
    ex = ActionExecutor(
        seerr=seerr, llm=llm, sender=None, posters=None,
        memory=memory, monitor=None, settings=settings, request_tracker=None,
    )
    ex._enrich_results = AsyncMock()
    return ex


def _seerr(results: list[SearchResult] | None = None) -> MagicMock:
    seerr = MagicMock()
    seerr.search = AsyncMock(return_value=results if results is not None else [_result()])
    seerr.get_media_status = AsyncMock(return_value={
        "id": REAL_TMDB,
        "title": "The Best Offer",
        "releaseDate": "2013-01-01",
        "mediaInfo": None,
    })
    seerr.request_media = AsyncMock(return_value={"id": 100})
    return seerr


@pytest.mark.asyncio
async def test_unbacked_tmdb_substitutes_this_turn_topic():
    """Search sets the topic; a hallucinated request tmdb is replaced by it."""
    llm = MagicMock()
    decisions = [
        LLMDecision(action=Action.SEARCH, message="searching",
                    query="The Best Offer 2013", media_type="movie"),
        LLMDecision(action=Action.REQUEST, message="The Best Offer (2013) added to your queue.",
                    tmdb_id=HALLUCINATED_TMDB, media_type="movie"),
    ]
    llm.decide = AsyncMock(side_effect=[(d, {}) for d in decisions])
    seerr = _seerr()
    ex = _executor(llm, seerr)

    response = await ex.handle_message(SENDER, "the best offer, 2013 movie")

    assert "something went wrong" not in response.lower()
    seerr.request_media.assert_awaited_once()
    args = seerr.request_media.await_args
    assert args.args[1] == REAL_TMDB, "substitution should replace the hallucinated tmdb"
    assert args.args[0] == "movie"
    assert llm.decide.await_count == 2


@pytest.mark.asyncio
async def test_repeat_search_refused_after_one_execution():
    """An LLM that always re-searches the same query gets exactly 1 Seerr search."""
    llm = MagicMock()
    loop_decision = LLMDecision(
        action=Action.SEARCH, query="Hidden Figures", message="searching",
    )
    llm.decide = AsyncMock(return_value=(loop_decision, {}))
    seerr = _seerr([_result(381284, "Hidden Figures")])
    ex = _executor(llm, seerr)

    response = await ex.handle_message(SENDER, "Hidden figures")

    assert seerr.search.await_count == 1, "identical re-search must be refused"
    # decide → respond(search_results) → respond(forced_reply) = 3, far
    # below MAX_LLM_CALLS_PER_TURN=6
    assert llm.decide.await_count <= 3
    # An LLM that NEVER replies leaves only the terminal error
    assert response == ERROR_GENERIC


@pytest.mark.asyncio
async def test_forced_reply_recovers_with_real_answer():
    """One loop iteration, then the model obeys forced_reply → user gets a reply."""
    llm = MagicMock()
    loop_decision = LLMDecision(action=Action.SEARCH, query="Hidden Figures", message="searching")
    good = LLMDecision(action=Action.REPLY, message="Hidden Figures (2016) is already in your library.")
    llm.decide = AsyncMock(side_effect=[(loop_decision, {}), (loop_decision, {}), (good, {})])
    seerr = _seerr([_result(381284, "Hidden Figures")])
    ex = _executor(llm, seerr)

    response = await ex.handle_message(SENDER, "Hidden figures")

    assert seerr.search.await_count == 1
    assert "library" in response


@pytest.mark.asyncio
async def test_forced_reply_rejects_request_action():
    """forced_reply is terminal — a request inside it must not reach Seerr."""
    llm = MagicMock()
    loop_decision = LLMDecision(action=Action.SEARCH, query="Green Book", message="searching")
    bad_request = LLMDecision(action=Action.REQUEST, message="adding",
                              tmdb_id=REAL_TMDB, media_type="movie")
    llm.decide = AsyncMock(side_effect=[
        (loop_decision, {}),   # call 1: decide → search
        (loop_decision, {}),   # call 2: respond repeats the search → forced_reply
        (bad_request, {}),     # call 3: forced_reply disobeys with a request
    ])
    seerr = _seerr([_result(490132, "Green Book")])
    ex = _executor(llm, seerr)

    response = await ex.handle_message(SENDER, "Green book")

    seerr.request_media.assert_not_awaited()
    assert response == ERROR_GENERIC


@pytest.mark.asyncio
async def test_search_budget_caps_distinct_query_variants():
    """Endless query variants stop at MAX_SEARCHES_PER_TURN executions."""
    llm = MagicMock()
    variants = ["Green Book", "Green Book movie", "Green Book 2018 film", "Green Book drama"]
    responses = [
        (LLMDecision(action=Action.SEARCH, query=q, message="searching"), {})
        for q in variants
    ] * 3  # more than the guard will ever consume
    llm.decide = AsyncMock(side_effect=responses)
    seerr = _seerr([_result(490132, "Green Book")])
    ex = _executor(llm, seerr)

    await ex.handle_message(SENDER, "Green book")

    assert seerr.search.await_count <= ActionExecutor.MAX_SEARCHES_PER_TURN
