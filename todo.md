# iMessagarr

iMessage bot running on a Mac Mini (M1 16GB) that lets you text it to add shows/movies to Seerr. Uses Claude Haiku via `claude -p` CLI (Pro/Max subscription, no API fees).

## Core Flow

```
You: "add breaking bad"
Bot: searches TMDB → finds results
Bot: "Breaking Bad (2008) - A chemistry teacher turned meth manufacturer. This one? (y/n)"
You: "y"
Bot: requests on Seerr
Bot: "Added Breaking Bad to Seerr"
```

## Architecture

```
iMessage (chat.db poll) → Python daemon → claude -p (Haiku) → structured JSON
                                        ↕                       ↓
                        AppleScript send ←                 Python executes action
                                                           (Seerr API, poster fetch, etc.)
```

Single Python async process. The LLM never touches APIs directly. It receives user messages + context, returns structured JSON decisions (`{"action": "search", "query": "severance"}`), and Python executes the actual API calls.

**Important: `--json-schema` does NOT work with `--resume`.** Tested and confirmed -- the `structured_output` field disappears on resumed sessions, and `--append-system-prompt-file` doesn't persist either. So every `claude -p` call is a fresh session with the full conversation history packed into the prompt. This is fine -- conversations are short (3-5 turns) and Haiku's 200K context window is massive.

### How the LLM Loop Works

```bash
# Turn 1: User says "add severance"
claude -p "[conversation history + system context]
User: add severance" \
  --model haiku \
  --tools "" \
  --append-system-prompt-file personality.md \
  --output-format json \
  --json-schema '{"type":"object","properties":{"action":{"type":"string","enum":["search","request","check_status","reply"]},"query":{"type":"string"},"tmdb_id":{"type":"integer"},"media_type":{"type":"string"},"message":{"type":"string"}},"required":["action","message"]}'

# Returns: {"action": "search", "query": "severance", "message": "Let me look that up..."}
# Python calls Seerr API, gets results

# Turn 2: Feed results + full history back (fresh session, no --resume)
claude -p "[conversation history]
User: add severance
Assistant: Let me look that up...
[Search results: Severance (2022) tmdb:95396, status:unknown - Mark leads a team...]
What should we tell the user?" \
  --model haiku \
  --tools "" \
  --append-system-prompt-file personality.md \
  --output-format json \
  --json-schema '...'

# Returns: {"action": "reply", "message": "Found: Severance (2022) - Mark leads a team... Add to Seerr?"}
# Python sends message + poster to user

# Turn 3: User confirms (fresh session again, full history packed in)
claude -p "[conversation history]
User: add severance
Assistant: Found: Severance (2022)... Add to Seerr?
User: yes" \
  --model haiku \
  --tools "" \
  --append-system-prompt-file personality.md \
  --output-format json \
  --json-schema '...'

# Returns: {"action": "request", "tmdb_id": 95396, "media_type": "tv", "message": "Done, Severance requested."}
# Python calls Seerr request API, sends confirmation
```

Each call is ~5-15 seconds (Haiku). A 3-turn conversation takes ~20-30 seconds total. Fine for texting.

Key flags:
- `--tools ""` disables all built-in Claude Code tools (Read, Edit, Bash, etc.). The model just outputs structured JSON.
- `--json-schema` validates the response against the schema before returning. Guaranteed valid JSON.
- `--resume <session_id>` maintains conversation context across turns.
- `--append-system-prompt-file` loads personality without replacing Claude's base prompt.
- `--model haiku` uses Haiku 4.5 (fast, cheap on quota). Use `--model sonnet` if Haiku struggles.

### Components

1. **Message Monitor** - Poll `~/Library/Messages/chat.db` every 0.5s by ROWID cursor. Persist cursor in local SQLite so restarts don't replay history. Filter to incoming messages only (`is_from_me = 0`). Allowlist specific sender phone numbers/emails. Handle `attributedBody` binary plist fallback when `message.text` is NULL (common for certain message types -- extract text from the NSString blob).

2. **Message Sender** - AppleScript via `osascript` to Messages.app. Retry up to 3x with backoff. Chunk long messages (~1200 char limit per bubble). Echo detection so bot ignores its own sent messages appearing in chat.db. Typing indicators while LLM is thinking (GUI-automate a dot into compose window to show "..." bubble -- hacky but makes it feel alive). Supports sending images via `send POSIX file` for posters. Tapback reactions (thumbs up on confirmations) via AppleScript.

