# Markdown Memory: Replace SQL History/Facts with Tiered Markdown

## Context

Currently BluePopcorn duplicates every message into a SQLite `history` table and stores user preferences in a `user_facts` table. This is redundant — raw messages already exist in iMessage's `chat.db`, and flat SQL rows of preferences are just a markdown file with extra steps. Worse, conversation history older than 1 hour is silently deleted with zero compression, so the bot has no long-term memory of past interactions.

**Goal:** Replace SQL-based history and facts with per-user markdown files that support tiered compression — recent messages stay verbatim (from chat.db), older conversations get progressively summarized into the markdown file. The bot gains persistent, useful memory across days/weeks/months while keeping prompts small. Drop SQLite entirely.

## Architecture

```
Active conversation (today):
  Raw messages ← read directly from chat.db (both directions via chat join)
  Context entries (search results, weather) ← in-memory Python dict
  Gap markers inserted when 2+ hours between messages

End of day (compression trigger, runs after morning digest):
  Yesterday's chat.db messages → LLM summarizes → appended to per-user .md
  LLM also extracts suggested preferences from patterns → auto-appended to # Preferences

Per-user markdown file (data/memory/{phone}.md):
  # Profile            ← name and contact-like fields
  # Preferences        ← persistent, managed by remember/forget + auto-extracted
  # Recent             ← daily summaries (rolling 7 days)
  # Weekly             ← weekly summaries (rolling 4 weeks)
  # History            ← monthly summaries (permanent)

ROWID cursor:
  In-memory int + data/last_rowid file (write-through cache)

System prompt files (loaded into every claude -p call):
  personality.md       ← tone and voice only
  instructions.md      ← action schema, routing rules, and global defaults
```

## Per-User Markdown Format

File: `data/memory/{phone}.md` (e.g. `data/memory/+1XXXXXXXXXX.md`)

```markdown
# Profile
Name: Avery

# Preferences
- Prefers 4K quality profiles
- Watches a lot of sci-fi and dark comedy
- Already has all of Breaking Bad
- Interested in Korean dramas (auto-extracted 2026-03-10)

# Recent
- 2026-03-15: Requested Severance S3, added successfully. Asked about weather — high pollen.
- 2026-03-14: Asked about Avatar, picked the 2009 film, already available.
- 2026-03-13: Checked status on 3 pending requests.

# Weekly
- Week of Mar 3: 8 requests total, mostly TV dramas. Discovered Korean shows. Weather checks daily.

# History
- Feb 2026: Heavy usage (~40 requests). Shifted from sci-fi to thriller. Started checking pollen daily.
- Jan 2026: First month. Mostly testing. Set up preferences.
```

The `# Profile` section stores the sender's display name. Set via "remember my name is Avery" — the `remember` handler detects name-like facts and writes to `# Profile` instead of `# Preferences`. Replaces the contacts integration idea — no macOS Contacts permission needed.

## Prompt Structure (after changes)

```
<context>[Current time: Monday March 16, 2026 2:30 PM EDT]</context>

<memory>
# Profile
Name: Avery

# Preferences
- Prefers 4K quality profiles
- Watches a lot of sci-fi and dark comedy

# Recent
- Mar 15: Requested Severance S3, added successfully.
- Mar 14: Asked about Avatar, picked the 2009 film.

# Weekly
- Week of Mar 3: 8 requests, mostly TV dramas.
</memory>

<user>what's the weather</user>
<assistant>4°C, feels like 1°C. Cloudy with rain later. Pollen is moderate.</assistant>
<gap>3 hours later</gap>
<user>add severance</user>
<assistant>Let me look that up...</assistant>
<context>[Search results: Severance (2022) tmdb:95396...]</context>
<user>yes</user>
```

## Token Budget

- System prompt (personality + instructions): ~1000 tokens
- Per-user memory file: ~500 tokens (grows slowly with compression)
- Chat.db recent messages (20 messages): ~2500 tokens
- Context entries (search results, weather): ~1000 tokens
- **Total: ~5000 tokens per call**

Haiku's 200K context makes this a non-issue. `truncate_if_needed()` trims oldest `# History` entries first to stay under ~200 lines.

## Rename System Prompt Files

Current state is confused — `memory.md` contains rules, `personality.md` mixes tone with rules.

**Changes:**
- `memory.md` → **delete**. Its one rule ("only recommend post-2000 titles") moves into `instructions.md` under a `## Defaults` section.
- `personality.md` → keep, but only tone/voice (already cleaned up).
- `instructions.md` → keep, absorbs global defaults from `memory.md`.
- `llm.py` → update `_load_system_prompt()` to only load `personality.md` + `instructions.md` (drop `memory.md` from the list).

