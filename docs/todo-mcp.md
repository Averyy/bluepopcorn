# MCP Server: BluePopcorn as a Seerr MCP Tool Provider

## Goal

Ship BluePopcorn as **two installable modes** from the same repo:

1. **MCP server** (default) — exposes smart Seerr tools via Model Context Protocol, `uv run -m bluepopcorn.mcp`
2. **iMessage bot** (optional) — standalone iMessage chatbot on macOS, `uv run -m bluepopcorn`

Users pick one or both. The MCP server works with OpenClaw, Claude Desktop, Claude Code, Cursor, or any MCP client. No LLM API key needed — the MCP server is purely a tool provider; the LLM lives on the client side.

## Package Structure

One repo, one package, optional extras for iMessage:

```toml
[project]
name = "bluepopcorn"
description = "Smart Seerr MCP server + optional iMessage bot"
dependencies = [
    "fastmcp",
    "httpx>=0.27",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
imessage = [
    "aiosqlite>=0.20",
    "Pillow>=10.0",
]
```

- `pip install bluepopcorn` → MCP server ready (just needs `SEERR_URL` + `SEERR_API_KEY`)
- `pip install bluepopcorn[imessage]` → adds iMessage bot deps

## Existing Seerr MCP Servers

### seerr-cli (electather/seerr-cli)
- **Language:** Go | **Scope:** 52 tools, 9 resources — full Seerr API surface
- **Approach:** Thin API wrapper. Every Seerr endpoint gets a tool. No intelligence — raw params in, raw JSON out
- **Distribution:** Go binary, Docker. Supports stdio + HTTP transport
- **Weakness:** No smart search (year extraction, title matching, fallback chains), no recommendation logic, no genre resolution, no dedup checking. The LLM must figure all of that out from raw API responses

### jellyseerr-mcp (aserper/jellyseerr-mcp)
- **Language:** Python | **Scope:** 4 tools (search, request, get_request, ping)
- **Weakness:** Barely functional. Synchronous HTTP, no error handling, no enrichment

### jellyseerr OpenClaw Skill (ClawHub)
- **Type:** Skill (markdown + scripts), not MCP
- **Weakness:** Toy-level. Wrong status enums, uses `input()` (broken in agent context), no recommendations

## BluePopcorn's Differentiators

| Feature | seerr-cli | Others | BluePopcorn |
|---------|-----------|--------|-------------|
| Smart search (year extraction, title matching, fallback chains) | No | No | Yes |
| Genre resolution (dynamic loading, compound splitting, shorthands) | No | No | Yes |
| Discover/recommend with genre + keyword filters | Raw endpoint only | No | Yes |
| Request dedup (check status before POST) | No | No | Yes |
| Season-aware TV requests (auto-fetch, exclude specials) | No | No | Yes |
| Ratings aggregation (RT, IMDB via Seerr) | No | No | Yes |
| Detail enrichment (trailers, air dates, download progress) | No | No | Yes |
| Search fallback chain (400 errors → shorter queries) | No | No | Yes |
| Detail caching (2-week TTL for TMDB metadata) | No | No | Yes |

**Positioning:** seerr-cli is "raw API access" (52 dumb tools). BluePopcorn MCP is "smart media assistant" (5 intelligent tools). Fewer tools, each does more work.

## Architecture

```
src/bluepopcorn/
  seerr.py              ← shared Seerr client (unchanged)
  types.py              ← shared types (SearchResult, MediaStatus, etc.)
  config.py             ← Settings (needs adjustment, see below)
  mcp/                  ← NEW: MCP server package
    __init__.py
    __main__.py         ← entry point: uv run -m bluepopcorn.mcp
    server.py           ← FastMCP server + tool definitions + serialization helpers
  actions/              ← iMessage bot only (not used by MCP)
  prompts.py            ← iMessage bot only
  sender.py             ← iMessage bot only
  monitor.py            ← iMessage bot only
  ...
```

### Key design decisions

1. **`seerr.py` stays untouched.** The MCP tools call `SeerrClient` methods directly.

2. **MCP tools return structured dicts, not formatted text.** The MCP client's LLM handles presentation.

3. **No LLM credentials needed.** The MCP server only needs `SEERR_URL` and `SEERR_API_KEY`. No `ANTHROPIC_API_KEY`, no Claude config. Zero AI costs on the server side.

4. **Posters are NOT part of the MCP server.** MCP tools return `poster_url` (TMDB URL) and let the client handle display. Poster download/numbering/filtering is iMessage-specific.

5. **No conversation/session state in MCP.** The MCP server is stateless per-call. Context, memory, compression — all handled by the MCP client's agent. The `SeerrClient` instance persists for connection pooling and genre/detail caching.

6. **`Settings` needs decoupling.** Current `Settings` requires iMessage-specific env vars. MCP mode only needs `SEERR_URL`, `SEERR_API_KEY`, `HTTP_TIMEOUT`. Either make iMessage fields optional with defaults, or have the MCP server construct `SeerrClient` directly from env vars without going through `Settings`.

7. **Lift `_enrich_results` into `SeerrClient`.** Currently lives on `ActionExecutor`. Both modes need it (search enrichment with ratings, trailers, etc.). Moving it to `SeerrClient` keeps the MCP layer thin and benefits the standalone bot too.

## MCP Tools (5 tools)

### `seerr_search`
Search for movies and TV shows with smart matching.
- **Params:** `query` (str), `media_type` (optional: "movie" | "tv")
- **Returns:** List of results with title, year, overview, media_type, tmdb_id, status, genres, ratings, poster_url
- **Wraps:** `SeerrClient.search()` + enrichment (ratings, trailers)
- **Intelligence:** Year extraction + post-filter, title match ranking, fallback chain on 400, page 2 fetch

