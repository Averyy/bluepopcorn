from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from .config import Settings
from .prompts import COMPRESSION_SYSTEM_PROMPT, SYSTEM_PROMPT
from .schemas import DECIDE_SCHEMA
from .types import LLMDecision

log = logging.getLogger(__name__)

# SDK model IDs — the subprocess path accepts shorthand ("haiku", "sonnet")
# but the Anthropic SDK needs full model identifiers.
_SDK_MODEL_MAP = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
}


class LLMAuthError(Exception):
    """Authentication failed — API key missing or invalid."""


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.model
        self.fallback_model = settings.fallback_model
        self.timeout = settings.llm_timeout
        self._use_sdk = bool(settings.anthropic_api_key)

        if self._use_sdk:
            from anthropic import (
                APIError,
                AsyncAnthropic,
                AuthenticationError,
                PermissionDeniedError,
            )

            self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
            self._auth_errors = (AuthenticationError, PermissionDeniedError)
            self._api_error = APIError
            log.info("LLM client: Anthropic SDK (API key)")
        else:
            self._client = None
            self._auth_errors = ()
            self._api_error = None
            log.warning(
                "LLM client: claude CLI fallback (no ANTHROPIC_API_KEY "
                "— requires manual login, testing only)"
            )

        # Always resolve CLI path for subprocess fallback
        self._claude_path = shutil.which("claude") or str(
            Path.home() / ".local" / "bin" / "claude"
        )

    async def close(self) -> None:
        """Close the underlying HTTP client (no-op for subprocess mode)."""
        if self._client is not None:
            await self._client.close()

    # ── Dispatcher ───────────────────────────────────────────────────

    async def _call_claude(
        self,
        prompt: str,
        schema: dict,
        system_prompt: str,
        model: str,
        *,
        label: str = "call",
    ) -> tuple[dict, float]:
        """Route to SDK or subprocess, return (structured_output, duration)."""
        if self._use_sdk:
            return await self._call_sdk(
                prompt, schema, system_prompt, model, label=label,
            )
        return await self._call_subprocess(
            prompt, schema, system_prompt, model, label=label,
        )

    # ── SDK path (primary — API key auth) ────────────────────────────

    async def _call_sdk(
        self,
        prompt: str,
        schema: dict,
        system_prompt: str,
        model: str,
        *,
        label: str = "call",
    ) -> tuple[dict, float]:
        """Call the Anthropic API directly via SDK.

        Uses tool_use for structured output — output_config's grammar compiler
        times out on complex schemas (16+ properties), but tool_use handles
        them reliably.
        """
        sdk_model = _SDK_MODEL_MAP.get(model, model)
        log.info("LLM %s: model=%s, prompt_len=%d", label, model, len(prompt))
        start = time.monotonic()

        try:
            response = await asyncio.wait_for(
                self._client.messages.create(
                    model=sdk_model,
                    max_tokens=4096,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                    tools=[{
                        "name": "respond",
                        "description": "Structured response",
                        "input_schema": schema,
                    }],
                    tool_choice={"type": "tool", "name": "respond"},
                ),
                timeout=self.timeout,
            )
        except self._auth_errors as e:
            log.error("LLM %s auth failed: %s", label, e)
            raise LLMAuthError(
                "Anthropic API key invalid or expired — check ANTHROPIC_API_KEY in .env"
            ) from None
        except asyncio.TimeoutError:
            log.error(
                "LLM %s timed out after %ds (model=%s, prompt_len=%d)",
                label, self.timeout, model, len(prompt),
            )
            raise
        except self._api_error as e:
            # Rate limits, overloads, server errors, connection errors —
            # wrap as RuntimeError so the fallback retry logic catches them.
            log.error("LLM %s API error (model=%s): %s", label, model, e)
            raise RuntimeError(f"SDK {label} API error: {e}") from None

        duration = time.monotonic() - start

        # Extract structured data from the tool_use content block
        tool_block = next(
            (b for b in response.content if b.type == "tool_use"), None,
        )
        if tool_block is None:
            log.error(
                "LLM %s: no tool_use block in response (stop_reason=%s, content=%s)",
                label, response.stop_reason,
                [b.type for b in response.content],
            )
            raise RuntimeError(
                f"SDK {label}: no tool_use in response (stop_reason={response.stop_reason})"
            )

        return tool_block.input, duration

    # ── Subprocess path (fallback — Claude Code CLI / OAuth) ─────────

    async def _call_subprocess(
        self,
        prompt: str,
        schema: dict,
        system_prompt: str,
        model: str,
        *,
        label: str = "call",
    ) -> tuple[dict, float]:
        """Run claude -p subprocess. Testing only — requires manual OAuth login."""
        schema_json = json.dumps(schema)
        cmd = [
            self._claude_path,
            "-p", prompt,
            "--model", model,
            "--tools", "",
            "--output-format", "json",
            "--json-schema", schema_json,
            "--system-prompt", system_prompt,
        ]

        log.info("LLM %s: model=%s, prompt_len=%d (subprocess)", label, model, len(prompt))
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            kill_stdout, kill_stderr = await proc.communicate()
            out = kill_stdout.decode("utf-8", errors="ignore").strip() if kill_stdout else ""
            err = kill_stderr.decode("utf-8", errors="ignore").strip() if kill_stderr else ""
            log.error(
                "LLM %s timed out after %ds (model=%s, prompt_len=%d)%s%s",
                label, self.timeout, model, len(prompt),
                f"\n  stdout: {out[:500]}" if out else "",
                f"\n  stderr: {err[:500]}" if err else "",
            )
            raise

        duration = time.monotonic() - start

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore").strip()
            out = stdout.decode("utf-8", errors="ignore").strip()
            log.error(
                "LLM %s failed (rc=%d, model=%s)%s%s",
                label, proc.returncode, model,
                f"\n  stdout: {out[:500]}" if out else "",
                f"\n  stderr: {err[:500]}" if err else "",
            )
            # Detect auth failures (OAuth token expired)
            combined = (err + out).lower()
            if "authentication_error" in combined or "token has expired" in combined:
                raise LLMAuthError(
                    "Claude Code OAuth token expired — set ANTHROPIC_API_KEY in .env "
                    "for unattended use"
                )
            # Negative rc = killed by signal (e.g. SIGTERM during shutdown)
            if proc.returncode < 0:
                raise RuntimeError(f"claude -p killed by signal {-proc.returncode}")
            raise RuntimeError(f"claude -p {label} failed (rc={proc.returncode}): {err or out[:200]}")

        raw = stdout.decode("utf-8", errors="ignore").strip()
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("Failed to parse LLM %s response: %s", label, raw[:500])
            raise RuntimeError(f"Invalid JSON from claude -p {label}: {e}") from e

        structured = response.get("structured_output")
        if structured is None:
            log.error("No structured_output in %s response: %s", label, raw[:500])
            raise RuntimeError(f"No structured_output in claude -p {label} response")

        return structured, duration

    # ── Public API ───────────────────────────────────────────────────

    async def decide(
        self, prompt: str, model: str | None = None, schema: dict | None = None,
    ) -> tuple[LLMDecision, dict]:
        """Return the structured decision for a user message.

        Returns (decision, metadata). Pass schema to override the default
        DECIDE_SCHEMA (e.g. RESPOND_SCHEMA).
        """
        use_model = model or self.model
        system_prompt = SYSTEM_PROMPT

        try:
            structured, duration = await self._call_claude(
                prompt, schema or DECIDE_SCHEMA, system_prompt, use_model,
                label="decide",
            )
        except LLMAuthError:
            raise
        except (asyncio.TimeoutError, RuntimeError) as e:
            if use_model == self.model and use_model != self.fallback_model:
                if isinstance(e, RuntimeError) and "killed by signal" in str(e):
                    raise
                log.info("Retrying with fallback model: %s", self.fallback_model)
                return await self.decide(prompt, model=self.fallback_model, schema=schema)
            raise

        decision = LLMDecision.from_dict(structured)
        metadata = {
            "model": use_model,
            "duration_s": round(duration, 2),
        }
        log.info(
            "LLM decision: action=%s, model=%s, duration=%.1fs",
            decision.action.value, use_model, duration,
        )
        return decision, metadata

    async def summarize(
        self,
        prompt: str,
        schema: dict,
        system_prompt: str = COMPRESSION_SYSTEM_PROMPT,
        model: str | None = None,
    ) -> dict:
        """Call LLM with a custom JSON schema and return the raw dict.

        Used by compression and digest to get structured summaries.
        """
        use_model = model or self.model
        try:
            structured, duration = await self._call_claude(
                prompt, schema, system_prompt, use_model, label="summarize",
            )
        except LLMAuthError:
            raise
        except (asyncio.TimeoutError, RuntimeError) as e:
            if use_model == self.model and use_model != self.fallback_model:
                if isinstance(e, RuntimeError) and "killed by signal" in str(e):
                    raise
                log.info("Summarize retrying with fallback model: %s", self.fallback_model)
                return await self.summarize(prompt, schema, system_prompt, model=self.fallback_model)
            raise
        log.info("LLM summarize done: model=%s, duration=%.1fs", use_model, duration)
        return structured
