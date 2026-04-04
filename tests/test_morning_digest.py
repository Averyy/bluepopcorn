"""Unit tests for morning digest system.

Tests that the digest routes through the LLM (not Python), respects
user preferences, tracks suggested IDs for rotation, and handles
edge cases gracefully.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from bluepopcorn.config import Settings
from bluepopcorn.morning_digest import MorningDigest
from bluepopcorn.types import MediaStatus, SearchResult
from bluepopcorn.utils import safe_data_path, safe_sender_filename


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_data_dir(tmp_path):
    """Temporary data directory for file-based state."""
    return tmp_path / "data"


@pytest.fixture
def settings(tmp_data_dir):
    """Minimal settings pointing at temp dirs."""
    return Settings(
        seerr_url="http://localhost:5055",
        seerr_api_key="test-key",
        allowed_senders=["+11234567890"],
        data_dir=str(tmp_data_dir),
        memory_dir=str(tmp_data_dir / "memory"),
    )


@pytest.fixture
def mock_seerr():
    mock = AsyncMock()
    mock.get_recently_added.return_value = [
        {"title": "Severance", "tmdbId": 1},
        {"title": "Shogun", "tmdbId": 2},
    ]
    mock.get_pending.return_value = [{"id": 1}, {"id": 2}, {"id": 3}]
    mock.discover_trending.return_value = [
        SearchResult(
            tmdb_id=100, title="Cool Movie", year=2026,
            media_type="movie", overview="A really cool movie about stuff",
            status=MediaStatus.NOT_TRACKED, rating=8.1,
        ),
        SearchResult(
            tmdb_id=200, title="Bad Horror", year=2026,
            media_type="movie", overview="A scary horror film",
            status=MediaStatus.NOT_TRACKED, rating=7.5,
        ),
    ]
    return mock


@pytest.fixture
def mock_llm():
    mock = AsyncMock()
    mock.summarize.return_value = {
        "send": True,
        "message": "Good morning. Severance and Shogun are available. 3 requests pending.\n\nSuggestion: Cool Movie (2026) — A really cool movie, 8.1/10 on TMDB. Want me to add it?",
        "suggested_tmdb_id": 100,
    }
    return mock


@pytest.fixture
def mock_memory():
    mock = MagicMock()
    mock.load.return_value = "# Likes\nGenres: sci-fi\n\n# Dislikes\nGenres: horror"
    return mock


@pytest.fixture
def digest(settings, mock_seerr, mock_llm, mock_memory):
    return MorningDigest(settings, mock_seerr, mock_llm, mock_memory)


SENDER = "+11234567890"


# ── build() — LLM routing ────────────────────────────────────────────

async def test_build_routes_through_llm(digest, mock_llm):
    """Digest message must be composed by the LLM, not Python."""
    result = await digest.build(SENDER)
    mock_llm.summarize.assert_called_once()
    assert result is not None
    assert "Good morning" in result


async def test_build_passes_memory_to_llm(digest, mock_llm, mock_memory):
    """User memory (likes, dislikes) must be included in the LLM prompt."""
    await digest.build(SENDER)
    prompt = mock_llm.summarize.call_args[0][0]
    assert "horror" in prompt  # from dislikes
    assert "sci-fi" in prompt  # from likes


async def test_build_passes_available_and_pending_to_llm(digest, mock_llm):
    """Recently available titles and pending count appear in the prompt."""
    await digest.build(SENDER)
    prompt = mock_llm.summarize.call_args[0][0]
    assert "Severance" in prompt
    assert "Shogun" in prompt
    assert "3" in prompt  # pending count


async def test_build_passes_trending_candidates_to_llm(digest, mock_llm):
    """Trending titles are formatted with tmdb IDs for the LLM."""
    await digest.build(SENDER)
    prompt = mock_llm.summarize.call_args[0][0]
    assert "[tmdb:100]" in prompt
    assert "Cool Movie" in prompt


async def test_build_passes_last_digest_to_llm(digest, mock_llm):
    """Last digest is included so LLM can decide if there's anything new."""
    await digest.build(SENDER, last_digest="Good morning. Old digest.")
    prompt = mock_llm.summarize.call_args[0][0]
    assert "Old digest" in prompt


async def test_build_first_digest_marker(digest, mock_llm):
    """First digest (no last_digest) shows marker in prompt."""
    await digest.build(SENDER, last_digest=None)
    prompt = mock_llm.summarize.call_args[0][0]
    assert "(first digest)" in prompt


# ── build() — skip behaviour ─────────────────────────────────────────

async def test_build_returns_none_when_llm_says_skip(digest, mock_llm):
    """LLM can set send=false to skip the digest entirely."""
    mock_llm.summarize.return_value = {
        "send": False,
        "message": "Good morning. Nothing new.",
        "suggested_tmdb_id": None,
    }
    result = await digest.build(SENDER)
    assert result is None


