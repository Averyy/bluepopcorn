"""Multi-turn conversation test harness for BluePopcorn.

Runs realistic conversation scenarios against the live bot (real LLM + real Seerr)
and checks that each turn picks the right action.

Usage:
    uv run python test_conversations.py                  # Run all scenarios
    uv run python test_conversations.py --scenario A     # Run one scenario
    uv run python test_conversations.py --delay 5        # Faster re-runs
    uv run python test_conversations.py -v               # Verbose (show prompts)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from bluepopcorn.actions import ActionExecutor
from bluepopcorn.config import load_settings
from bluepopcorn.llm import LLMClient
from bluepopcorn.memory import UserMemory
from bluepopcorn.seerr import SeerrClient
from bluepopcorn.types import LLMDecision

TEST_SENDER = "test-user"


@dataclass
class Turn:
    message: str
    expect_action: str | None = None           # e.g. "search", "recommend"
    alt_action: str | list[str] | None = None   # acceptable alternative action(s)
    expect_media_type: str | None = None       # e.g. "movie", "tv"
    expect_keywords: list[str] = field(default_factory=list)  # in response text
    reject_keywords: list[str] = field(default_factory=list)  # NOT in response
    alt_keywords: list[str] = field(default_factory=list)      # required if alt_action matches
    alt_tmdb_id: int | None = None             # if alt_action is request, verify correct tmdb_id


@dataclass
class TurnResult:
    scenario: str
    turn_num: int
    message: str
    expected_action: str | None
    actual_action: str
    actual_media_type: str | None
    response: str
    passed: bool
    failures: list[str]
    duration: float


SCENARIOS: dict[str, list[Turn]] = {
    "A": [
        Turn("what's Severance about?", expect_action="search"),
        Turn("add it", expect_action="request",
             alt_action="reply"),  # may ask about seasons or say already available
    ],
    "B": [
        Turn("recommend me some sci-fi movies", expect_action="recommend", expect_media_type="movie"),
        Turn("tell me more about the second one", expect_action="search",
             alt_action="reply"),  # reply from context is also valid
        Turn("yeah add that one", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
    ],
    "C": [
        Turn("what's Bugonia about?", expect_action="search"),
        Turn("when does white lotus come back", expect_action="search"),
        Turn("what's new on the server", expect_action="recent"),
        Turn("any good horror shows?", expect_action="recommend", expect_media_type="tv"),
    ],
    "D": [
        Turn("recommend me a good movie", expect_action="recommend", expect_media_type="movie"),
        Turn("recommend some good tv shows", expect_action="recommend", expect_media_type="tv"),
        Turn("add the bear", expect_action="search"),
    ],
    "E": [
        Turn("recommend me a good sci-fi movie", expect_action="recommend", expect_media_type="movie"),
        Turn("what do you know about me?", expect_action="reply"),
        Turn("recommend something else", expect_action="recommend"),
    ],
    "F": [
        Turn("the first greenland movie", expect_action="search"),
        Turn("is avatar done downloading", expect_action="search"),
        Turn("2001 a space odyssey", expect_action="search"),
        Turn("that new tom cruise movie", expect_action="search"),
    ],
    # --- New scenarios for deeper coverage ---
    "G": [
        # Ambiguous intent — "is it good?" should search for info, not check_status
        Turn("have you heard of Dune", expect_action="search",
             alt_action="reply"),  # casual question, reply is fine
        Turn("is it good?", expect_action="search",
             alt_action="reply"),
        Turn("add part 2", expect_action="search",
             alt_action="reply", alt_keywords=["already"]),  # may already be in library
    ],
    "H": [
        # Genre + year combos
        Turn("best comedy movies from 2024", expect_action="recommend", expect_media_type="movie"),
        Turn("what about 2025?", expect_action="recommend", expect_media_type="movie"),
        Turn("any animated shows?", expect_action="recommend", expect_media_type="tv"),
    ],
    "I": [
        # Status check flow
        Turn("what's pending?", expect_action="recent"),
        Turn("is project hail mary done yet?", expect_action="search"),
        Turn("what else is downloading?", expect_action="recent"),
    ],
    "J": [
        # Rapid topic switches with similar-to
        Turn("something like breaking bad", expect_action="recommend"),
        Turn("actually what about something like the office", expect_action="recommend"),
        Turn("add the first one", expect_action="request",
             alt_action="search"),  # might search to disambiguate
    ],
    "K": [
        # Natural phrasing variations
        Turn("put on interstellar", expect_action="search"),
        Turn("can you grab me the new gladiator", expect_action="search"),
        Turn("throw on some action movies", expect_action="recommend", expect_media_type="movie"),
    ],
    "L": [
        # Multi-word title edge cases
        Turn("how to train your dragon", expect_action="search"),
        Turn("no country for old men", expect_action="search"),
        Turn("the boy who harnessed the wind", expect_action="search"),
    ],
    # --- Memory, preference-aware recs, and tricky searches ---
    "M": [
        # Preference-aware recs and tricky searches
        Turn("recommend me a Denis Villeneuve movie", expect_action="recommend", expect_media_type="movie"),
        Turn("what about his tv shows", expect_action="recommend", expect_media_type="tv",
             alt_action="reply"),
        Turn("search for arrival", expect_action="search"),
    ],
    "N": [
        # Genre browsing and follow-up
        Turn("recommend me a thriller", expect_action="recommend"),
        Turn("any good Korean movies?", expect_action="recommend", expect_media_type="movie"),
        Turn("what about Korean dramas", expect_action="recommend", expect_media_type="tv"),
    ],
    "O": [
        # Sequel / franchise disambiguation
        Turn("add john wick", expect_action="search"),
        Turn("the fourth one", expect_action="search",
             alt_action="reply"),  # may reply from context if results already shown
        Turn("what about the matrix", expect_action="search"),
    ],
    "P": [
        # Foreign / non-English title handling
        Turn("parasite", expect_action="search"),
        Turn("squid game", expect_action="search"),
        Turn("anything like parasite?", expect_action="recommend"),
    ],
    "Q": [
        # Vague / conversational queries the bot shouldn't fumble
        Turn("i'm bored", expect_action="recommend",
             alt_action="reply"),
        Turn("what should I watch tonight", expect_action="recommend",
             alt_action="reply"),  # may suggest from trending results already shown
        Turn("something short, like under 2 hours", expect_action="recommend",
             alt_action="reply"),
    ],
    "R": [
        # Actor/director search
        Turn("anything with oscar isaac", expect_action="search",
             alt_action="recommend"),
        Turn("what has christopher nolan directed", expect_action="search",
             alt_action="recommend"),
        Turn("add oppenheimer", expect_action="search",
             alt_action=["request", "reply"], alt_tmdb_id=872585,
             alt_keywords=["already"]),
    ],
    "S": [
        # Release date / airing questions
        Turn("when does the last of us come back", expect_action="search"),
        Turn("when does mission impossible come out", expect_action="search"),
        Turn("what movies are coming out this summer", expect_action="recommend",
             alt_action="search"),
    ],
    "T": [
        # Typos and partial titles
        Turn("sevrance", expect_action="search"),
        Turn("breaking baf", expect_action="search"),
        Turn("the white lotis", expect_action="search"),
    ],
    # --- Preference-aware + natural user flows ---
    "U": [
        # Personalized recs — genre + follow-up
        Turn("recommend me a dark psychological thriller", expect_action="recommend"),
        Turn("anything darker?", expect_action="recommend",
             alt_action="reply"),
        Turn("recommend me a documentary", expect_action="recommend"),
    ],
    "V": [
        # Real user browsing session — quick fire questions
        Turn("what's trending right now", expect_action="recommend"),
        Turn("what about just movies", expect_action="recommend", expect_media_type="movie",
             alt_action="reply"),  # may filter from context
        Turn("add the best one", expect_action="request",
             alt_action="search"),
        Turn("what's new on plex", expect_action="recent"),
    ],
    "W": [
        # Correcting yourself mid-conversation
        Turn("add game of thrones", expect_action="search"),
        Turn("wait no, I meant house of the dragon", expect_action="search",
             alt_action=["request", "reply"], alt_tmdb_id=94997),  # may reply from prior results or request directly
        Turn("is that one any good?", expect_action="search",
             alt_action="reply"),
    ],
    "X": [
        # Similar-to + search in natural flow
        Turn("what's something like inception", expect_action="recommend"),
        Turn("what about tenet, is that good?", expect_action="search",
             alt_action="reply"),  # may answer from similar-to results context
        Turn("add it", expect_action="request",
             alt_action=["reply", "search"], alt_keywords=["already"]),  # may already be in library, or search first
    ],
    "Y": [
        # Specific decade/era requests
        Turn("recommend classic 90s action movies", expect_action="recommend", expect_media_type="movie"),
        Turn("what about 80s horror", expect_action="recommend"),
        Turn("anything from the 2010s that's really good", expect_action="recommend"),
    ],
    "Z": [
        # Testing "add" vs "tell me about" distinction
        Turn("tell me about the penguin", expect_action="search"),
        Turn("actually just add it", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
        Turn("what about shogun, is it worth watching?", expect_action="search"),
        Turn("add it", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
    ],
    # --- New feature scenarios ---
    "AA": [
        # Upcoming releases
        Turn("what movies are coming out soon", expect_action="recommend"),
        Turn("any upcoming tv shows?", expect_action="recommend", expect_media_type="tv"),
        Turn("what's coming out this year", expect_action="recommend",
             alt_action="search"),
    ],
    "AB": [
        # Season selection — multi-season show
        Turn("add breaking bad", expect_action="search"),
        Turn("just season 1", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
    ],
    "AC": [
        # Season selection — latest season
        Turn("add the latest season of grey's anatomy", expect_action="search"),
        Turn("yeah the newest one", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
    ],
    "AD": [
        # Season selection — all seasons (LLM may skip search and request directly)
        Turn("add all of friends", expect_action="search",
             alt_action="request"),
        Turn("all seasons", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
    ],
    "AE": [
        # Collections — search shows collection info, then request whole collection
        Turn("search for the dark knight", expect_action="search"),
        Turn("add the whole collection", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
    ],
    "AF": [
        # Collections — single movie from a collection (should NOT batch)
        Turn("add the dark knight rises", expect_action="search"),
        Turn("yeah just that one", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
    ],
    "AG": [
        # Upcoming + genre filter
        Turn("any upcoming horror movies", expect_action="recommend", expect_media_type="movie"),
        Turn("what about upcoming sci-fi shows", expect_action="recommend", expect_media_type="tv"),
    ],
    "AH": [
        # Mixed flow: upcoming → search → request
        Turn("what's coming out soon", expect_action="recommend"),
        Turn("tell me more about the first one", expect_action="search",
             alt_action="reply"),
        Turn("add it", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
    ],
    "AI": [
        # Edge case: asking about seasons for a movie (should ignore)
        Turn("add inception season 2", expect_action="search",
             alt_action="reply"),  # may reply "it's a movie, no seasons" from context
        Turn("add it", expect_action="request",
             alt_action="reply"),  # may already be in library or clarify
    ],
    "AJ": [
        # Edge case: collection that's already fully available
        Turn("search for toy story", expect_action="search"),
        Turn("add the whole collection", expect_action="request",
             alt_action="reply", alt_keywords=["already"]),
    ],
}


class DecisionCapture:
    """Wraps LLMClient.decide to capture the first decision per turn.

    The bot makes two LLM calls: decide (action routing) then _llm_respond
    (response generation). We only care about the first one — the action decision.
    """

    def __init__(self, llm: LLMClient) -> None:
        self._original = llm.decide
        self.first_decision: LLMDecision | None = None
        self.llm = llm

    async def __call__(self, prompt: str, model: str | None = None, schema: dict | None = None) -> tuple[LLMDecision, dict]:
        result = await self._original(prompt, model=model, schema=schema)
        if self.first_decision is None:
            self.first_decision = result[0]
            self.first_prompt = prompt
        return result

    def install(self) -> None:
        self.llm.decide = self.__call__  # type: ignore[assignment]

    def reset(self) -> None:
        self.first_decision = None
        self.first_prompt = None


def patch_dry_run(seerr: SeerrClient) -> None:
    """Replace request_media with a no-op that logs instead of POSTing.

    All read-only calls (search, discover, recommendations) still hit the real API.
    """
    original = seerr.request_media

    async def fake_request(
        media_type: str, tmdb_id: int, *, seasons: list[int] | None = None
    ) -> dict:
        print(f"  [DRY RUN] Would request {media_type} tmdb:{tmdb_id} (skipped)")
        return {"id": 0, "status": 2, "media": {"tmdbId": tmdb_id, "mediaType": media_type}}

    seerr.request_media = fake_request  # type: ignore[assignment]


def check_turn(
    scenario_name: str,
    turn_num: int,
    turn: Turn,
    decision: LLMDecision | None,
    response: str,
    duration: float,
) -> TurnResult:
    """Evaluate a single turn against expectations."""
    failures: list[str] = []
    actual_action = decision.action.value if decision else "???"
    actual_media_type = decision.media_type if decision else None

    if turn.expect_action and actual_action != turn.expect_action:
        # Check alt_action before failing
        alt_actions = [turn.alt_action] if isinstance(turn.alt_action, str) else (turn.alt_action or [])
        if actual_action in alt_actions:
            # Alt action matched — check alt_keywords and alt_tmdb_id
            resp_lower = response.lower()
            for kw in turn.alt_keywords:
                if kw.lower() not in resp_lower:
                    failures.append(f"alt action matched ({actual_action}) but missing keyword: {kw!r}")
            if turn.alt_tmdb_id and actual_action == "request" and decision and decision.tmdb_id != turn.alt_tmdb_id:
                failures.append(
                    f"alt action used wrong tmdb_id: expected {turn.alt_tmdb_id}, got {decision.tmdb_id}"
                )
            return TurnResult(
                scenario=scenario_name, turn_num=turn_num, message=turn.message,
                expected_action=turn.expect_action, actual_action=actual_action,
                actual_media_type=actual_media_type, response=response,
                passed=len(failures) == 0, failures=failures, duration=duration,
            )
        failures.append(f"action: expected {turn.expect_action}, got {actual_action}")

    if turn.expect_media_type and actual_media_type != turn.expect_media_type:
        failures.append(
            f"media_type: expected {turn.expect_media_type}, got {actual_media_type}"
        )

    resp_lower = response.lower()
    for kw in turn.expect_keywords:
        if kw.lower() not in resp_lower:
            failures.append(f"missing keyword: {kw!r}")
    for kw in turn.reject_keywords:
        if kw.lower() in resp_lower:
            failures.append(f"unwanted keyword: {kw!r}")

    return TurnResult(
        scenario=scenario_name,
        turn_num=turn_num,
        message=turn.message,
        expected_action=turn.expect_action,
        actual_action=actual_action,
        actual_media_type=actual_media_type,
        response=response,
        passed=len(failures) == 0,
        failures=failures,
        duration=duration,
    )


async def run_scenario(
    name: str,
    turns: list[Turn],
    executor: ActionExecutor,
    capture: DecisionCapture,
    delay: float,
    verbose: bool,
) -> list[TurnResult]:
    """Run a single scenario and return results for each turn."""
    results: list[TurnResult] = []

    # Reset session state
    executor._clear_context(TEST_SENDER)
    executor._cli_history.pop(TEST_SENDER, None)
    executor._sent_posters.pop(TEST_SENDER, None)
    executor._session_start.pop(TEST_SENDER, None)
    executor._prompt_cache.pop(TEST_SENDER, None)
    executor._prompt_cache_ctx_count.pop(TEST_SENDER, None)
    # Reset memory file to clean baseline (prevents cross-scenario contamination)
    executor.memory.replace_section(TEST_SENDER, "Preferences", [])
    executor.memory.replace_section(TEST_SENDER, "Likes", [])
    executor.memory.replace_section(TEST_SENDER, "Dislikes", [])

    for i, turn in enumerate(turns, 1):
        capture.reset()
        if verbose:
            print(f"\n  [{name}.{i}] Sending: {turn.message}")

        start = time.monotonic()
        try:
            response = await executor.handle_message(TEST_SENDER, turn.message)
        except Exception as e:
            response = f"[ERROR: {e}]"
        elapsed = time.monotonic() - start

        if verbose and capture.first_prompt:
            print(f"  --- PROMPT (decision call) ---")
            print(capture.first_prompt)
            print(f"  --- END PROMPT ---\n")

        result = check_turn(name, i, turn, capture.first_decision, response, elapsed)
        results.append(result)

        status = "PASS" if result.passed else "FAIL"
        action_str = result.actual_action
        if result.actual_media_type:
            action_str += f"({result.actual_media_type})"

        # Truncate response for display
        resp_preview = response[:120].replace("\n", " ")
        if len(response) > 120:
            resp_preview += "..."
        print(f"  [{name}.{i}] {status} | action={action_str} | {resp_preview}")

        if not result.passed:
            for f in result.failures:
                print(f"         ^ {f}")

        # Wait between turns (simulates user typing gap)
        if i < len(turns):
            await asyncio.sleep(delay)

    return results


def print_summary(all_results: list[TurnResult]) -> None:
    """Print a summary table."""
    print("\n" + "=" * 70)
    print(f"{'Scenario':>10} {'Turn':>5} {'Expected':>12} {'Actual':>12} {'Result':>8} {'Time':>6}")
    print("-" * 70)
    for r in all_results:
        status = "PASS" if r.passed else "FAIL"
        exp = r.expected_action or "-"
        print(f"{r.scenario:>10} {r.turn_num:>5} {exp:>12} {r.actual_action:>12} {status:>8} {r.duration:>5.1f}s")
        if not r.passed:
            for f in r.failures:
                print(f"{'':>42} ^ {f}")

    passed = sum(1 for r in all_results if r.passed)
    total = len(all_results)
    print("-" * 70)
    print(f"{'':>10} {'':>5} {'':>12} {'':>12} {passed}/{total}")
    print("=" * 70)


async def main() -> None:
    parser = argparse.ArgumentParser(description="BluePopcorn conversation tests")
    parser.add_argument("--scenario", "-s", help="Run only this scenario (A-L)")
    parser.add_argument("--delay", "-d", type=float, default=10, help="Seconds between turns (default: 10)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show extra detail")
    parser.add_argument("--live", action="store_true", help="Actually submit requests to Seerr (default: dry run)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Show LLM decisions even in non-verbose mode
    logging.getLogger("bluepopcorn.actions").setLevel(logging.INFO)
    logging.getLogger("bluepopcorn.llm").setLevel(logging.INFO)

    settings = load_settings()
    seerr = SeerrClient(settings)
    llm = LLMClient(settings)
    memory = UserMemory(settings)

    executor = ActionExecutor(
        seerr=seerr,
        llm=llm,
        sender=None,
        posters=None,
        memory=memory,
        monitor=None,
        settings=settings,
    )

    if not args.live:
        patch_dry_run(seerr)
        print("(dry run — requests won't be submitted. Use --live to actually request.)\n")

    capture = DecisionCapture(llm)
    capture.install()

    # Pick scenarios
    if args.scenario:
        names = [s.upper() for s in args.scenario.split(",")]
        scenarios = {n: SCENARIOS[n] for n in names if n in SCENARIOS}
        if not scenarios:
            print(f"Unknown scenario(s): {args.scenario}. Available: {', '.join(SCENARIOS)}")
            sys.exit(1)
    else:
        scenarios = SCENARIOS

    all_results: list[TurnResult] = []

    for name, turns in scenarios.items():
        print(f"\n--- Scenario {name} ({len(turns)} turns) ---")
        results = await run_scenario(name, turns, executor, capture, args.delay, args.verbose)
        all_results.extend(results)

    print_summary(all_results)

    await seerr.close()

    # Exit code: 0 if all passed, 1 if any failed
    if not all(r.passed for r in all_results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
