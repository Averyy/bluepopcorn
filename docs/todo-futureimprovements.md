# Future Improvements (Do After Markdown Memory)

**Note:** Infrastructure fixes that only touch `sender.py` (temp-file AppleScript trick, error dialog clearing) are in `todo-futureimprovements-priority.md` — do those first, before the markdown memory refactor.

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

Integration: replace the `asyncio.sleep(poll_interval)` in `__main__.py` with `asyncio.wait_for(change_event.wait(), timeout=poll_interval)`. The existing poll interval becomes a fallback safety net, not the normal path. New file: `src/bluepopcorn/watcher.py` (~80 lines).

## Lower Priority

### Collection Requests
Seerr supports requesting entire movie collections. "Add the entire Alien collection" → `GET /api/v1/collection/{collectionId}` for details (returns all movies in the collection with status), then request each. Niche but cool.

### Specials Handling
Skip TV specials by default when requesting — they're hard to find and requests never complete, causing broken states. Only request specials if explicitly asked.