async def test_build_returns_fallback_on_llm_failure(digest, mock_llm):
    """If the LLM call fails, a fallback message is returned (not silent skip)."""
    mock_llm.summarize.side_effect = RuntimeError("claude -p timed out")
    result = await digest.build(SENDER)
    assert result is not None
    assert "Good morning" in result


async def test_build_returns_none_on_empty_message(digest, mock_llm):
    """Empty LLM message is treated as skip."""
    mock_llm.summarize.return_value = {
        "send": True,
        "message": "",
        "suggested_tmdb_id": None,
    }
    result = await digest.build(SENDER)
    assert result is None


# ── build() — pre-fetched data ───────────────────────────────────────

async def test_build_uses_prefetched_available_and_pending(digest, mock_seerr, mock_llm):
    """When available/pending are passed in, Seerr is not called for them."""
    await digest.build(SENDER, available="Inception", pending="5")
    mock_seerr.get_recently_added.assert_not_called()
    mock_seerr.get_pending.assert_not_called()
    # Trending is always per-user (different exclude_ids)
    mock_seerr.discover_trending.assert_called_once()
    # Verify pre-fetched values appear in prompt
    prompt = mock_llm.summarize.call_args[0][0]
    assert "Inception" in prompt
    assert "5" in prompt


# ── Suggested ID tracking ────────────────────────────────────────────

async def test_suggested_id_saved_after_build(digest, tmp_data_dir):
    """Suggested tmdb_id from LLM response is persisted for rotation."""
    await digest.build(SENDER)
    ids = digest._load_suggested_ids(SENDER)
    assert 100 in ids


async def test_suggested_ids_excluded_from_trending(digest, mock_seerr, tmp_data_dir):
    """Previously suggested IDs are passed as exclude_ids to trending API."""
    # Pre-populate with some suggested IDs
    digest._save_suggested_id(SENDER, 999, existing=[])
    await digest.build(SENDER)
    call_kwargs = mock_seerr.discover_trending.call_args[1]
    assert 999 in call_kwargs["exclude_ids"]


async def test_suggested_id_not_saved_when_none(digest, tmp_data_dir, mock_llm):
    """No tmdb_id saved when LLM doesn't suggest anything."""
    mock_llm.summarize.return_value = {
        "send": True,
        "message": "Good morning. Nothing interesting trending.",
        "suggested_tmdb_id": None,
    }
    await digest.build(SENDER)
    ids = digest._load_suggested_ids(SENDER)
    assert len(ids) == 0


async def test_suggested_id_not_saved_when_invalid_type(digest, tmp_data_dir, mock_llm):
    """Non-integer suggested_tmdb_id is ignored (isinstance check)."""
    mock_llm.summarize.return_value = {
        "send": True,
        "message": "Good morning.",
        "suggested_tmdb_id": "not-a-number",
    }
    await digest.build(SENDER)
    ids = digest._load_suggested_ids(SENDER)
    assert len(ids) == 0


async def test_suggested_ids_capped_at_100(digest, tmp_data_dir):
    """Suggested IDs file is capped at 100 entries."""
    existing = list(range(100))
    digest._save_suggested_id(SENDER, 9999, existing=existing)
    ids = digest._load_suggested_ids(SENDER)
    assert 9999 in ids
    assert len(ids) <= 100
    # Most recent IDs kept (9999 should be last)
    assert ids[-1] == 9999


# ── Suggested IDs file resilience ─────────────────────────────────────

async def test_load_suggested_ids_missing_file(digest):
    """Missing file returns empty list."""
    ids = digest._load_suggested_ids("+19999999999")
    assert ids == []