### `seerr_details`
Get full details about a specific movie or TV show by TMDB ID.
- **Params:** `tmdb_id` (int), `media_type` ("movie" | "tv")
- **Returns:** Full detail: title, year, overview, genres, ratings (RT/IMDB), cast, trailer, status, availability, download progress, seasons (for TV), streaming providers
- **Wraps:** `SeerrClient.get_detail()` + `get_ratings()` + `get_detail_extras()` + status/download progress
- **Intelligence:** Aggregates multiple API calls into one rich response. Includes availability status and download ETA

### `seerr_request`
Request a movie or TV show for download.
- **Params:** `tmdb_id` (int), `media_type` ("movie" | "tv"), `seasons` (optional: list[int])
- **Returns:** Request result with status, request_id, or error if already available/requested
- **Wraps:** `SeerrClient.request_media()` with pre-flight status check
- **Intelligence:** Dedup checking (checks media status before POST), auto-fetches seasons for TV, excludes specials

### `seerr_recommend`
Get recommendations based on genre, mood, or theme.
- **Params:** `genre` (optional: str), `keyword` (optional: str), `media_type` (optional: "movie" | "tv")
- **Returns:** List of recommended titles with metadata
- **Wraps:** `SeerrClient.discover()` + `search_keywords()` + genre resolution
- **Intelligence:** Dynamic genre map loading, compound genre splitting, keyword-to-ID resolution, shorthand expansion ("sci-fi" → "science fiction")

### `seerr_recent`
Show recent media requests and their status.
- **Params:** `limit` (optional: int, default 10)
- **Returns:** List of recent requests with title, status, requester, dates, download progress
- **Wraps:** `SeerrClient.get_requests()` + detail enrichment
- **Intelligence:** Download progress parsing, status label mapping

## Installation / Usage

### MCP server (default setup)

```bash
# Install
git clone https://github.com/avery/bluepopcorn && cd bluepopcorn
uv sync  # or: pip install bluepopcorn

# Configure
echo 'SEERR_URL=https://your-seerr.example.com' >> .env
echo 'SEERR_API_KEY=your-api-key' >> .env

# Run
uv run -m bluepopcorn.mcp
```

Claude Desktop (`~/Library/Application Support/Claude/claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "bluepopcorn": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bluepopcorn", "-m", "bluepopcorn.mcp"],
      "env": {
        "SEERR_URL": "https://your-seerr.example.com",
        "SEERR_API_KEY": "your-api-key"
      }
    }
  }
}
```

OpenClaw (`~/.openclaw/openclaw.json`):
```json
{
  "mcpServers": {
    "bluepopcorn": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bluepopcorn", "-m", "bluepopcorn.mcp"],
      "env": {
        "SEERR_URL": "${SEERR_URL}",
        "SEERR_API_KEY": "${SEERR_API_KEY}"
      }
    }
  }
}
```

### iMessage bot (additional, macOS only)

Requires the `imessage` extra and macOS with Full Disk Access + Accessibility permissions.

```bash
# Install with iMessage deps
pip install bluepopcorn[imessage]  # or: uv sync --extra imessage

# Additional config (.env)
ANTHROPIC_API_KEY=your-key      # LLM for the chatbot
PHONE_NUMBERS=+1234567890       # Allowed senders

# Run
uv run -m bluepopcorn           # daemon
uv run -m bluepopcorn --cli     # interactive CLI
```

### Both

Run the iMessage daemon AND register the MCP server. They share `seerr.py` but are independent processes. The MCP server is stateless so there's no conflict.

## Implementation Plan

### Phase 1: Foundation
1. Add `fastmcp` dependency, restructure deps (core vs `[imessage]` extra)
2. Decouple `Settings` — MCP mode only needs Seerr credentials
3. Lift `_enrich_results` from `ActionExecutor` into `SeerrClient`

### Phase 2: MCP server core
4. Create `src/bluepopcorn/mcp/` package with `server.py` (5 tools) and `__main__.py`
5. Write tool docstrings carefully — these are the MCP equivalent of `prompts.py`. The LLM reads them to decide when and how to call each tool. Spend real time on these; they matter more than the code
6. Error handling — every tool wraps its body in try/except, catching `SeerrConnectionError`, `SeerrSearchError`, etc. Return actionable error strings ("Seerr is unreachable — check SEERR_URL and that your server is running"), never raise. An unhandled exception kills the tool call with no useful info for the agent
7. Test with MCP Inspector (`npx @modelcontextprotocol/inspector`)

### Phase 3: Polish + ship
8. README restructure — MCP as primary setup (front and center), iMessage bot as "Additional: macOS iMessage Bot" section. Include keywords for discoverability (seerr, overseerr, jellyseerr, mcp, media requests)
9. Test with Claude Desktop and/or OpenClaw
10. Consider naming/discoverability — "bluepopcorn" is fun but not searchable. Options: rename repo/package (e.g. `seerr-mcp`, `smartseerr`), keep "bluepopcorn" as brand but add subtitle/keywords, or add a PyPI alias. Decide before publishing

### Deferred to v2
- HTTP transport (for remote access — MCP server on Mac Mini, client elsewhere)
- Morning digest as MCP tool or prompt template
- Separate `seerr_discover` tool (raw discover endpoint for power users)
- Docker image with both modes
- PyPI / ClawHub publishing