Per-user memory is now injected dynamically via `_build_prompt()`, not as a static system prompt file.

---

## Phase 0: Validate chat.db JOIN Query

**Do this before writing any code.** This is the riskiest assumption — if the query doesn't work, the entire design needs rethinking.

Validate that we can reliably query both incoming AND outgoing messages from chat.db using `chat_message_join`. The current `monitor.py` only reads incoming messages via the `handle` table — but outgoing messages don't always have `handle_id` set, so we need a different join path.

**The query:**
```sql
SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date
FROM message m
JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
JOIN chat c ON cmj.chat_id = c.ROWID
WHERE c.chat_identifier = ?
  AND m.item_type = 0
  AND m.date > ?
ORDER BY m.date ASC
LIMIT ?
```

**What to verify:**
1. **Phone format match**: Does `chat.chat_identifier` store the phone as `+1XXXXXXXXXX` or some other format? Must match what we pass.
2. **Both directions**: Returns rows with both `is_from_me=0` (incoming) and `is_from_me=1` (outgoing).
3. **Text extraction**: Verify `parse_attributed_body()` works on outgoing messages too.
4. **Noise**: Confirm `item_type=0` filters out tapbacks, edit markers, typing indicators. Check image-only messages.
5. **Chunked bot messages**: How do multi-bubble sends appear? Sequential ROWIDs, same/close timestamps? This informs the dedup logic.

**Also while here:** Check if there are any existing facts in bot.db worth migrating:
```sql
sqlite3 ~/.local/share/bluepopcorn/bot.db "SELECT * FROM user_facts"
```
If there are facts, seed the markdown file manually. If empty, move on.

**How to test:**
```sql
-- Check chat_identifier format
SELECT chat_identifier FROM chat WHERE chat_identifier LIKE '%XXXXXXXXXX%';

-- Test the full query (last 20 messages)
SELECT m.ROWID, m.is_from_me, m.text, m.date, m.item_type
FROM message m
JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
JOIN chat c ON cmj.chat_id = c.ROWID
WHERE c.chat_identifier = '+1XXXXXXXXXX'
  AND m.item_type = 0
ORDER BY m.date DESC
LIMIT 20;
```

**If it fails:**
- **Alt 1**: Query `handle` table for incoming + separate query for outgoing
- **Alt 2**: Keep writing outgoing messages to our own store

---

## Phase 1: Foundation (additive — nothing breaks)

Add new modules and config without touching existing code paths. The bot continues to use `db.py` after this phase.

### 1. New: `src/bluepopcorn/memory.py` — Per-user markdown memory manager

All methods are **synchronous** (plain file I/O). Markdown files are tiny (<200 lines). No async needed.

**All writes use atomic write-to-temp-then-rename** — `path.with_suffix(".tmp")`, write, then `tmp.rename(path)`. Rename is atomic on POSIX, prevents corrupted files on crash.

**Directory bootstrapping:** `load_or_create()` calls `Path(memory_dir).mkdir(parents=True, exist_ok=True)` before writing.

Section parsing: split by lines, find `# SectionName` headers, extract lines between them. Lines prefixed with `- ` are entries.

**Methods:**
- `load(sender)` → read the full markdown file, return as string (for prompt injection). Empty string if no file
- `get_profile(sender)` → parse `# Profile` as a dict (e.g. `{"name": "Avery"}`)
- `set_profile_field(sender, key, value)` → set a field in `# Profile`
- `get_preferences(sender)` → parse and return `# Preferences` lines
- `add_preference(sender, fact)` → append under `# Preferences` (fuzzy duplicate check — case-insensitive substring). If fact looks like a name ("my name is X", "I'm X", "call me X"), route to `set_profile_field(sender, "name", X)` instead
- `remove_preference(sender, keyword)` → remove first matching line from `# Preferences`. Returns bool
- `append_summary(sender, date, summary, tier)` → append to `# Recent`, `# Weekly`, or `# History`
- `get_section(sender, section)` → return lines from a specific section
- `replace_section(sender, section, lines)` → replace all lines in a section (used by compression)
- `load_or_create(sender)` → create file with empty sections if it doesn't exist
- `truncate_if_needed(sender, max_lines=200)` → trim oldest `# History` entries first
- Phone number used directly as filename (e.g. `+1XXXXXXXXXX.md`)

### 2. Modify: `src/bluepopcorn/monitor.py` — Add bidirectional message queries

