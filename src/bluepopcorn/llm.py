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

# Project root (where personality.md and memory.md live)
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
        """Load personality.md + instructions.md + memory.md as inline system prompt."""
        if self._system_prompt is not None:
            return self._system_prompt

        parts: list[str] = []
        for filename in ("personality.md", "instructions.md", "memory.md"):
            path = PROJECT_ROOT / filename
            if path.exists():
                parts.append(path.read_text().strip())

        self._system_prompt = "\n\n".join(parts)
        return self._system_prompt

    async def decide(
        self, prompt: str, model: str | None = None
    ) -> tuple[LLMDecision, dict]:
        """Call claude -p and return the structured decision.

        Returns (decision, metadata) where metadata includes cost, duration, etc.
        """
        use_model = model or self.model
        system_prompt = self._load_system_prompt()
        schema_json = json.dumps(LLM_JSON_SCHEMA)

        cmd = [
            self._claude_path,
            "-p", prompt,
            "--model", use_model,
            "--tools", "",
            "--output-format", "json",
            "--json-schema", schema_json,
            "--system-prompt", system_prompt,
        ]

        log.info(
            "LLM call: model=%s, prompt_len=%d, system_len=%d",
            use_model, len(prompt), len(system_prompt),
        )
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
            # Kill the zombie subprocess
            proc.kill()
            kill_stdout, kill_stderr = await proc.communicate()
            out = kill_stdout.decode("utf-8", errors="ignore").strip() if kill_stdout else ""
            err = kill_stderr.decode("utf-8", errors="ignore").strip() if kill_stderr else ""
            log.error(
                "LLM call timed out after %ds (model=%s, prompt_len=%d)%s%s",
                self.timeout, use_model, len(prompt),
                f"\n  stdout: {out[:500]}" if out else "",
                f"\n  stderr: {err[:500]}" if err else "",
            )
            # Try fallback model if this was the primary
            if use_model == self.model and use_model != self.fallback_model:
                log.info("Retrying with fallback model: %s", self.fallback_model)
                return await self.decide(prompt, model=self.fallback_model)
            raise

        duration = time.monotonic() - start

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="ignore").strip()
            out = stdout.decode("utf-8", errors="ignore").strip()
            log.error(
                "LLM call failed (rc=%d, model=%s)%s%s",
                proc.returncode, use_model,
                f"\n  stdout: {out[:500]}" if out else "",
                f"\n  stderr: {err[:500]}" if err else "",
            )
            # Don't retry on signal kills (negative rc = killed by signal, e.g. SIGTERM during shutdown)
            if proc.returncode < 0:
                raise RuntimeError(f"claude -p killed by signal {-proc.returncode}")
            # Try fallback model if this was the primary
            if use_model == self.model and use_model != self.fallback_model:
                log.info("Retrying with fallback model: %s", self.fallback_model)
                return await self.decide(prompt, model=self.fallback_model)
            raise RuntimeError(f"claude -p failed (rc={proc.returncode}): {err or out[:200]}")

        raw = stdout.decode("utf-8", errors="ignore").strip()
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as e:
            log.error("Failed to parse LLM response: %s", raw[:500])
            raise RuntimeError(f"Invalid JSON from claude -p: {e}") from e

        # Extract structured_output (where --json-schema puts the parsed object)
        structured = response.get("structured_output")
        if structured is None:
            log.error("No structured_output in response: %s", raw[:500])
            raise RuntimeError("No structured_output in claude -p response")

        decision = LLMDecision.from_dict(structured)

        metadata = {
            "model": use_model,
            "duration_s": round(duration, 2),
            "estimated_cost_usd": response.get("total_cost_usd", 0),
            "session_id": response.get("session_id"),
        }
        log.info(
            "LLM decision: action=%s, model=%s, duration=%.1fs, estimated_cost=$%.6f",
            decision.action.value, use_model, duration, metadata["estimated_cost_usd"],
        )
        return decision, metadata
