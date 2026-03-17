# Markdown Memory: Replace SQL History/Facts with Tiered Markdown

## Context

Currently iMessagarr duplicates every message into a SQLite `history` table and stores user preferences in a `user_facts` table. This is redundant — raw messages already exist in iMessage's `chat.db`, and flat SQL rows of preferences are just a markdown file with extra steps. Worse, conversation history older than 1 hour is silently deleted with zero compression, so the bot has no long-term memory of past interactions.

**Goal:** Replace SQL-based history and facts with per-user markdown files that support tiered compression — recent messages stay verbatim (from chat.db), older conversations get progressively summarized into the markdown file. The bot gains persistent, useful memory across days/weeks/months while keeping prompts small. Drop SQLite entirely.

## Research: How Other Projects Handle This

### OpenClaw — Two-Layer Markdown Memory
- **Daily logs** (`memory/YYYY-MM-DD.md`): Append-only, one per day. Today + yesterday auto-loaded at session start.
- **Curated long-term** (`MEMORY.md`): Durable facts/preferences. User-curated, not auto-generated. Key lesson: auto-growing memory becomes a junk drawer fast.
- Optional vector search (SQLite + sqlite-vec) for large memory corpus. **Not needed at our scale.**
- Pre-compaction memory flush — writes important context to disk before long sessions get pruned.

### agent-memory — Four Categories, Tiered Depth
- **Profile** (what is true): preferences, timezone, tech stack
- **Procedures** (how to do X): workflows, deploy steps
- **Directives** (rules): "always confirm before deleting"
- **Episodes** (what happened): daily logs, conversation summaries
- **Tiered storage**: Frontmatter description (always visible, ~1000 tokens) → body (loaded on demand, ~10k tokens) → reference files (unlimited, read when detail needed)
- SessionEnd hooks auto-capture conversations via bash script, optionally summarize with a cheap model.

### mnemonic — YAML Frontmatter + Temporal Decay
- YAML frontmatter per memory file with metadata (created, modified, tags, confidence)
- **Ebbinghaus decay model**: `half_life: P7D` — recent memories weighted higher, old ones fade
- **Bi-temporal tracking**: "when it became true" vs "when you documented it"
- Search via ripgrep — no vector DB needed at personal scale

### Key Takeaways for iMessagarr
1. **Markdown is the lingua franca** — every LLM already knows how to read/write it. No serialization layer needed.
2. **Two tiers minimum**: Curated preferences (persistent) + episodic summaries (rolling compressed). This is what we're building.
3. **No vector search needed** — we have 1-2 users, the full memory file fits in the prompt. Just load it.
4. **Auto-growing memory without compression fails** — all projects agree. Compression is essential.
5. **Compression trigger = end of day** — matches our morning digest schedule perfectly.
6. **YAML frontmatter is nice-to-have** — useful for metadata but overkill for v1. Plain markdown sections are sufficient.

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

## Rename System Prompt Files

Current state is confused — `memory.md` contains rules, `personality.md` mixes tone with rules.

**Changes:**
- `memory.md` → **delete**. Its one rule ("only recommend post-2000 titles") moves into `instructions.md` under a `## Defaults` section.
- `personality.md` → keep, but only tone/voice (already cleaned up).
- `instructions.md` → keep, absorbs global defaults from `memory.md`.
- `llm.py` → update `_load_system_prompt()` to only load `personality.md` + `instructions.md` (drop `memory.md` from the list).

Per-user memory is now injected dynamically via `_build_prompt()`, not as a static system prompt file.

## Per-User Markdown Format

File: `data/memory/{phone}.md` (e.g. `data/memory/+1XXXXXXXXXX.md`)

```markdown
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

## Files to Modify

### 1. New: `src/imessagarr/memory.py` — Per-user markdown memory manager

All methods are **synchronous** (plain file I/O). Markdown files are tiny (<200 lines). No async needed — avoids aiosqlite-style ceremony for something that takes <1ms.

Section parsing approach: split file by lines, find `# SectionName` headers, extract lines between them. Lines prefixed with `- ` are entries.