Add `get_recent_messages(sender, limit, since_hours)` → query chat.db for both directions (`is_from_me` 0 and 1) for a specific sender within a time window. Returns `list[HistoryEntry]` with role="user" or role="assistant".

Uses the `chat_message_join` approach validated in Phase 0.

**Noise filtering:** Skip messages where:
- `text` is NULL/empty AND `attributedBody` parsing returns nothing (image-only messages)
- `item_type != 0` (tapbacks, edit markers, typing indicators)
- Duplicate from chunked sends (same sender, sequential ROWIDs within 2 seconds)

Also add: `get_messages_for_date(sender, date)` → all messages on a specific date (for Phase 3 compression).

### 3. Modify: `src/bluepopcorn/config.py` + `config.toml`

- Add `memory_dir: str = "data/memory"`, wire to `[paths]` in config.toml
- Remove `db_path` setting
- **Keep `history_window`** — still used as LIMIT for `get_recent_messages()`
- Remove `history_gap_hours` (replaced by `conversation_gap_hours`)
- Add `conversation_gap_hours: float = 2.0` for gap marker threshold

### 4. Modify: `instructions.md` — Absorb defaults from `memory.md`

Add a `## Defaults` section with the rule from `memory.md`.

### 5. Modify: `src/bluepopcorn/llm.py` — Update system prompt loading

- Remove `memory.md` from `_load_system_prompt()` — only load `personality.md` + `instructions.md`
- Add `summarize(prompt, schema)` method — same subprocess pattern as `decide()` but accepts a custom JSON schema and returns raw dict. Needed by Phase 3 compression, but adding now keeps Phase 3 simple

### Phase 1 Verification

1. Unit test memory.py — section parsing, duplicate detection, truncation
2. SQL query test — `get_recent_messages()` returns both directions, noise filtered
3. Config loads correctly, system prompt loads without `memory.md`
4. **Existing bot still works** — `db.py` is untouched

---

## Phase 2: The Swap (atomic — all in one commit)

Replace all `db.py` usage with `memory.py` (facts) + `monitor.py` (chat.db history) + in-memory context buffer. Delete `db.py`. Every file listed below changes together.

### DB Call Site Map

| Call | Count | Replacement |
|---|---|---|
| `db.add_history(sender, "user", text)` | 2 | Delete (chat.db has it). CLI mode: append to `_cli_history` |
| `db.add_history(sender, "assistant", response)` | 2 | Delete (chat.db has it). CLI mode: append to `_cli_history` |
| `db.add_history(sender, "context", ...)` | 8 | `self._add_context(sender, text)` |
| `db.get_history(sender)` in `_build_prompt` | 1 | `monitor.get_recent_messages()` or `_cli_history` |
| `db.get_history(sender)` in `_handle_search` | 1 | Pass `user_text` param directly (see poster intent fix) |
| `db.get_history(sender)` in `recommend.py` | 1 | Delete — context buffer handles same-session dedup, cross-session not worth it |
| `db.get_facts(sender)` in `_build_prompt` | 1 | `memory.load(sender)` (full markdown file) |
| `db.clear_history(sender)` in "new" bypass | 1 | `_clear_context()` + set `_session_start[sender]` + clear `_sent_posters` (already done) |
| `db.add_fact(sender, fact)` | 1 | `memory.add_preference(sender, fact)` |
| `db.remove_fact(sender, keyword)` | 1 | `memory.remove_preference(sender, keyword)` |

**Grep before starting** to locate every site — the counts above are validated but files may have shifted.

### 1. Rewrite: `src/bluepopcorn/actions/` package

**Constructor changes (`__init__.py`):**
```python
class ActionExecutor:
    def __init__(
        self,
        seerr, llm, sender, posters,
        memory: UserMemory,                # was: db: BotDatabase
        monitor: MessageMonitor | None,    # NEW: for chat.db reads (None in CLI)
        settings: Settings,
    ):
        self._context: dict[str, list[tuple[float, str]]] = {}
        self._session_start: dict[str, float] = {}
        self._cli_history: dict[str, list[HistoryEntry]] = {}
```

**`_build_prompt()` rewrite:**
1. Time context tag (same as now)
2. `<memory>` block from `memory.load(sender)` — full per-user markdown file
3. Get messages: `monitor.get_recent_messages()` in daemon mode, `_cli_history` in CLI mode
4. Filter by `_session_start[sender]` (messages after "new"/"reset" only)
5. Merge chat.db messages + context buffer entries into sorted timeline by timestamp
6. Insert `<gap>N hours later</gap>` between entries with 2+ hour gaps
7. Render as `<user>`, `<assistant>`, `<context>` tags

