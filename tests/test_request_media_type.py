"""Unit tests for request-action media_type defenses.

Covers the helpers that defend ``handle_request`` against an LLM that
returns the right tmdb_id but the wrong media_type.

Reproduces the 2026-05-18 incident: the morning digest seeded
``_last_topic`` for the user with ``tmdb:1439930 movie`` (The Punisher:
One Last Kill). The user replied "Yes". Haiku returned
``action=request, tmdb_id=1439930, media_type=tv``. The request handler
trusted the type and hit ``/api/v1/tv/1439930`` → Seerr 500 → user got
"Something went wrong, try again in a sec." The fix is a context-backed
override that pins the media_type to what Seerr-derived data already
said.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bluepopcorn.actions.request import (
    _known_media_type_for_tmdb,
    handle_request,
)
from bluepopcorn.types import Action, LLMDecision, MediaStatus

SENDER = "+11234567890"


def _executor(
    *,
    topic: dict | None = None,
    cached_prompt: str | None = None,
    context_entries: list[str] | None = None,
):
    """Build a minimal stand-in ActionExecutor for the helper."""
    exec_ = SimpleNamespace()
    exec_._last_topic = {SENDER: topic} if topic else {}
    exec_._prompt_cache = {SENDER: cached_prompt} if cached_prompt else {}
    exec_._context = {}
    if context_entries:
        now = time.time()
        exec_._context[SENDER] = [(now, t) for t in context_entries]
    return exec_


# ── _known_media_type_for_tmdb ────────────────────────────────────────


def test_known_media_type_from_last_topic_movie():
    exec_ = _executor(topic={
        "title": "The Punisher: One Last Kill (2026)",
        "tmdb_id": 1439930,
        "media_type": "movie",
        "set_ts": time.time(),
    })
    assert _known_media_type_for_tmdb(exec_, SENDER, 1439930) == "movie"


def test_known_media_type_from_last_topic_tv():
    exec_ = _executor(topic={
        "title": "Off Campus (2026)",
        "tmdb_id": 273240,
        "media_type": "tv",
        "set_ts": time.time(),
    })
    assert _known_media_type_for_tmdb(exec_, SENDER, 273240) == "tv"


def test_known_media_type_topic_tmdb_mismatch_falls_through():
    """A non-matching topic shouldn't poison the lookup."""
    exec_ = _executor(
        topic={
            "title": "Other Show",
            "tmdb_id": 111,
            "media_type": "tv",
            "set_ts": time.time(),
        },
        cached_prompt="1. The Punisher: One Last Kill (2026) [Movie] tmdb:1439930 - desc",
    )
    assert _known_media_type_for_tmdb(exec_, SENDER, 1439930) == "movie"


def test_known_media_type_from_cached_prompt_topic_injection():
    """LAST_DISCUSSED_TITLE format in the cached prompt is matched."""
    exec_ = _executor(
        cached_prompt="[Last discussed title: Punisher (2026) tmdb:1439930 movie]",
    )
    assert _known_media_type_for_tmdb(exec_, SENDER, 1439930) == "movie"


def test_known_media_type_from_search_result_in_context():
    """format_result_line output in the context buffer is matched."""
    exec_ = _executor(
        context_entries=[
            "[Search results for 'punisher':\n"
            "1. Punisher (2026) [Movie] tmdb:1439930 - desc (Status: not in the library)\n"
            "2. Other (2024) [TV] tmdb:2222 - other (Status: not in the library)\n"
            "]",
        ],
    )
    assert _known_media_type_for_tmdb(exec_, SENDER, 1439930) == "movie"
    assert _known_media_type_for_tmdb(exec_, SENDER, 2222) == "tv"


def test_known_media_type_returns_none_when_unseen():
    exec_ = _executor(cached_prompt="nothing relevant here")
    assert _known_media_type_for_tmdb(exec_, SENDER, 1439930) is None


