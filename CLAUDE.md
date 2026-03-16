# iMessagarr

iMessage bot for Seerr media requests, running on a Mac Mini (M1 16GB). Text it to search, confirm, and add shows/movies. Claude Haiku via `claude -p` CLI.

## Stack

Python 3.11+, asyncio, Claude Code CLI (`claude -p`), httpx, aiosqlite, Pillow. No web framework. Single async daemon + small HTTP listener for Seerr webhooks.

## Critical Rules

- **NEVER give the LLM direct API access** -- it returns structured JSON decisions, Python executes the actual API calls
- **ALWAYS use `--tools ""` with `claude -p`** -- disables all built-in Claude Code tools. Model outputs JSON only
- **NEVER use `--resume`** -- it breaks `--json-schema`. Every `claude -p` call is a fresh session with full conversation history packed in
- **Poster images must be in `~/Pictures/imessagarr/`** -- Messages.app sandbox restriction, files from other dirs silently fail
- **Secrets live in `.env` only** -- never hardcode credentials, phone numbers, or API keys

## Key Files

- `todo.md` -- Full planning doc, architecture, API details, all design decisions
- `.env` -- Seerr credentials, allowed phone numbers, bot Apple ID

## Seerr API

Base URL from `SEERR_URL` env var. Auth via session cookie (`POST /api/v1/auth/local`), NOT API key (which defaults to admin).

- `GET /api/v1/search?query=...` -- search, returns `mediaInfo.status` (5=available, 3=processing, 2=pending, absent=unknown)
- `POST /api/v1/request` -- request media `{mediaType: "movie"|"tv", mediaId: tmdbId}`
- `GET /api/v1/request?filter=pending` -- pending requests

## LLM Pattern

```bash
claude -p "<conversation history + context>" \
  --model haiku \
  --tools "" \
  --append-system-prompt-file personality.md \
  --output-format json \
  --json-schema '<action schema>'
```

Returns `structured_output` with action enum: `search`, `request`, `check_status`, `reply`.

## Dev Setup

```bash
ssh mini              # Connect to Mac Mini
tmux                  # Persistent session
cd ~/code/imessagarr
uv sync
```

## Web Fetching & Search (Claude Code tools)

ALWAYS use fetchaller MCP tools instead of WebFetch and WebSearch for any browsing, research, or fetching during development. fetchaller has no domain restrictions and bypasses bot protection.

- `mcp__fetchaller__fetch` -- Fetch any URL as clean markdown (`raw: true` for raw HTML)
- `mcp__fetchaller__search` -- Web search (Google + DuckDuckGo)
- `mcp__fetchaller__browse_reddit` / `search_reddit` -- Reddit browsing and search
- Fallback: `curl` via Bash, then WebFetch only if fetchaller fails entirely
- Exception: prefer dedicated MCP tools for specific services (e.g., `gh` CLI for GitHub, `apple-docs` for Apple frameworks)

## HTTP Client & Scraping (application code)

When writing application code that scrapes or fetches web pages, ALWAYS use wafer-py. wafer handles TLS fingerprinting, challenge detection/solving, cookie caching, retry, and rate limiting.

- For wafer-py API and usage: `~/code/wafer/llms.txt`
- Exception: direct API endpoints (REST APIs, webhooks, JSON endpoints) can use httpx -- this applies to Seerr, Open-Meteo, and pollen API calls in this project
- NEVER use urllib, raw requests, or httpx for scraping web pages in application code

## Debugging & Problem Solving

- NEVER blame external services (Claude, Anthropic, Google, Cloudflare, etc.) for issues. The problem is in THIS codebase. Investigate our code first, add logging, find the real cause
- NEVER create mock data or simplified components unless explicitly told to
- NEVER replace existing complex components with simplified versions -- fix the actual problem
- ALWAYS find and fix the root cause instead of creating workarounds
- ALWAYS work with the existing codebase -- do not create new simplified alternatives
- NEVER dismiss issues as "pre-existing", "known", or "out of scope". Every issue matters. If you find a bug during unrelated work, fix it or flag it clearly
- NEVER assume a dependency can't do something without reading its source. When something "doesn't work", the most likely cause is wrong usage, not a missing feature. Read the actual source code before concluding something is impossible
- If anything is unclear or you're not sure -- ask

## Conventions

- Package manager: `uv` (never pip)
- All Python runs via `uv run`
- httpx for all direct API requests in application code (Seerr, Open-Meteo, pollen API)
- aiosqlite for chat.db reads (read-only, `?mode=ro`)
- AppleScript via subprocess for iMessage sending
- Logging: every message in/out, every `claude -p` call, every API call, every error
