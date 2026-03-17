# Future Improvements

**Note (2026-03-16):** The current "Robust Seerr Integration" plan addresses: URL encoding fix, enum corrections, error differentiation, search/discover quality, ratings enrichment, request dedup, concurrent title resolution, dynamic genre loading, health check, and trailer url fix. Items below are AFTER that plan is complete.

## High Priority

### Completion Notifications
When media becomes available, text the user who requested it: "Severance S3 is ready to watch." Webhook listener already exists — just need to match requestor to phone number and fire the message. The #1 loved feature across every Seerr bot (Requestrr, Doplarr, Telegram bot). Per-episode notifications too, not just full seasons.

### Season Selection for TV
Currently requests all seasons. Ask "All seasons or just S3?" when requesting a show that's partially available. Seerr API supports `seasons` field (array of ints or `"all"`) in POST `/api/v1/request` body. Can also modify existing requests via `PUT /api/v1/request/{requestId}` to add/remove seasons.

### Auto-Suggest in Morning Digest
Use stored user preferences + discover endpoints to find new releases matching taste. Full params now available: `genre`, `sortBy=popularity.desc`, `voteAverageGte`, `voteCountGte`, `primaryReleaseDateGte/Lte`, `certification`, `excludeKeywords`. Include one suggestion: "New this week: Companion (2025) — sci-fi thriller, 7.8 on TMDB. Want me to add it?"

### Recently Added Digest (Newsletter Replacement)
The most-missed Ombi feature. Seerr never built a "recently added" email digest. Our morning text already does this — make sure it's comprehensive (movies, TV episodes, not just titles).

### Upcoming for Existing Shows
"When's the next episode of Severance?" → `GET /api/v1/tv/{tvId}` returns `nextEpisodeToAir` with air date, and `GET /api/v1/tv/{tvId}/season/{seasonNumber}` gives full episode list with dates. Very common question, data is already there.

### Request Count Summary
`GET /api/v1/request/count` returns `{total, pending, approved, processing, available, completed}` in one lightweight call. Use for quick overview: "You have 3 pending, 2 downloading, 12 available" without fetching full request lists. Could power a dashboard-style status response.

## Medium Priority

### Bulk Requests
"Add 1 and 3" after recommendations, or "Add The Dark Knight, The Simpsons, and The Simpsons Movie" in one message. Parse multiple selections/titles and batch-request them. Plex Concierge's recommendation-to-request pipeline was a praised killer feature.

### Actor/Director Search
"What else has Oscar Isaac been in?" → search returns PersonResult, then `GET /api/v1/person/{personId}/combined_credits` returns full filmography. Filter by movie/TV, sort by popularity or year. Natural question for discovery.

### Multilingual Search
"Add the Spanish version of Money Heist" or search by original title (La Casa de Papel). Seerr search already handles original titles but we could be smarter about surfacing the right language version.

### Natural Language → TMDB Filters
SuggestArr does this well. "A psychological thriller from the 90s with a twist ending" → LLM translates to structured discover query (genre IDs, year range, min rating, language). More powerful than keyword matching.

### Calendar / Upcoming Releases
"What's coming out this week?" → query TMDB upcoming releases, filtered by user preferences. Seerr has `GET /api/v1/discover/movies/upcoming` and `GET /api/v1/discover/tv/upcoming`. Could also add to morning digest.

## Infrastructure / Reliability

### Error Dialog Clearing (from CamHenlin/imessageclient)
AppleScript send failures can leave modal error dialogs in Messages.app ("There was an error sending the previous message" with Ignore/Open Messages/Resend buttons) that block ALL future sends silently. We retry with backoff but never dismiss the dialog, so the bot goes deaf after a single failure until Messages.app is manually interacted with.

**Approach:** Add a `_dismiss_error_dialogs()` method to `sender.py`, called before each send retry. No major project does this well — CamHenlin just presses Enter blindly, BlueBubbles clicks `button 1` of extra windows.