def test_known_media_type_does_not_cross_result_lines():
    """`[Movie]` on one line must not pair with `tmdb:X` on the next."""
    exec_ = _executor(
        context_entries=[
            "1. Foo (2024) [Movie] tmdb:111 - desc\n"
            "2. Bar (2025) [TV] tmdb:222 - desc",
        ],
    )
    # 111 is on the Movie line, 222 is on the TV line — both should match
    # their own line, not cross over.
    assert _known_media_type_for_tmdb(exec_, SENDER, 111) == "movie"
    assert _known_media_type_for_tmdb(exec_, SENDER, 222) == "tv"


# ── handle_request: media_type correction integration ─────────────────


@pytest.mark.asyncio
async def test_handle_request_corrects_hallucinated_media_type(monkeypatch):
    """The Punisher: One Last Kill scenario end-to-end against a fake Seerr.

    Topic was seeded as movie. The LLM returns media_type=tv. After the
    correction, ``request_media`` must be called with ``"movie"``.
    """
    exec_ = MagicMock()
    exec_._last_topic = {SENDER: {
        "title": "The Punisher: One Last Kill (2026)",
        "tmdb_id": 1439930,
        "media_type": "movie",
        "set_ts": time.time(),
    }}
    exec_._prompt_cache = {
        SENDER: "[Last discussed title: Punisher (2026) tmdb:1439930 movie]",
    }
    exec_._context = {SENDER: []}
    exec_._store_request_context = AsyncMock()
    exec_._add_context = MagicMock()
    exec_.request_tracker = None
    exec_.seerr.get_media_status = AsyncMock(return_value={
        "id": 1439930,
        "title": "The Punisher: One Last Kill",
        "releaseDate": "2026-01-01",
        "mediaInfo": None,
    })
    exec_.seerr.request_media = AsyncMock(return_value={"id": 99})

    decision = LLMDecision(
        action=Action.REQUEST,
        message="Adding Punisher: One Last Kill (2026) to your library.",
        tmdb_id=1439930,
        media_type="tv",  # Haiku's hallucinated type
    )

    response = await handle_request(exec_, decision, SENDER)

    # The corrected media_type was used for both status check and request.
    exec_.seerr.get_media_status.assert_awaited_once_with("movie", 1439930)
    exec_.seerr.request_media.assert_awaited_once()
    args, kwargs = exec_.seerr.request_media.call_args
    assert args[0] == "movie"
    assert args[1] == 1439930
    # No season list for movies.
    assert kwargs.get("seasons") is None
    # The decision was mutated so downstream uses see the corrected type.
    assert decision.media_type == "movie"
    # The user-facing message from the decision is returned on success.
    assert response == decision.message


@pytest.mark.asyncio
async def test_handle_request_leaves_correct_media_type_alone(monkeypatch):
    """If the LLM's media_type already matches context, nothing changes."""
    exec_ = MagicMock()
    exec_._last_topic = {SENDER: {
        "title": "Off Campus (2026)",
        "tmdb_id": 273240,
        "media_type": "tv",
        "set_ts": time.time(),
    }}
    exec_._prompt_cache = {SENDER: "tmdb:273240 tv"}
    exec_._context = {SENDER: []}
    exec_._store_request_context = AsyncMock()
    exec_._add_context = MagicMock()
    exec_.request_tracker = None
    exec_.seerr.get_media_status = AsyncMock(return_value={
        "id": 273240,
        "name": "Off Campus",
        "firstAirDate": "2026-01-01",
        "seasons": [
            {"seasonNumber": 0},
            {"seasonNumber": 1},
        ],
        "mediaInfo": None,
    })
    exec_.seerr.request_media = AsyncMock(return_value={"id": 99})

    decision = LLMDecision(
        action=Action.REQUEST,
        message="Adding Off Campus (2026) to your library.",
        tmdb_id=273240,
        media_type="tv",
    )

    await handle_request(exec_, decision, SENDER)

    args, _ = exec_.seerr.request_media.call_args
    assert args[0] == "tv"
    assert decision.media_type == "tv"
