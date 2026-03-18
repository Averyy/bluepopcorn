# BluePopcorn

iMessage bot for Seerr media requests on Mac Mini. Claude Haiku via `claude -p`.

## Critical Rules

- **NEVER give the LLM direct API access** -- structured JSON decisions only, Python executes
- **ALWAYS use `--tools ""`** with `claude -p` -- disables all built-in tools
- **NEVER use `--resume`** -- breaks `--json-schema`. Every call is fresh with history packed in
- **`--append-system-prompt-file` does NOT exist** -- only `--append-system-prompt` (inline string)
- **Poster images must be in `~/Pictures/bluepopcorn/`** -- Messages.app sandbox, other dirs silently fail
- **Secrets in `.env` only** -- never hardcode credentials or phone numbers
- **NEVER disable the typing indicator** -- essential UX. Fix bugs instead
- **NEVER rebuild wrapper.swift unless its source changes** -- rebuilding revokes FDA/Accessibility permissions. Python code changes only need a daemon restart
- **After code changes, run `./restart.sh`** -- always restart the daemon after modifying Python code

## Commands

```bash
uv sync                              # Install deps
uv run -m bluepopcorn --cli           # CLI test mode
uv run -m bluepopcorn --digest        # One-shot digest
uv run -m bluepopcorn                 # Run daemon

# Restart daemon (for Python code changes — NO rebuild needed)
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist   # Stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist # Start

# Rebuild wrapper (ONLY if wrapper.swift or Info.plist change — rare)
# WARNING: rebuilding revokes FDA/Accessibility, must re-grant in System Settings
swiftc -o BluePopcorn.app/Contents/MacOS/BluePopcorn wrapper.swift  # Build macOS wrapper
codesign --force --sign - BluePopcorn.app                          # Ad-hoc sign
```

## Key References

- @personality.md -- Bot tone (loaded into LLM system prompt)
- @instructions.md -- Action routing rules (loaded into LLM system prompt)
- docs/ref-seerr-api.md -- Seerr API reference (enums, endpoints, params)
- config.toml -- Non-secret settings

## Architecture: Two-Call Pattern

Haiku decides the action (call 1), Python executes the API call, then Haiku crafts the response using conversation history + API results as context (call 2). Python only formats responses as a fallback if the second LLM call fails.

```
User text → Haiku (action) → Python executes API → store results as context → Haiku (response) → send
```

Only exceptions: bypass commands (status/help/new) and remember/forget use Python responses directly.

## File Layout

Each external service is its own module:

- `seerr.py` -- Seerr API client (search, request, discover, ratings, genres)
- `weather.py` -- Weather (Open-Meteo) + pollen (Aerobiology) as standalone functions
- `morning_digest.py` -- Composes weather + seerr for daily digest message
- `actions/` -- Action dispatch + handler package (search, request, status, weather, recent, recommend, memory)
- Adding a new service = new file + new handler in actions.py

## Seerr Integration

- Auth: `X-Api-Key` header (set on httpx client from `SEERR_API_KEY` env var)
- URL encoding: must use `%20` not `+` for spaces (Seerr 3.x rejects `+`)
- Genres: loaded dynamically from `/api/v1/genres/movie` and `/api/v1/genres/tv`, cached
- Custom exceptions: `SeerrConnectionError`, `SeerrSearchError`
- Request dedup: checks media status before POSTing to avoid duplicates
- MediaStatus enum: NOT_TRACKED=0, UNKNOWN=1, PENDING=2, PROCESSING=3, PARTIALLY_AVAILABLE=4, AVAILABLE=5, BLOCKLISTED=6, DELETED=7
- RequestStatus enum: PENDING_APPROVAL=1, APPROVED=2, DECLINED=3, FAILED=4, COMPLETED=5

## Conventions

- Package manager: `uv` (never pip), all Python via `uv run`
- httpx for API requests (Seerr, Open-Meteo, pollen)
- aiosqlite for chat.db (read-only `?mode=ro`)
- Pollen API: always `?provider=aerobiology` (Hamilton station)
- Weather: Open-Meteo, St. Catharines coordinates
- AppleScript: `account`/`participant` pattern (Tahoe 26+), not old `service`/`buddy`
- chat.db dates: nanoseconds since 2001-01-01 (Core Foundation epoch)
- Log rotation: 5MB max, 3 backups
