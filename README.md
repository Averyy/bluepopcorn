# BluePopcorn — Seerr Chatbot & MCP Server

**[bluepopcorn.dev](https://bluepopcorn.dev)**

Seerr MCP server + iMessage chatbot. Search, discover, and request movies and TV shows from Claude, ChatGPT, Gemini, or iMessage.

Unlike raw API wrappers, BluePopcorn handles fuzzy search with fallback chains, automatic year extraction, genre resolution for compound queries ("sci-fi comedy"), duplicate detection, and TV season auto-fetching.

## Quick Start

```bash
git clone https://github.com/Averyy/bluepopcorn.git
cd bluepopcorn
cp .env.example .env   # fill in SEERR_URL, SEERR_API_KEY, MCP_API_KEY
uv sync
uv run -m bluepopcorn.mcp           # HTTP server (default :8080)
uv run -m bluepopcorn.mcp --stdio   # stdio for local clients
```

## Client Configuration

### Claude Code (stdio)

```bash
claude mcp add bluepopcorn -- uv run --directory /path/to/bluepopcorn -m bluepopcorn.mcp --stdio
```

Or in JSON config:
```json
{
  "mcpServers": {
    "bluepopcorn": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bluepopcorn", "-m", "bluepopcorn.mcp", "--stdio"]
    }
  }
}
```

### Claude Desktop / Windsurf (stdio)

Same JSON config as above in the client's MCP settings file.

### Claude Desktop (HTTP)

```json
{
  "mcpServers": {
    "bluepopcorn": {
      "type": "streamable-http",
      "url": "http://YOUR_HOST:8080/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_API_KEY"
      }
    }
  }
}
```

### Cursor / VS Code

```json
{
  "mcp": {
    "servers": {
      "bluepopcorn": {
        "type": "streamable-http",
        "url": "http://YOUR_HOST:8080/mcp",
        "headers": {
          "Authorization": "Bearer YOUR_MCP_API_KEY"
        }
      }
    }
  }
}
```

## Tools

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `seerr_search` | Search movies and TV shows by title | `query`, `media_type?` |
| `seerr_details` | Full details by TMDB ID (ratings, trailers, seasons) | `tmdb_id`, `media_type` |
| `seerr_request` | Request a title for download (dedup built-in) | `tmdb_id`, `media_type`, `seasons?` |
| `seerr_recommend` | Discover by genre, keyword, similarity, or trending | `genre?`, `keyword?`, `similar_to?`, `trending?`, `upcoming?` |
| `seerr_recent` | Recently added and pending requests | `page?`, `limit?` |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `SEERR_URL` | Yes | Seerr instance URL (e.g. `http://seerr:5055`) |
| `SEERR_API_KEY` | Yes | Seerr API key |
| `MCP_API_KEY` | HTTP mode | Bearer token for HTTP auth (comma-separated for multiple keys) |
| `HTTP_PORT` | No | HTTP listen port (default `8080`) |
| `HTTP_HOST` | No | HTTP listen host (default `127.0.0.1`) |
| `HTTP_TIMEOUT` | No | Seerr API timeout in seconds (default `15`) |
| `ANTHROPIC_API_KEY` | iMessage | Claude API key for the iMessage bot |
| `ALLOWED_SENDERS` | iMessage | E.164 phone numbers allowed to use the bot (comma-separated) |

## iMessage Bot (optional, macOS only)

BluePopcorn also includes an iMessage bot that runs as a macOS daemon. This is separate from the MCP server.

```bash
uv sync --extra imessage
# edit imessage/config.toml with your settings
# add to .env: ANTHROPIC_API_KEY, ALLOWED_SENDERS (E.164 phone numbers)
uv run -m bluepopcorn --cli                    # CLI test mode
uv run -m bluepopcorn                          # daemon mode
```

### Daemon Setup

```bash
cd imessage
swiftc -o BluePopcorn.app/Contents/MacOS/BluePopcorn wrapper.swift
codesign --force --sign - BluePopcorn.app
cp com.bluepopcorn.daemon.plist.example ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist
# Edit the plist to set your paths, then:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist
```

Requires Full Disk Access + Accessibility permissions for `BluePopcorn.app` in System Settings.

## Network Access

BluePopcorn needs local network access to reach your Seerr instance. When your MCP client prompts for local network permission, **you must allow it** — otherwise BluePopcorn can't connect to Seerr and all tools will fail.

## Auth

**stdio mode:** No auth needed — the MCP client launches the process directly.

**HTTP mode:** Bearer token auth via `MCP_API_KEY` environment variable. All major MCP clients (Claude Code, Claude Desktop, Cursor, VS Code) support setting auth headers in their config. Multiple keys supported (comma-separated) for key rotation.

Note: OAuth 2.1 (needed for claude.ai web connectors) is not yet implemented. Use stdio or HTTP with Bearer token for now.