Responsibilities:
- `load(sender)` → read the full markdown file for a sender, return as string (for prompt injection). Returns empty string if file doesn't exist
- `get_preferences(sender)` → parse and return just the `# Preferences` section lines
- `add_preference(sender, fact)` → append a line under `# Preferences` (with fuzzy duplicate check — case-insensitive substring match)
- `remove_preference(sender, keyword)` → remove first matching line from `# Preferences`. Returns bool
- `append_summary(sender, date, summary, tier)` → append to `# Recent`, `# Weekly`, or `# History`
- `get_section(sender, section)` → parse and return lines from a specific section
- `replace_section(sender, section, lines)` → replace all lines in a section (used by compression for rollup)
- `load_or_create(sender)` → create the file with empty sections if it doesn't exist
- `truncate_if_needed(sender, max_lines=200)` → if file exceeds limit, trim oldest `# History` entries first
- Phone number used directly as filename (e.g. `+1XXXXXXXXXX.md`)

### 2. New: `src/imessagarr/compression.py` — Tiered compression engine

Responsibilities:
- `compress_daily(sender, messages)` → send a day's raw messages to LLM, get summary + optional preference suggestions, append to `# Recent`
- `compress_weekly(sender)` → take daily entries older than 7 days from `# Recent`, LLM-compress into `# Weekly` paragraph, remove originals
- `compress_monthly(sender)` → take weekly entries older than 4 weeks from `# Weekly`, LLM-compress into `# History`, remove originals
- `run_compression(sender, chat_db_messages)` → orchestrate all three tiers
- `catch_up(sender, missed_days)` → process each missed day individually if bot was down

**Compression schema:** Expanded to support proactive preference extraction:
```json
{
  "summary": "string",
  "suggested_preferences": ["string"]
}
```
`suggested_preferences` is optional. When present, each entry gets auto-appended to `# Preferences` with a date tag (e.g. "Interested in Korean dramas (auto 2026-03-10)"). The LLM is prompted to only suggest preferences that represent genuine patterns, not one-off requests.

**Compression LLM calls:** Use `claude -p` but via a new `LLMClient.summarize(prompt, schema)` method — NOT `decide()`, which uses the bot's action JSON schema. `summarize()` uses the same subprocess pattern but accepts a custom JSON schema and returns a raw dict. This keeps all LLM subprocess management in one place. Haiku is fine — summarizing 20 messages into a sentence is trivial. One LLM call per sender per day, separate per user to keep entries clean.

**Compression prompt guidance:** Tell the LLM to:
- Focus on what was requested/discussed, not conversation mechanics ("User said yes, bot confirmed")
- Skip information already captured in the existing Preferences section (passed as context)
- Capture intent and outcome ("Requested Severance S3, added successfully")
- Only suggest preferences for genuine repeated patterns, not one-off requests

**Failure handling:** If the LLM call fails (timeout, quota hit), log the error and skip that day. Next run catches up. Compression is best-effort, not critical path. Never block the main message loop.

**Multi-day catch-up:** If the bot was down for several days, compression processes each missed day individually rather than lumping them together. Query chat.db day-by-day. Track last compression date in the memory file or a small `data/last_compressed` file.

### 3. Modify: `src/imessagarr/monitor.py` — Add recent message query

Add method: `get_recent_messages(sender, limit, since_hours)` → query chat.db for both directions (`is_from_me` 0 and 1) for a specific sender within a time window. Returns `list[HistoryEntry]` with role="user" or role="assistant".

This replaces `db.get_history()` for prompt building. Uses the existing `_get_db()` connection and `parse_attributed_body()` for text extraction.

Join through `chat_message_join` → `chat` where `chat.chat_identifier` = the phone number. This gets both directions reliably — outgoing messages don't always have `handle_id` set:
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

