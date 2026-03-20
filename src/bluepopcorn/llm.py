from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path

from .config import Settings
from .types import LLM_JSON_SCHEMA, LLMDecision

log = logging.getLogger(__name__)

# Project root (where personality.md and instructions.md live)
PROJECT_ROOT = Path(__file__).parent.parent.parent


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.model = settings.model
        self.fallback_model = settings.fallback_model
        self.timeout = settings.llm_timeout
        self._system_prompt: str | None = None
        # Resolve claude CLI path at init (may not be in PATH for launchd)
        self._claude_path = shutil.which("claude") or str(
            Path.home() / ".local" / "bin" / "claude"
        )

    def _load_system_prompt(self) -> str:
        """Load personality.md + instructions.md as inline system prompt."""
        if self._system_prompt is not None:
            return self._system_prompt

        parts: list[str] = []
        for filename in ("personality.md", "instructions.md"):
            path = PROJECT_ROOT / filename
            if path.exists():
                parts.append(path.read_text().strip())

        self._system_prompt = "\n\n".join(parts)
        return self._system_prompt

    async def _call_claude(
        self,
        prompt: str,
        schema: dict,
        system_prompt: str,
        model: str,
        *,
        label: str = "call",
    ) -> tuple[dict, float]:
        """Run claude -p subprocess, parse JSON, return (structured_output, duration).

        Raises RuntimeError on failure or timeout.
        """
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

        log.info("LLM %s: model=%s, prompt_len=%d", label, model, len(prompt))
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

    async def decide(
        self, prompt: str, model: str | None = None, schema: dict | None = None,
    ) -> tuple[LLMDecision, dict]:
        """Call claude -p and return the structured decision.

        Returns (decision, metadata) where metadata includes cost, duration, etc.
        Pass schema to override the default LLM_JSON_SCHEMA (e.g. LLM_RESPOND_SCHEMA).
        """
        use_model = model or self.model
        system_prompt = self._load_system_prompt()

        try:
            structured, duration = await self._call_claude(
                prompt, schema or LLM_JSON_SCHEMA, system_prompt, use_model,
                label="decide",
            )
        except (asyncio.TimeoutError, RuntimeError) as e:
            # Try fallback model if this was the primary
            if use_model == self.model and use_model != self.fallback_model:
                # Don't retry signal kills
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
        system_prompt: str = "You are a helpful assistant that summarizes conversations.",
        model: str | None = None,
    ) -> dict:
        """Call claude -p with a custom JSON schema and return the raw dict.

        Used by compression to summarize conversations with arbitrary schemas.
        """
        use_model = model or self.model
        try:
            structured, duration = await self._call_claude(
                prompt, schema, system_prompt, use_model, label="summarize",
            )
        except (asyncio.TimeoutError, RuntimeError) as e:
            if use_model == self.model and use_model != self.fallback_model:
                if isinstance(e, RuntimeError) and "killed by signal" in str(e):
                    raise
                log.info("Summarize retrying with fallback model: %s", self.fallback_model)
                return await self.summarize(prompt, schema, system_prompt, model=self.fallback_model)
            raise
        log.info("LLM summarize done: model=%s, duration=%.1fs", use_model, duration)
        return structured
