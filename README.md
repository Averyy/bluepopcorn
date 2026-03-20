# BluePopcorn

iMessage bot for Seerr media requests. Runs on a Mac Mini, uses Claude Haiku for natural language understanding.

## What It Does

Text the bot to:
- **Add movies/shows** — "add severance" → searches TMDB → shows poster → confirms → requests on Seerr
- **Ask about titles** — "what's The Abandons about?" → searches and describes with TMDB rating + trailer link
- **Get recommendations** — "good sci-fi from 2026?" → searches TMDB and presents options
- **See what's new** — "what's been added?" → recently added movies/shows from Seerr
- **Check requests** — "status" → pending Seerr requests
- **Remember things** — "remember I like sci-fi" → stores per-user preferences in markdown memory files

### Proactive Notifications
- **Morning digest** — media status at a configurable time
- **Seerr webhooks** — alerts when media is approved, available, or fails
- **Quiet hours** — no proactive messages between 22:00-07:00

### Commands

| Text | What happens |
|------|-------------|
| "add severance" | Search + poster + request flow |
| "what's X about" | Search + describe with rating/trailer |
| "recommend a thriller" | TMDB search for the genre |
| "what's new" | Recently added media from Seerr |
| "status" / "pending" | Pending Seerr requests (no LLM) |
| "new" / "reset" / "clear" | Clear conversation history |
| "remember I like sci-fi" | Store user preference |
| "forget sci-fi" | Remove stored preference |
| "help" | Show all capabilities |

## Architecture

```
iMessage (chat.db poll) → Python daemon → claude -p (Haiku) → structured JSON
                                        ↕                       ↓
                        AppleScript send ←                 Python executes + formats
                                                           (Seerr API, posters)
```

Single async Python daemon. Two LLM calls per message — Haiku returns a structured JSON decision (call 1: `{"action": "search", "query": "severance"}`), Python executes the API call, then Haiku crafts the natural-language response using the results as context (call 2). Python formats directly only as a fallback if call 2 fails.

### How It Works
- **Conversation history**: chat.db bidirectional reads + in-memory context buffer, session boundaries via "new"/"reset"
- **Per-user memory**: Markdown files with tiered compression (daily → weekly → monthly), injected into every LLM prompt
- **Time context**: current date/time included in every call
- **Poster intelligence**: collage for disambiguation ("add avatar"), single poster for info queries
## Stack

- Python 3.12, asyncio
- Claude Haiku via `claude -p` CLI (Pro/Max subscription, no API fees)
- httpx (Seerr API)
- aiosqlite (chat.db reads)
- Pillow (poster collages)
- AppleScript (iMessage sending + typing indicator)

## Setup

### Prerequisites
- Mac with iMessage signed in (bot Apple ID)
- Full Disk Access + Accessibility permissions for the `BluePopcorn` binary
- Seerr instance with a local bot user
- Claude Code CLI installed and authenticated

### Install
```bash
git clone <repo>
cd bluepopcorn
cp .env.example .env  # Fill in credentials
uv sync
```

### Configure
- `.env` — Seerr credentials, bot Apple ID, allowed phone numbers
- `config.toml` — Model, poll interval, location, quiet hours
- `personality.md` — Bot tone/personality
- `instructions.md` — Action routing rules for the LLM

### Run
```bash
uv run -m bluepopcorn --cli      # CLI test mode (no iMessage)
uv run -m bluepopcorn --digest   # One-shot morning digest
uv run -m bluepopcorn            # Daemon (production)
```

### Auto-start (launchd)
```bash
swiftc -o BluePopcorn.app/Contents/MacOS/BluePopcorn wrapper.swift
codesign --force --sign - BluePopcorn.app

cp com.bluepopcorn.daemon.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist
```

**Permissions** — both must be added manually (no automatic prompts):
1. System Settings → Privacy & Security → **Full Disk Access** → `+` → select `BluePopcorn.app` (required for reading chat.db)
2. System Settings → Privacy & Security → **Accessibility** → `+` → select `BluePopcorn.app` (required for typing indicators)

After recompiling, remove and re-add `BluePopcorn.app` in both lists (the code signature changes).

## Project Structure

```
bluepopcorn/
  .env                    # Secrets (gitignored)
  config.toml             # Non-secret settings
  personality.md          # Bot tone (→ LLM system prompt)
  instructions.md         # Action routing (→ LLM system prompt)
  wrapper.swift           # Swift wrapper (compiled into BluePopcorn.app bundle)
  BluePopcorn.app/        # macOS app bundle (binary gitignored, Info.plist tracked)
  data/memory/            # Per-user markdown memory files (gitignored)
  src/bluepopcorn/
    __main__.py           # Entry point, daemon loop
    config.py             # Settings from .env + config.toml
    types.py              # Dataclasses, enums, JSON schema
    llm.py                # claude -p subprocess wrapper
    memory.py             # Per-user markdown memory manager
    compression.py        # Tiered memory compression (daily/weekly/monthly)
    actions/              # Action dispatch + handler package (search, request, status, etc.)
    seerr.py              # Seerr API client (search, request, discover, ratings)
    morning_digest.py     # Daily digest (media status from Seerr)
    monitor.py            # chat.db poller + bidirectional message queries
    sender.py             # AppleScript iMessage + typing indicator
    posters.py            # TMDB poster download + Pillow collages
    webhooks.py           # Seerr webhook listener
    cli.py                # CLI test mode
```