**Noise filtering:** Skip messages where:
- `text` is NULL/empty AND `attributedBody` parsing returns nothing (image-only messages like posters)
- `item_type != 0` (tapback reactions, edit markers, typing indicators)
- Message is a duplicate from chunked sends (same sender, sequential ROWIDs within 2 seconds — bot response chunks can span longer than 1 second)

Also add: `get_messages_for_date(sender, date)` → query chat.db for all messages on a specific date (using `date BETWEEN ? AND ?` for day boundaries). Used by the compression engine to get yesterday's conversation for summarization.

### 4. Modify: `src/imessagarr/actions.py` — Rewrite prompt building and memory actions

This is the core of the migration. There are **18 `self.db.*` call sites** to replace:

**Complete call site map (validated against code):**

| Call | Count | Replacement |
|---|---|---|
| `db.add_history(sender, "user", text)` | 3 | Delete (chat.db has it). CLI mode: append to `_cli_history` |
| `db.add_history(sender, "assistant", response)` | 3 | Delete (chat.db has it). CLI mode: append to `_cli_history` |
| `db.add_history(sender, "context", ...)` | 6 | `self._add_context(sender, text)` |
| `db.get_history(sender)` in `_build_prompt` | 1 | `monitor.get_recent_messages()` or `_cli_history` |
| `db.get_history(sender)` in `_handle_search` | 1 | Pass `user_text` param directly (see below) |
| `db.get_facts(sender)` in `_build_prompt` | 1 | `memory.get_preferences(sender)` |
| `db.clear_history(sender)` in "new" bypass | 1 | `_clear_context()` + set `_session_start[sender]` |
| `db.add_fact(sender, fact)` | 1 | `memory.add_preference(sender, fact)` |
| `db.remove_fact(sender, keyword)` | 1 | `memory.remove_preference(sender, keyword)` |

**Constructor changes:**
```python
class ActionExecutor:
    def __init__(
        self,
        seerr, llm, sender, posters,
        memory: UserMemory,                # was: db: BotDatabase
        monitor: MessageMonitor | None,    # NEW: for chat.db reads (None in CLI)
        settings: Settings,
    ):
        self._context: dict[str, list[tuple[float, str]]] = {}    # context buffer
        self._session_start: dict[str, float] = {}                 # per-sender reset timestamps
        self._cli_history: dict[str, list[HistoryEntry]] = {}      # CLI mode only
```

**`_build_prompt()` rewrite:**
1. Time context tag (same as now)
2. `<memory>` block from `memory.load(sender)` — full per-user markdown file
3. Get messages: `monitor.get_recent_messages()` in daemon mode, `_cli_history` in CLI mode
4. Filter by `_session_start[sender]` (messages after "new"/"reset" only)
5. Merge chat.db messages + context buffer entries into sorted timeline by timestamp
6. Insert `<gap>N hours later</gap>` between entries with 2+ hour gaps (use `conversation_gap_hours` setting)
7. Render as `<user>`, `<assistant>`, `<context>` tags

**Poster intent fix (DISCOVERED — not in original spec):**
`_handle_search()` re-reads `db.get_history()` at line 342 to inspect the last user message for "add"/"request"/"get"/"download" keywords, deciding collage vs single poster. Fix: pass `user_text` through `_execute()` → `_handle_search()` as a parameter. Check `user_text.lower()` directly instead of re-reading history.

**"new"/"reset" session boundary (DISCOVERED — not in original spec):**
The original spec says "clear context buffer on reset", but gap markers alone won't separate old from new conversation when there's no time gap (user says "new" and immediately sends a message). Fix: maintain `_session_start: dict[str, float]` — on "new"/"reset", set `_session_start[sender] = time.time()`. In `_build_prompt()`, filter out all messages with timestamp < `_session_start[sender]`.

