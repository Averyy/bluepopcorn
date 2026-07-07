"""Tests for the title-probe-before-clarifying backstop (fully mocked).

2026-07-07 incident: "Analyze this" → call 1 read it as an instruction and
asked "Are you asking me to analyze a movie or show title?" instead of
searching. The probe searches the exact text before any clarifying
question goes out; the question is only sent when the search finds nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bluepopcorn.actions import ActionExecutor
from bluepopcorn.types import Action, LLMDecision, MediaStatus, SearchResult

SENDER = "+10000000000"
CLARIFY = "That's not enough info. Are you asking me to analyze a movie or something else?"


def _result() -> SearchResult:
    return SearchResult(
        tmdb_id=10396, title="Analyze This", year=1999, media_type="movie",
        overview="A mafia boss sees a psychiatrist", status=MediaStatus.NOT_TRACKED,
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


def _llm(*decisions: LLMDecision) -> MagicMock:
    llm = MagicMock()
    llm.decide = AsyncMock(side_effect=[(d, {}) for d in decisions])
    return llm


@pytest.mark.asyncio
async def test_probe_presents_title_instead_of_clarifying():
    """'Analyze this' → probe search hits → present the title, not the question."""
    llm = _llm(
        LLMDecision(action=Action.REPLY, message=CLARIFY),
        LLMDecision(action=Action.REPLY, message="Analyze This (1999) is a crime comedy. Want me to add it?"),
    )
    seerr = MagicMock()
    seerr.search = AsyncMock(return_value=[_result()])
    ex = _executor(llm, seerr)

    response = await ex.handle_message(SENDER, "Analyze this")

    seerr.search.assert_awaited_once_with("Analyze this")
    assert "Analyze This (1999)" in response
    assert response != CLARIFY


@pytest.mark.asyncio
async def test_probe_empty_sends_original_clarification():
    """No search hits → the clarifying question goes out unchanged."""
    llm = _llm(LLMDecision(action=Action.REPLY, message=CLARIFY))
    seerr = MagicMock()
    seerr.search = AsyncMock(return_value=[])
    ex = _executor(llm, seerr)

    response = await ex.handle_message(SENDER, "Analyze this")

    seerr.search.assert_awaited_once()
    assert response == CLARIFY
    assert llm.decide.await_count == 1


@pytest.mark.asyncio
async def test_probe_skipped_for_conversational_tokens():
    """'Thanks' never triggers a probe even if the reply asks a question."""
    llm = _llm(LLMDecision(action=Action.REPLY, message="You're welcome! Anything else?"))
    seerr = MagicMock()
    seerr.search = AsyncMock(return_value=[_result()])
    ex = _executor(llm, seerr)

    response = await ex.handle_message(SENDER, "Thanks!")

    seerr.search.assert_not_awaited()
    assert response == "You're welcome! Anything else?"


@pytest.mark.asyncio
async def test_probe_skipped_when_reply_is_not_a_question():
    """A plain conversational reply passes through without a probe."""
    llm = _llm(LLMDecision(action=Action.REPLY, message="Glad you liked it."))
    seerr = MagicMock()
    seerr.search = AsyncMock(return_value=[_result()])
    ex = _executor(llm, seerr)

    response = await ex.handle_message(SENDER, "that movie was great")

    seerr.search.assert_not_awaited()
    assert response == "Glad you liked it."


@pytest.mark.asyncio
async def test_probe_skipped_when_user_asked_a_question():
    """The user's own question gets answered, never probe-searched."""
    llm = _llm(LLMDecision(action=Action.REPLY, message="I can search, recommend, and request titles. What are you in the mood for?"))
    seerr = MagicMock()
    seerr.search = AsyncMock(return_value=[_result()])
    ex = _executor(llm, seerr)

    await ex.handle_message(SENDER, "what can you do?")

    seerr.search.assert_not_awaited()