```applescript
tell application "System Events"
    tell process "Messages"
        -- Escape dismisses sheets/popovers without triggering actions
        try
            key code 53
            delay 0.2
        end try
        -- Dismiss extra windows (error dialogs appear as separate windows)
        try
            set winCount to count windows
            repeat while winCount > 1
                tell window 1
                    try
                        click button "Ignore"
                    on error
                        try
                            click button "OK"
                        on error
                            try
                                click button 1
                            end try
                        end try
                    end try
                end tell
                delay 0.3
                set winCount to count windows
            end repeat
        end try
    end tell
end tell
```

Wire into `send_text()` retry loop: call `_dismiss_error_dialogs()` after each failed attempt, before the backoff sleep. Wrap in try — this is best-effort cleanup, never block on it. Requires Accessibility (already granted).

### Temp-File Trick for AppleScript Escaping (from imessage_tools)
Write the message to a temp file, then use `read (POSIX file "...") as «class utf8»` in AppleScript instead of string interpolation. Message content never enters AppleScript string literals, eliminating ALL escaping edge cases — emoji, quotes, backslashes, newlines, Unicode. Strictly superior to our `_escape_applescript()` string replacements.

**Approach:** Replace `_build_send_text_script()` in `sender.py`. Use `tempfile.mkstemp()` for unique paths (the original imessage_tools code has a race condition with a hardcoded filename). Cleanup via `try/finally`.

```python
async def _send_text_once(self, phone: str, message: str) -> tuple[bool, str]:
    fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="imessagarr_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(message)
        safe_phone = self._sanitize_phone(phone)
        script = f'''
tell application "Messages"
    set targetAccount to first account whose service type = iMessage
    set targetParticipant to participant "{safe_phone}" of targetAccount
    send (read (POSIX file "{tmp_path}") as «class utf8») to targetParticipant
end tell
'''
        return await self._run_applescript(script)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
```

This replaces `_build_send_text_script()` + `_escape_applescript()`. The `_sanitize_phone()` check stays (phone is still interpolated). Each concurrent send gets a unique temp path — no race conditions. Low effort, high reliability.

### File-Watcher on chat.db + WAL + SHM (from imsg)
Watch all 3 SQLite files (db, WAL, SHM) via kqueue VNODE events instead of fixed-interval polling. WAL fires on every INSERT/COMMIT (when new messages arrive); the main db only fires during checkpoint. Near-instant message detection vs our current 2s poll ceiling.

**Correction:** BlueBubbles does NOT use file watching — they poll like us. Only imsg (Swift) does this, via `DispatchSource` with `O_EVTONLY` on all 3 files, 250ms debounce.

**Approach:** Raw `select.kqueue()` + `asyncio.add_reader()`. Zero dependencies (stdlib only, macOS-only). The kqueue fd becomes readable when VNODE events are pending, so it integrates natively with asyncio. Open files with `O_EVTONLY` (0x8000, macOS event-only flag — doesn't prevent volume unmount). Watch for `KQ_NOTE_WRITE | KQ_NOTE_EXTEND | KQ_NOTE_DELETE | KQ_NOTE_RENAME`. Debounce via `loop.call_later(0.25, poll)`.

Key findings from testing:
- WAL fires `WRITE|EXTEND` on every INSERT/COMMIT — this is the primary trigger
- Main DB fires only during WAL checkpoint — useless alone
- SHM fires on connection open — minor
- `O_EVTONLY` fds survive WAL truncation and continue working
- aiosqlite read-only connections see latest WAL state automatically — no reconnect needed
- If WAL/SHM don't exist at startup, skip them (they appear when Messages.app opens the db)

Integration: replace the `asyncio.sleep(poll_interval)` in `__main__.py` with `asyncio.wait_for(change_event.wait(), timeout=poll_interval)`. The existing poll interval becomes a fallback safety net, not the normal path. New file: `src/imessagarr/watcher.py` (~80 lines).

## Lower Priority

### Collection Requests
Seerr supports requesting entire movie collections. "Add the entire Alien collection" → `GET /api/v1/collection/{collectionId}` for details (returns all movies in the collection with status), then request each. Niche but cool.

### Specials Handling
Skip TV specials by default when requesting — they're hard to find and requests never complete, causing broken states. Only request specials if explicitly asked.