**CLI mode message tracking (DISCOVERED — not in original spec):**
CLI has no chat.db for outgoing messages. `_build_prompt()` must branch: if `self.monitor is None` (CLI mode), read from `_cli_history` instead. In `handle_message()`, append user/assistant messages to `_cli_history` when in CLI mode.

**Context buffer lifecycle:**
- `_add_context(sender, text)` → append `(time.time(), text)` to buffer
- `_clear_context(sender)` → pop sender from dict
- Cleared on: "new"/"reset" bypass, daemon restart (naturally in-memory)
- NOT auto-cleared on gap detection — gap markers handle conversation separation
- **Safety cap**: If a sender has >50 context entries, drop the oldest. Prevents unbounded growth
- **Restart mid-conversation**: Context (search results) is lost. LLM won't know what "yes" confirms and should ask user to search again. Acceptable per spec

**`_handle_remember()`** → call `memory.add_preference()` instead of `db.add_fact()`
**`_handle_forget()`** → call `memory.remove_preference()` instead of `db.remove_fact()`

### 5. Delete: `src/imessagarr/db.py`

Remove entirely. ROWID cursor becomes an in-memory int with a write-through file cache:
- On startup: read `data/last_rowid` if it exists, otherwise fall back to `monitor.get_max_rowid()`
- On each poll cycle: update the in-memory int, write to `data/last_rowid` (just `str(rowid)` — one integer in a plain text file)
- This means restarts pick up where they left off, no missed messages
- Lives in `__main__.py` or a tiny helper, not a whole db module

### 6. Modify: `src/imessagarr/__main__.py` — Replace db wiring + wire compression

**Replace BotDatabase with UserMemory:**
- Remove `BotDatabase` import, add `UserMemory` import
- Replace `db = BotDatabase(settings)` / `await db.init()` with `memory = UserMemory(settings)`
- Pass `memory` + `monitor` to `ActionExecutor` instead of `db`
- Remove `await db.close()` from shutdown

**ROWID cursor becomes file-based:**
- On startup: read `data/last_rowid` if it exists, else `await monitor.get_max_rowid()`
- On each poll cycle: write `str(rowid)` to `data/last_rowid` (write-through)
- Simple `read_last_rowid()` / `write_last_rowid()` helper functions, not a class

**Wire compression into `_schedule_digest()`:**
```python
# After digest sends...
compressor = Compressor(settings, llm, monitor, memory)
for phone in settings.allowed_senders:
    try:
        await compressor.run_compression(phone)
    except Exception as e:
        log.error("Compression failed for %s: %s", phone, e)
```

**Note:** `digest.py` and `webhooks.py` have zero `db.py` references — confirmed no changes needed.

### 7. Modify: `src/imessagarr/config.py` — Update settings

- Add `memory_dir: str = "data/memory"` setting, wire to `[paths]` in config.toml
- Remove `db_path` setting entirely (no more SQLite)
- **Keep `history_window`** — still used as the LIMIT for `monitor.get_recent_messages()` chat.db query
- Remove `history_gap_hours` (replaced by `conversation_gap_hours`)
- Add `conversation_gap_hours: float = 2.0` for gap marker threshold

### 8. Modify: `config.toml` — Update paths

```toml
[paths]
memory_dir = "data/memory"
# db_path removed — no more SQLite

[messages]
conversation_gap_hours = 2.0
```

### 9. Modify: `src/imessagarr/llm.py` — Update system prompt loading

- Remove `memory.md` from the file list in `_load_system_prompt()`
- Only load `personality.md` + `instructions.md`
- Add `summarize(prompt, schema)` method for compression — same subprocess pattern as `decide()` but accepts a custom JSON schema and returns raw dict (not `LLMDecision`). This is needed because `decide()` hardcodes the action JSON schema

### 10. Modify: `instructions.md` — Absorb global defaults