**Context buffer lifecycle:**
- `_add_context(sender, text)` → append `(time.time(), text)` to buffer
- `_clear_context(sender)` → pop sender from dict
- Cleared on: "new"/"reset" bypass, daemon restart (naturally in-memory)
- NOT auto-cleared on gap detection — gap markers handle conversation separation
- **Safety cap**: >50 context entries per sender → drop oldest
- **Restart mid-conversation**: Context lost. LLM should ask user to search again. Acceptable.

**Poster intent fix (DISCOVERED):**
`_handle_search()` re-reads `db.get_history()` to inspect the last user message for "add"/"request"/"get"/"download" keywords (collage vs single poster). Fix: pass `user_text` through `_execute()` → `_handle_search()` as a parameter.

**"new"/"reset" session boundary (DISCOVERED):**
Gap markers alone won't separate old from new when there's no time gap. Fix: `_session_start: dict[str, float]` — on "new"/"reset", set to `time.time()`. Filter out messages with timestamp < session start in `_build_prompt()`.

**CLI mode message tracking (DISCOVERED):**
CLI has no chat.db for outgoing messages. `_build_prompt()` branches: if `self.monitor is None`, read from `_cli_history`.

**Handler changes:**
- All `db.add_history(sender, "context", ...)` → `self._add_context(sender, text)`
- All `db.add_history(sender, "user/assistant", ...)` → delete (or `_cli_history` append)
- `memory.py`: `db.add_fact()` → `memory.add_preference()`, `db.remove_fact()` → `memory.remove_preference()`
- `recommend.py`: `db.get_history()` for dedup → delete. Context buffer catches same-session repeats naturally; cross-session dedup isn't worth the complexity

### 2. Modify: `src/bluepopcorn/__main__.py`

- Remove `BotDatabase` import, add `UserMemory` import
- Replace `db = BotDatabase(settings)` / `await db.init()` with `memory = UserMemory(settings)`
- Pass `memory` + `monitor` to `ActionExecutor` instead of `db`
- Remove `await db.close()` from shutdown
- ROWID cursor becomes file-based: read `data/last_rowid` on startup, write-through on each poll cycle. `write_last_rowid()` does `mkdir(parents=True, exist_ok=True)` on first write

**Note:** `digest.py` and `webhooks.py` have zero `db.py` references — no changes needed.

### 3. Modify: `src/bluepopcorn/cli.py`

- Replace `BotDatabase` with `UserMemory` — no `db.init()`, no `db.clear_history()`, no `db.close()`
- Pass `monitor=None` to `ActionExecutor` — signals CLI mode
- CLI preferences write to `data/memory/cli-user.md`

### 4. Delete: `src/bluepopcorn/db.py`

### 5. Delete: `memory.md`

### Phase 2 Verification

1. **CLI mode** (`uv run -m bluepopcorn --cli`): remember/forget, search → confirm → request, "new" clears context
2. **Gap markers**: Messages with time gaps produce `<gap>` tags
3. **Prompt inspection**: Log full prompt — memory block, gap markers, chat.db messages, context entries
4. **Daemon mode**: Send test messages via iMessage, verify responses use chat.db + markdown memory
5. **Restart test**: Stop daemon, send message, start daemon — picks up from `data/last_rowid`

---

## Phase 3: Compression (follow-up — bot works without it)

Add tiered compression so the bot builds long-term memory automatically. Runs once daily after the morning digest.

### 1. New: `src/bluepopcorn/compression.py` — Tiered compression engine

**Methods:**
- `compress_daily(sender, messages)` → send a day's raw messages to LLM, get summary + optional preference suggestions, append to `# Recent`
- `compress_weekly(sender)` → daily entries older than 7 days from `# Recent` → LLM-compress into `# Weekly`, remove originals
- `compress_monthly(sender)` → weekly entries older than 4 weeks from `# Weekly` → LLM-compress into `# History`, remove originals
- `run_compression(sender)` → orchestrate all three tiers
- `catch_up(sender, missed_days)` → process each missed day individually if bot was down

**Compression schema:**
```json
{
  "summary": "string",
  "suggested_preferences": ["string"]
}
```
`suggested_preferences` is optional. Each entry auto-appended to `# Preferences` with date tag (e.g. "Interested in Korean dramas (auto 2026-03-10)"). LLM prompted to only suggest genuine patterns, not one-off requests.

**LLM calls:** Use `LLMClient.summarize()` (added in Phase 1). Haiku. One call per sender per day.