async def test_load_suggested_ids_corrupt_lines(digest, tmp_data_dir):
    """Corrupt lines in suggested IDs file are skipped, valid ones kept."""
    path = digest._suggested_ids_path(SENDER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("100\nnot_a_number\n200\n\n300\n")
    ids = digest._load_suggested_ids(SENDER)
    assert ids == [100, 200, 300]


async def test_load_suggested_ids_empty_file(digest, tmp_data_dir):
    """Empty file returns empty list."""
    path = digest._suggested_ids_path(SENDER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    ids = digest._load_suggested_ids(SENDER)
    assert ids == []


async def test_load_suggested_ids_preserves_order(digest, tmp_data_dir):
    """IDs are returned in file order (insertion order) for correct truncation."""
    path = digest._suggested_ids_path(SENDER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n2\n3\n4\n5\n")
    ids = digest._load_suggested_ids(SENDER)
    assert ids == [1, 2, 3, 4, 5]


# ── Data fetchers ─────────────────────────────────────────────────────

async def test_fetch_available_returns_titles(digest):
    result = await digest.fetch_available()
    assert result == "Severance, Shogun"


async def test_fetch_available_returns_none_on_empty(digest, mock_seerr):
    mock_seerr.get_recently_added.return_value = []
    result = await digest.fetch_available()
    assert result is None


async def test_fetch_available_returns_none_on_error(digest, mock_seerr):
    mock_seerr.get_recently_added.side_effect = Exception("connection refused")
    result = await digest.fetch_available()
    assert result is None


async def test_fetch_pending_returns_count(digest):
    result = await digest.fetch_pending()
    assert result == "3"


async def test_fetch_pending_returns_none_on_empty(digest, mock_seerr):
    mock_seerr.get_pending.return_value = []
    result = await digest.fetch_pending()
    assert result is None


async def test_fetch_trending_filters_low_rated(digest, mock_seerr):
    """Trending items below 7.0 rating are excluded from candidates."""
    mock_seerr.discover_trending.return_value = [
        SearchResult(
            tmdb_id=300, title="Low Rated", year=2026,
            media_type="movie", overview="Some movie",
            status=MediaStatus.NOT_TRACKED, rating=5.0,
        ),
    ]
    result = await digest.fetch_trending()
    assert result is None


async def test_fetch_trending_filters_in_library(digest, mock_seerr):
    """Items already in the library are excluded from candidates."""
    mock_seerr.discover_trending.return_value = [
        SearchResult(
            tmdb_id=400, title="Already Have", year=2026,
            media_type="movie", overview="Already downloaded",
            status=MediaStatus.AVAILABLE, rating=9.0,
        ),
    ]
    result = await digest.fetch_trending()
    assert result is None


async def test_fetch_trending_includes_tmdb_id(digest):
    """Trending output includes [tmdb:ID] tags for LLM reference."""
    result = await digest.fetch_trending()
    assert "[tmdb:100]" in result
    assert "[tmdb:200]" in result


# ── utils: safe_data_path / safe_sender_filename ──────────────────────

def test_safe_sender_filename_strips_plus():
    assert safe_sender_filename("+16478235569") == "16478235569"


def test_safe_sender_filename_replaces_slash():
    assert safe_sender_filename("+1/234") == "1_234"


def test_safe_data_path_valid():
    p = safe_data_path(Path("/tmp/data"), "last_digest", "+16478235569")
    assert p == Path("/tmp/data/last_digest_16478235569")


def test_safe_data_path_neutralises_traversal():
    """Path traversal attempts are neutralised by sanitisation."""
    p = safe_data_path(Path("/tmp/data"), "test", "../../../etc/passwd")
    assert p.resolve().is_relative_to(Path("/tmp/data").resolve())


# ── Webhook templates ─────────────────────────────────────────────────

def test_webhook_templates_format_correctly():
    """Webhook templates from prompts.py accept $title and $subject."""
    from string import Template
    from bluepopcorn.prompts import (
        WEBHOOK_MEDIA_APPROVED, WEBHOOK_MEDIA_AVAILABLE,
        WEBHOOK_MEDIA_FAILED, WEBHOOK_MEDIA_PENDING, WEBHOOK_FALLBACK,
    )
    assert "Inception" in Template(WEBHOOK_MEDIA_APPROVED).safe_substitute(title="Inception")
    assert "Inception" in Template(WEBHOOK_MEDIA_AVAILABLE).safe_substitute(title="Inception")
    assert "Inception" in Template(WEBHOOK_MEDIA_FAILED).safe_substitute(title="Inception")
    assert "Inception" in Template(WEBHOOK_MEDIA_PENDING).safe_substitute(title="Inception")
    assert "Test" in Template(WEBHOOK_FALLBACK).safe_substitute(subject="Test")


def test_webhook_template_with_braces_in_title():
    """Titles with curly braces must not crash the webhook formatter."""
    from string import Template
    from bluepopcorn.prompts import WEBHOOK_MEDIA_AVAILABLE
    result = Template(WEBHOOK_MEDIA_AVAILABLE).safe_substitute(title="Movie {Special}")
    assert "Movie {Special}" in result


# ── Last digest dedup ─────────────────────────────────────────────────

def test_last_digest_round_trip(tmp_data_dir):
    """_load_last_digest / _save_last_digest round-trip correctly."""
    from bluepopcorn.__main__ import _load_last_digest, _save_last_digest
    data_dir = tmp_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    assert _load_last_digest(data_dir, SENDER) is None
    _save_last_digest(data_dir, SENDER, "Good morning. Test digest.")
    assert _load_last_digest(data_dir, SENDER) == "Good morning. Test digest."


def test_last_digest_overwrite(tmp_data_dir):
    """Saving a new digest overwrites the previous one."""
    from bluepopcorn.__main__ import _load_last_digest, _save_last_digest
    data_dir = tmp_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    _save_last_digest(data_dir, SENDER, "Day 1")
    _save_last_digest(data_dir, SENDER, "Day 2")
    assert _load_last_digest(data_dir, SENDER) == "Day 2"