Add a `## Defaults` section with rules from the old `memory.md`:
```markdown
## Defaults
- Only recommend movies/shows released after 2000 unless the user specifically asks for older titles.
```

### 11. Delete: `memory.md`

No longer needed — global rules live in `instructions.md`, per-user memory lives in `data/memory/`.

### 12. Modify: `src/imessagarr/cli.py` — Update for new memory system

CLI test mode needs to work without chat.db:
- Replace `BotDatabase` with `UserMemory` — no `db.init()`, no `db.clear_history()`, no `db.close()`
- Pass `monitor=None` to `ActionExecutor` — this signals CLI mode
- `ActionExecutor` uses `_cli_history` in-memory buffer when `monitor is None` (see actions.py changes)
- CLI preferences write to `data/memory/cli-user.md`
- Context buffer works the same as daemon mode

## Prompt Structure (after changes)

```
<context>[Current time: Monday March 16, 2026 2:30 PM EDT]</context>

<memory>
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

The `<memory>` block is the full per-user markdown file contents. Recent raw messages come from chat.db with gap markers between separate conversations. Context entries come from the in-memory buffer, interleaved by timestamp. This keeps the prompt compact while giving the LLM both immediate conversation context and long-term memory.

## Token Budget

Estimated per-call token usage:
- System prompt (personality + instructions): ~1000 tokens
- Per-user memory file: ~500 tokens (grows slowly with compression)
- Chat.db recent messages (20 messages): ~2500 tokens
- Context entries (search results, weather): ~1000 tokens
- **Total: ~5000 tokens per call**

Haiku's 200K context makes this a non-issue. If the memory file ever grows unexpectedly, `truncate_if_needed()` trims oldest `# History` entries first to stay under ~200 lines (~3000 tokens).

## Edge Cases

- **Bot down for multiple days**: Compression catches up day-by-day on next run, not lumped together. Tracks last compression date.
- **Context loss on restart mid-conversation**: Accepted as rare. User resends their message. LLM won't know what "yes" confirms and should ask user to search again.
- **Memory file grows too large**: `truncate_if_needed()` caps at ~200 lines, trimming oldest `# History` first.
- **User asks "what did we talk about last week?"**: LLM has weekly summaries in the prompt — can answer naturally.
- **Duplicate preferences**: `add_preference()` checks for fuzzy duplicates (case-insensitive substring) before appending. Auto-extracted preferences tagged with `(auto YYYY-MM-DD)`.
- **Contradicting preferences**: If user says "remember I like horror" after "remember I don't like horror", the `remember` action just appends. The LLM sees both and uses the more recent one. Could add explicit contradiction detection later.
- **No messages on a given day**: Compression skips days with no messages — no empty summary entries.
- **Compression fails**: Log error, skip that day, catch up next run. Never blocks message processing.
- **Chat.db noise**: Image-only messages (posters), tapback reactions, and edit markers filtered out before prompt injection.
- **Chunked bot responses**: Long messages sent as multiple bubbles may appear as separate entries in chat.db. Filter by deduplicating sequential same-sender messages within 2 seconds (bot chunks can span >1s).
- **"new"/"reset" without time gap**: `_session_start` timestamp filters out pre-reset messages even when gap markers can't help (user resets and immediately sends a message).
- **Poster intent detection without history**: `_handle_search()` currently re-reads history to check for "add" keywords. Fixed by passing `user_text` directly as a parameter through `_execute()`.
- **CLI mode without chat.db**: Falls back to `_cli_history` in-memory buffer. Preferences still persist to `data/memory/cli-user.md`.
- **Context buffer unbounded growth**: Soft cap at 50 entries per sender, drops oldest when exceeded.

## Migration

- Delete `bot.db`, remove `db_path` from config
- Keep `aiosqlite` dependency (still used for chat.db reads)
- Delete `memory.md` after moving its rule to `instructions.md`
- One-time: export any existing `user_facts` rows to per-user markdown files (or just re-enter, there are few)

