# BluePopcorn

Smart Seerr MCP server + iMessage bot. Claude Haiku via Anthropic SDK (API key) for iMessage mode, with `claude -p` subprocess as testing fallback.

## Critical Rules

- **Keep BOTH llms.txt files up to date** -- `src/bluepopcorn/prompts.py` LLMS_TXT is MCP-server-only (tools, params, response formats — served at /llms.txt by the MCP HTTP server). `landing/public/llms.txt` is the full project reference (MCP + iMessage + install + env vars — served by the landing site). When tools, params, or behavior change, update both

- **NEVER give the LLM direct API access** -- structured JSON decisions only, Python executes
- **LLM calls use Anthropic SDK (primary) or `claude -p` subprocess (fallback)** -- SDK uses tool_use for structured output (output_config's grammar compiler times out on complex schemas). Subprocess path is testing-only, requires daily manual OAuth login
- **ALWAYS use `--tools ""`** with `claude -p` -- disables all built-in tools (subprocess fallback only)
- **NEVER use `--resume`** -- breaks `--json-schema`. Every call is fresh with history packed in (subprocess fallback only)
- **`--append-system-prompt-file` does NOT exist** -- only `--append-system-prompt` (inline string) (subprocess fallback only)
- **Poster images must be in `~/Pictures/bluepopcorn/`** -- Messages.app sandbox, other dirs silently fail
- **Secrets in `.env` only** -- never hardcode credentials or phone numbers
- **NEVER disable the typing indicator** -- essential UX. Fix bugs instead
- **NEVER rebuild wrapper.swift unless its source changes** -- rebuilding revokes FDA/Accessibility permissions. Python code changes only need a daemon restart
- **After code changes, run `imessage/restart.sh`** -- always restart the daemon after modifying Python code
- **NEVER trigger real Seerr requests when testing** -- CLI tests hit the live API. Only test read-only flows (search, recommend, status, info). Do NOT test "add it", number picking, or any flow that triggers `action=request` / `request_media`. If you need to verify request logic, read the code — don't execute it
- **After significant changes to prompts, actions, or LLM routing, run the conversation tests** -- `uv run python tests/test_conversations.py -s A,E,I` for a quick smoke test (~5 min), or the full suite for thorough validation. Significant = changes to prompts.py, actions/*.py, llm.py, or _build_prompt
- **ALL LLM-facing text must live in `prompts.py`** -- system prompt, status labels, context templates, scenario instructions, compression prompts, error messages. Never hardcode prompt strings in handler files. NEVER move LLM-facing text out of prompts.py into other modules to solve import issues — fix the import issue instead
- **ALL JSON schemas must live in `schemas.py`** -- decide schema, respond schema, compression schemas. Never define schemas inline in other files

## Commands

```bash
uv sync                              # Install deps (MCP server)
uv sync --extra imessage             # Install deps (iMessage bot)

# MCP server
uv run -m bluepopcorn.mcp            # HTTP mode (default :8080)
uv run -m bluepopcorn.mcp --stdio    # stdio mode (local clients)

# iMessage bot
uv run -m bluepopcorn --cli           # CLI test mode
uv run -m bluepopcorn --digest        # One-shot digest
uv run -m bluepopcorn                 # Run daemon
imessage/restart.sh                   # Restart daemon after Python changes
tail -30 bluepopcorn.log              # Recent logs (adjust count as needed)

# Tests
uv run pytest tests/test_morning_digest.py -v             # Digest unit tests (fast, mocked)
uv run pytest tests/test_mcp_tools.py -m integration -v  # MCP integration tests
uv run python tests/test_conversations.py              # Full suite (A-Z + DIGEST)
uv run python tests/test_conversations.py -s A,E,I     # Smoke test (3 scenarios)
uv run python tests/test_conversations.py -s DIGEST    # Digest-only live tests
uv run python tests/test_conversations.py -s X --delay 3  # Single scenario, faster

# Manual restart (imessage/restart.sh does this for you)
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist   # Stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist # Start

# Rebuild wrapper (ONLY if wrapper.swift or Info.plist change — rare)
# WARNING: rebuilding revokes FDA/Accessibility, must re-grant in System Settings
cd imessage
swiftc -o BluePopcorn.app/Contents/MacOS/BluePopcorn wrapper.swift  # Build macOS wrapper
codesign --force --sign - BluePopcorn.app                          # Ad-hoc sign
```

## Key References

- prompts.py -- All LLM-facing text (system prompt, MCP tool descriptions, llms.txt, context templates, instructions, error messages)
- schemas.py -- All JSON schemas and XML tag constants
- docs/ref-seerr-api.md -- Seerr API reference (enums, endpoints, params)
- imessage/config.toml -- Non-secret settings (iMessage bot)

## Architecture

### MCP Server (`bluepopcorn.mcp`)

Stateless MCP server exposing 5 Seerr tools. Runs as HTTP (with Bearer auth) or stdio.

```
MCP client → HTTP POST /mcp (or stdio) → MCP server → Seerr API → JSON response
```

### iMessage Bot (`bluepopcorn`)

Two-call pattern: Haiku decides the action (call 1), Python executes the API call, then Haiku crafts the response using conversation history + API results as context (call 2).

```
User text → Haiku (action) → Python executes API → store results as context → Haiku (response) → send
```

LLM calls use the Anthropic SDK directly when `ANTHROPIC_API_KEY` is set (production), or fall back to `claude -p` subprocess (testing only — requires daily manual OAuth login). The SDK path uses tool_use for structured output enforcement. Custom exception `LLMAuthError` propagates auth failures distinctly from transient errors.

Only exception: bypass commands (status/help/new) use Python responses directly.

## File Layout

```
bluepopcorn/
  src/bluepopcorn/
    mcp/                    # MCP server package
      server.py             # MCP server (5 tools)
      config.py             # MCP config from env vars
      http/app.py           # FastAPI HTTP transport + /llms.txt
      http/middleware.py     # Bearer auth
    llm.py                  # LLM client (SDK primary, subprocess fallback)
    prompts.py              # All LLM-facing text
    schemas.py              # All JSON schemas
    seerr.py                # Seerr API client
    discover.py             # Genre/trend/similar discovery
    enrich.py               # Ratings/trailer enrichment
    morning_digest.py       # LLM-composed daily digest
    utils.py                # Shared utilities (phone masking, safe paths)
    actions/                # iMessage action handlers
    ...                     # iMessage bot modules
  imessage/                 # macOS daemon files
    wrapper.swift           # Swift wrapper → uv run -m bluepopcorn
    BluePopcorn.app/        # macOS app bundle
    config.toml             # Bot settings
    restart.sh              # Daemon restart
  tests/
    test_morning_digest.py  # Digest unit tests (mocked, fast)
    test_mcp_tools.py       # MCP integration tests
    test_conversations.py   # iMessage conversation tests + digest live tests
```

## Seerr Integration

- Auth: `X-Api-Key` header (set on httpx client from `SEERR_API_KEY` env var)
- URL encoding: must use `%20` not `+` for spaces (Seerr 3.x rejects `+`)
- Genres: loaded dynamically from `/api/v1/genres/movie` and `/api/v1/genres/tv`, cached
- Custom exceptions: `SeerrConnectionError`, `SeerrSearchError`, `LLMAuthError`
- Request dedup: checks media status before POSTing to avoid duplicates
- MediaStatus enum: NOT_TRACKED=0, UNKNOWN=1, PENDING=2, PROCESSING=3, PARTIALLY_AVAILABLE=4, AVAILABLE=5, BLOCKLISTED=6, DELETED=7
- RequestStatus enum: PENDING_APPROVAL=1, APPROVED=2, DECLINED=3, FAILED=4, COMPLETED=5

## Conventions

- Package manager: `uv` (never pip), all Python via `uv run`
- Anthropic SDK (`anthropic.AsyncAnthropic`) for LLM calls, tool_use for structured output
- httpx for API requests (Seerr)
- aiosqlite for chat.db (read-only `?mode=ro`)
- AppleScript: `account`/`participant` pattern (Tahoe 26+), not old `service`/`buddy`
- chat.db dates: nanoseconds since 2001-01-01 (Core Foundation epoch)
- Log rotation: 5MB max, 3 backups