**Compression prompt guidance:**
- Focus on what was requested/discussed, not conversation mechanics
- Skip information already in Preferences (passed as context)
- Capture intent and outcome ("Requested Severance S3, added successfully")
- Only suggest preferences for genuine repeated patterns

**Failure handling:** Log error, skip that day, catch up next run. Never block the main message loop.

**Multi-day catch-up:** Process each missed day individually via `monitor.get_messages_for_date()`. Track last compression date in `data/last_compressed`.

### 2. Modify: `src/bluepopcorn/__main__.py` — Wire into digest schedule

```python
# After digest sends...
compressor = Compressor(settings, llm, monitor, memory)
for phone in settings.allowed_senders:
    try:
        await compressor.run_compression(phone)
    except Exception as e:
        log.error("Compression failed for %s: %s", phone, e)
```

### Phase 3 Verification

1. Daily compression produces summaries in `# Recent` with auto-extracted preferences
2. Weekly/monthly rollup works at the right thresholds
3. Multi-day catch-up processes each day separately
4. Failure skips gracefully, next run catches up

---

## Edge Cases

- **Bot down for multiple days**: Compression catches up day-by-day on next run
- **Context loss on restart mid-conversation**: Accepted. User resends their request
- **Memory file grows too large**: `truncate_if_needed()` caps at ~200 lines, trimming oldest `# History` first
- **User asks "what did we talk about last week?"**: LLM has weekly summaries in the prompt
- **Duplicate preferences**: `add_preference()` fuzzy dedup (case-insensitive substring)
- **Contradicting preferences**: LLM sees both, uses the more recent one. Explicit contradiction detection is future work
- **No messages on a given day**: Compression skips — no empty summary entries
- **Compression fails**: Log error, skip, catch up next run. Never blocks message processing
- **Chat.db noise**: Image-only messages, tapbacks, edit markers filtered before prompt injection
- **Chunked bot responses**: Dedup sequential same-sender messages within 2 seconds
- **"new"/"reset" without time gap**: `_session_start` timestamp filters out pre-reset messages
- **Poster intent detection without history**: Fixed by passing `user_text` directly
- **CLI mode without chat.db**: Falls back to `_cli_history` in-memory buffer
- **Context buffer unbounded growth**: Soft cap at 50 entries per sender

## Risks

1. **chat.db JOIN correctness.** The `chat_message_join` query is untested. **Mitigation:** Phase 0.
2. **Bot messages appearing as duplicates.** Chunked sends create multiple rows. **Mitigation:** Test dedup with actual chunked messages.
3. **Context loss on restart.** By design. Could persist context buffer to file as future enhancement.
4. **Phase 2 atomicity.** Must be a single commit. **Mitigation:** Land Phase 1 first, then do Phase 2 focused.

## Migration

- Delete `bot.db`, remove `db_path` from config
- Keep `aiosqlite` dependency (still used for chat.db reads)
- Delete `memory.md` after moving its rule to `instructions.md`
- One-time: export any existing `user_facts` rows to per-user markdown files (or just re-enter, there are few)

---

## Appendix: Research

### OpenClaw — Two-Layer Markdown Memory
- **Daily logs** (`memory/YYYY-MM-DD.md`): Append-only, one per day. Today + yesterday auto-loaded at session start.
- **Curated long-term** (`MEMORY.md`): Durable facts/preferences. User-curated, not auto-generated. Key lesson: auto-growing memory becomes a junk drawer fast.
- Optional vector search (SQLite + sqlite-vec) for large memory corpus. **Not needed at our scale.**

### agent-memory — Four Categories, Tiered Depth
- **Profile** (what is true): preferences, timezone, tech stack
- **Procedures** (how to do X): workflows, deploy steps
- **Directives** (rules): "always confirm before deleting"
- **Episodes** (what happened): daily logs, conversation summaries
- **Tiered storage**: Frontmatter description (always visible) → body (on demand) → reference files (unlimited)

### mnemonic — YAML Frontmatter + Temporal Decay
- YAML frontmatter per memory file with metadata (created, modified, tags, confidence)
- **Ebbinghaus decay model**: `half_life: P7D` — recent memories weighted higher, old ones fade
- Search via ripgrep — no vector DB needed at personal scale

### Key Takeaways
1. Markdown is the lingua franca — every LLM already knows how to read/write it
2. Two tiers minimum: curated preferences + episodic summaries
3. No vector search needed at our scale
4. Auto-growing memory without compression fails — all projects agree
5. Compression trigger = end of day — matches our morning digest