## Implementation Order

Three phases. The bot works after Phase 2 without compression — Phase 3 is a follow-up.

### Phase 1: Foundation (additive — nothing breaks, existing code still works)

1. `memory.py` — new module, no dependencies on existing code
2. `monitor.py` — add `get_recent_messages()` and `get_messages_for_date()` with noise filtering
3. `config.py` + `config.toml` — add `memory_dir`, `conversation_gap_hours`, keep `history_window`, remove `db_path` + `history_gap_hours`
4. `instructions.md` — absorb defaults from `memory.md`
5. `llm.py` — drop `memory.md` from system prompt loading, add `summarize()` method

### Phase 2: The Swap (atomic — all in one commit, breaks if partial)

6. `actions.py` — rewrite constructor, `_build_prompt()`, all 18 db call sites, add context buffer + session start + CLI history + poster intent fix
7. `__main__.py` — replace BotDatabase with UserMemory, ROWID file cache, new ActionExecutor wiring
8. `cli.py` — replace BotDatabase with UserMemory, pass monitor=None
9. Delete `db.py`
10. Delete `memory.md`

### Phase 3: Compression (follow-up — bot works without it)

11. `compression.py` — new module, tiered summarization
12. `__main__.py` — wire compression into `_schedule_digest()`

**Why 3 phases:** Phase 1 is safe to land independently. Phase 2 must be atomic (actions.py expects `memory` but __main__.py passes `db` would break). Phase 3 is independent — compression is fire-and-forget, the bot functions without it.

## Risks

1. **chat.db JOIN correctness.** The `chat_message_join` query is untested. If `chat_identifier` doesn't match our phone format, outgoing messages will be missing. **Mitigation:** Test the SQL directly against chat.db before integrating — this should be step 0.
2. **Bot messages appearing as duplicates.** Chunked sends create multiple rows in chat.db. The dedup logic (sequential `is_from_me=1` within 2s) must handle this. **Mitigation:** Test with actual chunked messages in chat.db.
3. **Context loss on restart.** By design. If it becomes frequent, could persist context buffer to a file as a future enhancement.
4. **Phase 2 atomicity.** The swap must be a single commit — partial application breaks the bot. **Mitigation:** Implement and test Phase 1 first, then do Phase 2 as one focused commit.

## Verification

0. **SQL test first (before any code)**: Run the `chat_message_join` query directly against chat.db with a known phone number. Verify it returns both incoming (`is_from_me=0`) and outgoing (`is_from_me=1`) messages with correct content. This validates the JOIN and phone format matching before we build on it.
1. **Unit test memory.py**: Create/read/modify per-user markdown files, verify section parsing, duplicate detection, truncation
2. **CLI mode** (`uv run -m imessagarr --cli`): Test full conversation flow with markdown memory
   - "remember I like sci-fi" → check markdown file created with preference
   - "forget sci-fi" → check line removed
   - Conversation context works (search → confirm → request)
3. **Compression test**: Manually trigger compression with sample messages, verify:
   - Daily summaries appear in `# Recent`
   - Auto-extracted preferences appear in `# Preferences` with `(auto)` tag
   - Weekly/monthly rollup works
   - Multi-day catch-up processes each day separately
   - Failure is handled gracefully (skip + log)
4. **Gap marker test**: Send messages with time gaps, verify `<gap>` markers appear in prompt
5. **Noise filter test**: Send images, tapbacks — verify they don't appear in prompt
6. **Prompt inspection**: Log the full prompt to verify structure — memory block, gap markers, chat.db messages, context entries all correct
7. **Token budget check**: Log prompt token count, verify it stays in the ~5000 range
8. **Daemon mode**: Run daemon, send test messages via iMessage, verify responses use chat.db history + markdown memory
9. **Restart test**: Stop daemon, send a message, start daemon — verify it picks up from `data/last_rowid` and processes the message
