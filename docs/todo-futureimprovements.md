# Future Improvements

## High Priority

### Completion Notifications
When media becomes available, text the user who requested it: "Severance S3 is ready to watch."

**Current state:** Webhook listener works and fires on `MEDIA_AVAILABLE`, but broadcasts to ALL allowed senders instead of the original requester.

**Remaining work:**
- Extract `requestId` from Seerr webhook payload (needs testing — unclear if Seerr includes it)
- If available: call `/api/v1/request/{requestId}` to get `requestedBy` user info
- Map Seerr user IDs to phone numbers (new lookup table or reverse mapping)
- Change `on_notification` callback to accept a target phone number
- Alternative: store request→sender_phone mapping when `handle_request` fires, match on webhook

### Season Selection for TV
Ask "All seasons or just S3?" when requesting a show that's partially available.

**Current state:** Backend is fully ready — `request_media()` accepts `seasons` param, `extract_season_numbers()` works. But the LLM never asks.

**Remaining work:**
- Add `seasons` field to `DECIDE_SCHEMA` and `RESPOND_SCHEMA` in schemas.py
- Add prompt guidance in prompts.py: "If requesting TV, ask which seasons unless the user specified"
- Handle the case where LLM returns seasons in the response schema (call-2 request follow-up)

### Auto-Suggest in Morning Digest
Use discover endpoints to find new releases matching user taste. Include one suggestion: "New this week: Companion (2025) — sci-fi thriller, 7.8 on TMDB. Want me to add it?"

**Current state:** Morning digest only shows recently available + pending counts. No recommendation logic.

**Remaining work:**
- Call `discover_movies()`/`discover_tv()` with recent date range + high rating filter
- Add a suggestion block to the digest output
- Optionally use stored user preferences to personalize

### Upcoming Releases / Calendar
"What's coming out this week?" or "When does the new Marvel movie drop?"

**Current state:** `discover_movies()`/`discover_tv()` support year range filtering, but no dedicated upcoming endpoints are called.

**Remaining work:**
- Add `discover_upcoming_movies(take)` and `discover_upcoming_tv(take)` methods to SeerrClient calling `/api/v1/discover/movies/upcoming` and `/api/v1/discover/tv/upcoming`
- Add "upcoming" as a recognized intent in the recommend prompt
- Could also add an `upcoming` boolean field to the LLM decision schema

## Medium Priority

## Infrastructure

### File-Watcher on chat.db (kqueue)
Watch chat.db + WAL + SHM via kqueue VNODE events instead of polling. Near-instant message detection vs current poll interval.

**Current state:** Not implemented. Still using fixed-interval polling.

**Approach (researched):** Raw `select.kqueue()` + `asyncio.add_reader()`. Zero dependencies (stdlib, macOS-only). Open files with `O_EVTONLY` (macOS event-only flag). Watch for `KQ_NOTE_WRITE | KQ_NOTE_EXTEND | KQ_NOTE_DELETE | KQ_NOTE_RENAME`. Debounce via `loop.call_later(0.25, poll)`. WAL fires on every INSERT/COMMIT (primary trigger). Replace `asyncio.sleep(poll_interval)` with `asyncio.wait_for(change_event.wait(), timeout=poll_interval)` so poll interval becomes a fallback. ~80 lines in new `watcher.py`.

## Lower Priority

### Collection Requests
"Add the entire Alien collection" → `GET /api/v1/collection/{collectionId}` for details, then request each movie. Niche but cool.

