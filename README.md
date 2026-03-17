# iMessagarr

iMessage bot for Seerr media requests, weather, and more. Runs on a Mac Mini, uses Claude Haiku for natural language understanding.

## What It Does

Text the bot to:
- **Add movies/shows** — "add severance" → searches TMDB → shows poster → confirms → requests on Seerr
- **Ask about titles** — "what's The Abandons about?" → searches and describes with TMDB rating + trailer link
- **Get recommendations** — "good sci-fi from 2026?" → searches TMDB and presents options
- **Check weather** — "what's the weather like?" → St. Catharines weather + Hamilton pollen (Aerobiology)
- **See what's new** — "what's been added?" → recently added movies/shows from Seerr
- **Check requests** — "status" → pending Seerr requests
- **Remember things** — "remember I like sci-fi" → stores per-user preferences in SQLite

### Proactive Notifications
- **Morning digest** — weather + pollen + media status at a configurable time
- **Seerr webhooks** — alerts when media is approved, available, or fails
- **Quiet hours** — no proactive messages between 22:00-07:00

### Commands

| Text | What happens |
|------|-------------|
| "add severance" | Search + poster + request flow |
| "what's X about" | Search + describe with rating/trailer |
| "recommend a thriller" | TMDB search for the genre |
| "what's the weather" | Weather + pollen |
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
                                                           (Seerr API, weather, posters)
```

Single async Python daemon. One LLM call per message — Haiku returns a structured JSON decision (`{"action": "search", "query": "severance"}`), Python executes the API calls and formats the response. No second LLM call for formatting.

### How It Works
- **Conversation history**: 20-entry sliding window per user, auto-clears after 1h gap
- **Per-user memory**: SQLite `user_facts` table, injected into every LLM prompt
- **Time context**: current date/time included in every call
- **Poster intelligence**: collage for disambiguation ("add avatar"), single poster for info queries
- **Weather keywords**: "weather", "pollen", "forecast" always trigger weather — no LLM guessing

## Stack

- Python 3.12, asyncio
- Claude Haiku via `claude -p` CLI (Pro/Max subscription, no API fees)
- httpx (Seerr, Open-Meteo, pollen APIs)
- aiosqlite (chat.db reads, bot state)
- Pillow (poster collages)
- AppleScript (iMessage sending + typing indicator)

## Setup

### Prerequisites
- Mac with iMessage signed in (bot Apple ID)
- Full Disk Access + Accessibility permissions for the `iMessagarr` binary
- Seerr instance with a local bot user
- Claude Code CLI installed and authenticated

### Install
```bash
git clone <repo>
cd imessagarr
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
uv run -m imessagarr --cli      # CLI test mode (no iMessage)
uv run -m imessagarr --digest   # One-shot morning digest
uv run -m imessagarr            # Daemon (production)
```

### Auto-start (launchd)
```bash
swiftc -o iMessagarr.app/Contents/MacOS/iMessagarr wrapper.swift
codesign --force --sign - iMessagarr.app

cp com.imessagarr.daemon.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.imessagarr.daemon.plist
```

**Permissions** — both must be added manually (no automatic prompts):
1. System Settings → Privacy & Security → **Full Disk Access** → `+` → select `iMessagarr.app` (required for reading chat.db)
2. System Settings → Privacy & Security → **Accessibility** → `+` → select `iMessagarr.app` (required for typing indicators)

After recompiling, remove and re-add `iMessagarr.app` in both lists (the code signature changes).

## Project Structure

```
imessagarr/
  .env                    # Secrets (gitignored)
  config.toml             # Non-secret settings
  personality.md          # Bot tone (→ LLM system prompt)
  instructions.md         # Action routing (→ LLM system prompt)
  memory.md               # Global bot context (→ LLM system prompt)
  wrapper.swift           # Swift wrapper (compiled into iMessagarr.app bundle)
  iMessagarr.app/         # macOS app bundle (binary gitignored, Info.plist tracked)
  src/imessagarr/
    __main__.py           # Entry point, daemon loop
    config.py             # Settings from .env + config.toml
    types.py              # Dataclasses, enums, JSON schema
    llm.py                # claude -p subprocess wrapper
    actions.py            # Action dispatch + response formatting
    seerr.py              # Seerr API client (search, request, discover, ratings)
    weather.py            # Weather (Open-Meteo) + pollen (Aerobiology)
    morning_digest.py     # Daily digest (composes weather + seerr)
    monitor.py            # chat.db poller
    sender.py             # AppleScript iMessage + typing indicator
    db.py                 # Bot state SQLite (cursor, history, facts)
    posters.py            # TMDB poster download + Pillow collages
    webhooks.py           # Seerr webhook listener
    cli.py                # CLI test mode
```
