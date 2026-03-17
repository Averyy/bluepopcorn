# iMessagarr

iMessage bot for Seerr media requests on Mac Mini. Claude Haiku via `claude -p`.

## Critical Rules

- **NEVER give the LLM direct API access** -- structured JSON decisions only, Python executes
- **ALWAYS use `--tools ""`** with `claude -p` -- disables all built-in tools
- **NEVER use `--resume`** -- breaks `--json-schema`. Every call is fresh with history packed in
- **`--append-system-prompt-file` does NOT exist** -- only `--append-system-prompt` (inline string)
- **Poster images must be in `~/Pictures/imessagarr/`** -- Messages.app sandbox, other dirs silently fail
- **Secrets in `.env` only** -- never hardcode credentials or phone numbers
- **NEVER disable the typing indicator** -- essential UX. Fix bugs instead

## Commands

```bash
uv sync                              # Install deps
uv run -m imessagarr --cli           # CLI test mode
uv run -m imessagarr --digest        # One-shot digest
uv run -m imessagarr                 # Run daemon
swiftc -o iMessagarr wrapper.swift   # Build macOS wrapper
```

## Key References

- @personality.md -- Bot tone (loaded into LLM system prompt)
- @instructions.md -- Action routing rules (loaded into LLM system prompt)
- docs/ref-seerr-api.md -- Seerr API reference (enums, endpoints, params)
- config.toml -- Non-secret settings

## Architecture: One LLM Call Per Message

Haiku decides the action (structured JSON), Python executes and formats everything. No second LLM call anywhere.

```
User text → Haiku → {"action": "search", "query": "severance"} → Python executes → Python formats → send
```

## File Layout

Each external service is its own module:

- `seerr.py` -- Seerr API client (search, request, discover, ratings, genres)
- `weather.py` -- Weather (Open-Meteo) + pollen (Aerobiology) as standalone functions
- `morning_digest.py` -- Composes weather + seerr for daily digest message
- `actions.py` -- Action dispatch + all response formatting
- Adding a new service = new file + new handler in actions.py

## Seerr Integration

- Auth: session cookie via `POST /api/v1/auth/local`, NOT API key
- URL encoding: must use `%20` not `+` for spaces (Seerr 3.x rejects `+`)
- Genres: loaded dynamically from `/api/v1/genres/movie` and `/api/v1/genres/tv`, cached
- Custom exceptions: `SeerrConnectionError`, `SeerrAuthError`, `SeerrSearchError`
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