3. **LLM Client** - Wraps `claude -p` via subprocess. Haiku 4.5 as primary (`--model haiku`), Sonnet 4.6 as fallback (`--model sonnet`). All built-in tools disabled (`--tools ""`). Responses are structured JSON via `--json-schema`. Every call is a fresh session (no `--resume` -- it breaks `--json-schema`). Conversation history packed into each prompt from SQLite-backed sliding window (last 15-20 messages per sender).

4. **Action Executor** - Receives structured JSON from LLM and executes the corresponding action. The LLM never calls APIs directly.
   - `search` → GET `/api/v1/search?query={query}` on Seerr → download posters → stitch collage if multiple results → return results to LLM in next call
   - `request` → POST `/api/v1/request` on Seerr with `{mediaType, mediaId}` → send confirmation to user
   - `check_status` → GET `/api/v1/request?filter=pending` on Seerr → return to LLM or format directly
   - `reply` → send the message text to the user (no API call needed)

   **Seerr auth flow**: On daemon startup, `POST /api/v1/auth/local` with `{"email": "$SEERR_EMAIL", "password": "..."}` to get a session cookie (30 day TTL). Use that cookie for all subsequent requests. Re-auth automatically if cookie expires. Session cookie approach is safer than API key (which defaults to admin access).

   **Poster images**: TMDB returns poster paths with search results (`https://image.tmdb.org/t/p/w500/{poster_path}`). For disambiguation, download the top 3 posters, stitch them into a single side-by-side image with numbers overlaid (Pillow/PIL), and send as one iMessage. Two bubbles total (one image, one text list) instead of spamming 6+. Cache posters locally in `~/Pictures/imessagarr/` to avoid re-downloading. For single results (happy path), send just the one poster. AppleScript sends images via `send POSIX file` -- **files must be in `~/Pictures`** due to a Messages.app sandbox restriction (confirmed macOS 12-15, files from other directories silently fail).

   **Media status from search**: Seerr search results include a `mediaInfo` object with a `status` field when media is known:
   - `5` = available in library (tell user it's already there)
   - `4` = partially available (some seasons)
   - `3` = processing (approved, downloading)
   - `2` = pending approval
   - absent = not in Seerr (show request option)

   No extra API call needed -- search results already tell us everything.

5. **Conversation State** - SQLite DB tracking per-sender message history. Each `claude -p` call packs the recent history into the prompt (no `--resume`). The LLM handles the conversational flow naturally (disambiguation, confirmation) via the injected history. No need for an explicit state machine.

6. **Bypass Commands** - Simple commands that skip the LLM entirely and hit Seerr API directly. Fast, no token cost, always works even if Claude is down.
   - `status` / `pending` - list pending requests
   - `help` - show available commands

7. **Logging** - Every message in, every message out, every `claude -p` call (with session_id, model, cost), every API call, every error. Local log file. When the bot does something weird, you need to see what happened.

8. **Graceful Degradation** - If Seerr is unreachable, text back a clear error ("can't reach Seerr right now"). If `claude -p` fails or times out, retry once with Sonnet, then fall back to a simple error message. If subscription quota is hit, text back "I'm out of juice, try again in a bit" or fall back to bypass commands only.

## Conversation Examples

### Happy path
```
User: add severance season 3
Bot: [poster image]
Bot: Found: Severance (2022) - Mark leads a team of office workers whose memories have been surgically divided. Add to Seerr?
User: yes
Bot: 👍
Bot: Done, Severance requested on Seerr.
```

### Disambiguation
```
User: add avatar
Bot: [single image: 3 posters stitched side-by-side, numbered 1/2/3]
Bot: 1. Avatar (2009) - James Cameron sci-fi
     2. Avatar: The Last Airbender (2024) - Netflix live action
     3. Avatar: The Way of Water (2022)
     Which one?
User: 1
Bot: 👍 (tapback on "1")
Bot: Added Avatar (2009) to Seerr.
```

### Already exists
```
User: add the bear
Bot: The Bear (2022) is already available in your library.
```

### Casual chat
```
User: what's good to watch
Bot: [LLM responds naturally, action: "reply", no API call needed]
```

## Personality & Memory (stolen from OpenClaw)

**Personality file** (`personality.md`): Loaded via `--append-system-prompt-file`. Defines the bot's tone and behavior. Keep it short (~500 tokens max). Casual, helpful, concise. Not a chatbot, not an assistant -- just a friend who can add shows. Also includes the action schema description so the LLM knows what actions are available and when to use them.

**User memory** (`memory.md`): Simple markdown file tracking user preferences. Appended to the system prompt alongside personality. Examples:
- "Avery prefers 4K quality profiles"
- "Avery watches a lot of sci-fi and dark comedy"
- "Avery already has all of Breaking Bad"

Not auto-generated. The LLM can suggest updates ("want me to remember you like sci-fi?") but the file stays small and curated. OpenClaw's lesson: auto-growing memory becomes a junk drawer fast.

**Allowlist**: Only respond to specific phone numbers/emails. Everyone else gets ignored silently. No pairing codes needed -- it's a personal bot, just hardcode the allowed senders in .env.

## Notifications

Inspired by OpenClaw's heartbeat system. The bot proactively messages you:
- Seerr webhook listener: when a request is approved/available, send an iMessage ("Severance S3 is ready to watch")
- Morning digest (see below)

This means the daemon also runs a small HTTP listener for Seerr webhooks (Seerr supports outgoing webhooks natively). When a webhook fires, format a message and send via AppleScript. No LLM needed for this path.

## Morning Digest

One text every morning. No LLM needed, just formatted data from three sources:

**Weather** — [Open-Meteo](https://open-meteo.com). No API key, no signup. Uses Canadian GEM model at 2.5km resolution.
```
GET https://api.open-meteo.com/v1/forecast?latitude=43.2557&longitude=-79.8711&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,weather_code&daily=temperature_2m_max,temperature_2m_min,apparent_temperature_max,apparent_temperature_min,weather_code&timezone=America/Toronto
```
Key fields: `apparent_temperature` (feels like), `weather_code` (maps to conditions like rain/snow/clear).

**Pollen** — `pollen.mydoglog.ca` (self-hosted, Aerobiology Research Labs data). No auth, Hamilton station.
```
GET https://pollen.mydoglog.ca/api/nearest?lat=43.26&lng=-79.87
```
Key fields: `pollen_level` (0-4 scale), `total_trees`, `total_grasses`, `total_weeds`, `total_spores`, `species` breakdown. Will return `out_of_season: 1` during winter.

**Media** — Seerr pending requests, recently available downloads.

**Example morning text:**
```
Good morning. 3°C feels like -1°C, cloudy with rain later.
Pollen: High (trees). Maple and Cedar elevated.
Severance S3E04 downloaded overnight. 2 requests pending.
```

## CLI Test Mode

Run the bot in terminal without iMessage. Type messages, see responses, test the full LLM → action → response loop. Bypasses chat.db polling and AppleScript sending entirely. Same LLM client, same action executor, just stdin/stdout instead of iMessage. Essential for debugging tool calls and prompt tuning without texting yourself repeatedly.

## Config

**`config.toml`** — Non-secret settings, checked into repo:
- Claude model (`haiku` or `sonnet`)
- Morning digest time
- Hamilton coordinates (for weather + pollen)
- Poster cache directory
- Log level
- Debounce delay

**`.env`** — Secrets, gitignored. Contains Seerr credentials, bot Apple ID, allowed sender phone numbers. See `.env` file in repo.

## Tech Stack

- **Python 3.11+** with asyncio
- **Claude Code CLI** (`claude -p`) — Haiku 4.5 primary, Sonnet 4.6 fallback. Uses Pro/Max subscription.
- **SQLite** for conversation memory + ROWID cursor + session tracking
- **httpx** for Seerr, Open-Meteo, and pollen API calls (async)
- **aiosqlite** for chat.db reads
- **Pillow** for poster collage stitching
- **subprocess** for AppleScript sends and `claude -p` calls

## Mac Mini Setup

- **SSH access**: `ssh mini`
- **Bot Apple ID**: `$SEERR_EMAIL` (not yet created), signed into iMessage on the Mac Mini
- **Seerr**: `$SEERR_URL`, bot user `$SEERR_USERNAME`
- Full Disk Access granted to Terminal / the Python process (for chat.db reads)
- Accessibility permissions for AppleScript automation
- Claude Code installed and authenticated: `claude auth login`
- Python daemon managed via launchd
- Active GUI session required (for AppleScript to control Messages.app)
- Development: `ssh mini` → `tmux` → `claude` (tmux keeps sessions alive if SSH drops)

## Key Decisions

**Why Seerr API instead of direct Sonarr/Radarr?** Seerr (formerly Overseerr) `/api/v1/search` is unified (movies + TV in one call) and `/api/v1/request` handles routing to the right *arr service internally. No need to give the bot Sonarr/Radarr API keys at all. Auth via `X-Api-Key` header or session cookie from `POST /api/v1/auth/local`. Bot uses a dedicated non-admin local user (`$SEERR_USERNAME`) so requests are tracked separately and it can't change settings.

**Why structured JSON output instead of tool calling?** `claude -p --tools "" --json-schema` gives us validated structured output without the model having direct API access. The LLM decides what to do, Python does it. Clean separation, no risk of the model doing something unexpected with real tools.

**Why not OpenClaw?** Too heavy for this scope. OpenClaw's tool-calling loop was designed for large models and floods the context with tool schemas. Also costs $100-300/day with frontier models if you leave it running.

**Why not Home Assistant + Hassarr?** Extra infrastructure for no benefit. Direct Seerr API calls are simpler than routing through HA's conversation system. Hassarr also can't disambiguate (always picks first result).

**Why Haiku over Sonnet?** For 3-4 actions and short conversations, Haiku is plenty. 2x faster, uses less subscription quota. 87% first-try success on sequential tool calls, ~95% with one retry. Sonnet is available as fallback if needed.

**Why `claude -p` instead of Anthropic API?** The CLI uses your existing Pro/Max subscription -- no separate API billing. For a personal bot handling a few dozen messages a day, the subscription quota is more than enough.

**Why conversation memory instead of stateless?** The confirmation flow requires at least 2-3 turns of context. The LLM needs the conversation history packed into each prompt to know what it just searched for when the user says "yes". Sliding window of ~20 messages keeps token costs low.

## Open Questions

- Debounce: should the bot ignore messages if `claude -p` is still generating a response for that sender? (0.3s sleep + check if newer message arrived, from iMessages-Chatbot-Server)
- Should the bot respond to everything sent to its number, or require a prefix/keyword?
- Pro subscription quota: is Pro ($20/mo) enough for a casual personal bot (~20-50 messages/day), or is Max 5x ($100/mo) needed? Haiku uses less quota than Sonnet/Opus so Pro might be fine.
- macOS 16 Tahoe: AppleScript for Messages is reportedly further broken. Not an issue today but may need to switch to BlueBubbles Private API or Shortcuts in the future.

## Inspiration Projects

- [iMessages-Chatbot-Server](https://github.com/alextyhwang/iMessages-Chatbot-Server) - cleanest iMessage architecture (~800 lines), good debounce pattern
- [Apple Flow](https://github.com/dkyazzentwatwa/apple-flow) - echo detection, approval gates, multi-channel daemon pattern
- [BlueBubble-iMessage-Bot](https://github.com/IftatBhuiyan/BlueBubble-iMessage-Bot) - attributedBody blob parsing, contact name resolution
- [Hassarr](https://github.com/TegridyTate/Hassarr) - Seerr/Sonarr/Radarr API endpoints and payload structures

## Scope

Search, confirm, request. Movies and TV. Claude Haiku via `claude -p` with Sonnet fallback. Personality file. Allowlist. Conversation memory. User preferences memory. Proactive notifications via Seerr webhooks. Morning digest (weather, pollen, media). Poster collages for disambiguation. CLI test mode. 1:1 chats only (no group chats).

**Later**:
- Calendar integration — Read-only access to Calendar.app via AppleScript (`get events of calendar whose start date > current date`). Add to morning digest ("You have 3 meetings today, first at 10am") and as an LLM tool ("what's on my calendar today?"). No create/delete event tools, read-only only. Same AppleScript pattern as iMessage sending, just needs Automation permission.
- Home Assistant integration (lights, thermostat) — needs scoped access solution, HA has no real RBAC yet
