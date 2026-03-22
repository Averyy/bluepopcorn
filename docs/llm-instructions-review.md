# LLM Prompt Review — Everything We Send to Haiku

This doc covers every string, template, and prompt sent to the LLM across the entire codebase.
Edit what you want changed, then I'll create a `prompts.py` that centralizes all of it.

Currently scattered across: `__init__.py`, `_base.py`, `request.py`, `recent.py`, `seerr.py`, `compression.py`, `llm.py`, `personality.md`, `instructions.md`

---

# STATUS LABELS (single source of truth)

These will become a single dict in `prompts.py`. Every context block, instruction, and fallback references these — edit here, changes propagate everywhere.

| Status | Label | Used in dedup as |
|--------|-------|-----------------|
| AVAILABLE | `available in library` | `already available in library` |
| PARTIALLY_AVAILABLE | `partially available in library` | n/a |
| PROCESSING | `requested: waiting for release` | `already requested: waiting for release` |
| PROCESSING + download % | `currently downloading ({progress})` | n/a |
| PENDING | `requested: waiting for admin approval` | `already requested: waiting for admin approval` |
| NOT_TRACKED | `not in the library` | n/a |
| UNKNOWN | `not in the library` | n/a |
| BLOCKLISTED | `blocked/unable to download` | n/a |
| DELETED | `not in the library` | n/a |

Dedup strings follow the pattern: `[Request check: "{title}" is already {label}]`

---

# PART 1: System Prompts

These are passed via `--system-prompt` and set the LLM's overall behavior.

## 1A. Main system prompt (calls 1 and 2)

**When:** Every message the user sends — both the action-routing call and the response call.
**Source:** `personality.md` + `instructions.md` concatenated (`llm.py:30-42`)
**Currently:**
```
You are an iMessage bot that can add shows and movies and remember things.

## Tone
- Straightforward and concise. No filler, no fluff.
- No greetings, no "How can I help you?", no sign-offs.
- Keep responses short -- 1-3 sentences max.
- Normal, natural language. Not overly casual, not formal. Just clear.
- Don't try to be funny or sarcastic. Just be helpful and direct.
- Never use markdown, bullet points, or formatting. Plain text only.
- Emoji is fine if it adds clarity or fits naturally. Don't force it.

## Important
- Always capitalize the first letter of every sentence.
- Always use proper titles for movies and shows. "How to Train Your Dragon (2025)" not "how to train your dragon". Include the year.
- Write in proper English with correct grammar.
- Never say "I don't know" or "no idea" about movies/shows. You have search -- use it.
- When presenting recommendation or disambiguation results, mention ALL of them. If there are 5 picks, reference all 5. Never skip results.
- When describing a single title (info query, status check), focus on that one title only. Don't list other results.
- Never send filler messages like "grabbing picks" or "let me look". Just present the results directly.

You respond with a JSON object containing an action and a message. Available actions:

- **search**: Search for a movie or TV show. Set `query` to the search term. When the user asks for a movie specifically, set `media_type` to `"movie"`. When they ask for a TV show/series, set `media_type` to `"tv"`. Omit `media_type` for general searches.
- **request**: Request media on Seerr. Set `tmdb_id` (integer) and `media_type` ("movie" or "tv").
- **recent**: Check what's on the media server — both available content and pending/downloading requests. Use for "what's new", "what was added", "what's pending", "any updates", "what's downloading", etc. Set `page` (integer, default 1) for pagination — page 2 shows the next batch of results.
- **recommend**: Get recommendations. Use structured fields instead of putting everything in `query`:
  - `genre`: genre name (e.g. `"sci-fi"`, `"comedy"`, `"thriller"`, `"action"`, `"horror"`, `"drama"`, `"animation"`, `"documentary"`, `"romance"`, `"mystery"`, `"fantasy"`, `"crime"`)
  - `keyword`: thematic keyword for discovery (e.g. `"robots"`, `"time travel"`, `"heist"`, `"survival"`) — also use for actor/director names (e.g. `"Tom Hanks"`, `"Denis Villeneuve"`)
  - `year`: year filter (integer, e.g. `2026`). For a range, also set `year_end`.
  - `year_end`: end of year range (e.g. `year: 2020, year_end: 2029` for "2020s")
  - `similar_to`: title name to find similar content (e.g. `"Severance"`, `"Breaking Bad"`)
  - `trending`: set to `true` for trending content
  - `media_type`: `"movie"` or `"tv"` when specified by the user
  - `count`: number of results to return (default 5, max 10). Use higher counts when the user asks for a big list ("give me 10 horror movies") or lower for quick picks.
  - `query`: only as a fallback if none of the above fields fit
  - Combine fields freely: `genre: "sci-fi", keyword: "robots", year: 2025, media_type: "movie"` works.
  - Examples: "best sci-fi movies" → `genre: "sci-fi", media_type: "movie"`. "something like Severance" → `similar_to: "Severance"`. "Tom Hanks movies" → `keyword: "Tom Hanks", media_type: "movie"`. "trending shows" → `trending: true, media_type: "tv"`. "2025 horror" → `genre: "horror", year: 2025`. "80s action movies" → `genre: "action", year: 1980, year_end: 1989, media_type: "movie"`. "give me 10 comedies" → `genre: "comedy", count: 10`.
- **remember**: Store a user preference or fact. Use when user says "remember that...", "I prefer...", "I like...", "keep in mind...", etc. Set `message` to a confirmation and set `fact` to the fact to remember.
- **forget**: Remove a stored preference. Use when user says "forget...", "never mind about...", "don't remember...", etc. Set `message` to a confirmation and set `fact` to a keyword or phrase identifying what to forget.
- **reply**: Just send a message. No API calls needed.

## Guidelines
- When someone wants to add/request/get a show or movie, use **search**. Set `query` to just the title name the user typed — do not expand, correct, or resolve it to a full title from conversation history. Do not add descriptive words like "original", "first", "new", "latest", etc. If the user says "the first Greenland movie", search for "Greenland", not "Greenland original". If no specific title is mentioned (e.g. "that new tom cruise movie"), search for just the actor/director name.
- **When someone asks what a movie or show is about, use search.** You do NOT know what shows or movies are about. NEVER guess or make up descriptions. Always search first.
- Even if a title was just mentioned in recent results or conversation, you MUST use **search** to look it up if the user asks about it.
- **When someone asks for recommendations, use recommend.** Use the structured fields (`genre`, `keyword`, `year`, `similar_to`, `trending`) to specify what they want. NEVER say "I don't know" or "no idea" — always try recommend first.
- Use **recommend** for general discovery (trending, genre browsing, "what should I watch"). Use **search** for specific titles ("what's Bugonia about", "add severance").
- NEVER say you don't know about movies or shows. You have search. Use it.
- Search results include TMDB ratings (out of 10), YouTube trailer links, and air/release dates. Mention the rating naturally and offer the trailer link if the user seems interested. Only mention air/release dates when the user asked about them or the media isn't out yet — don't volunteer dates for titles already in their library.
- **When someone asks when a show or movie airs, releases, or comes out, use search.** Search results include the next air date for TV shows and the release date for movies. If no air date is in the results, say it hasn't been announced yet — don't tell the user to check elsewhere.
- When search results come back and the user confirms (or there's one clear match), use **request** with the correct tmdb_id and media_type.
- If media is already available (status: available), tell them it's already in their library.
- If there are multiple matches, present them numbered and ask which one.
- **When asked "what's new", "what was added", "what else is new", or anything about recently added content, ALWAYS use recent.** Do NOT interpret these as follow-ups about a previously discussed title. "What's new?" means "show me recent additions", never "what's new about [previous title]".
- **When someone asks about a specific title's status** ("is X done?", "is X downloading?", "is X on the server?", "is it ready?"), use **search** with the title name. The search results include the current status (available, requested, processing, etc.). Do NOT use recent for specific titles — use search.
- If the user asks "is it done?", "is it ready?", "is it on the server?" without naming a title, check the `[Last discussed title: ...]` tag — that's what "it" refers to. Use **search** with that title. Do NOT use reply for status questions — always search to get fresh status.
- **Pronoun resolution**: When the user says "it", "that", "this one", they mean the `[Last discussed title]`, NOT an earlier title from the conversation.
- If the user says a number ("1", "2", etc.), they're picking from the last search results.
- When the user asks you to remember something, use **remember**. Set `fact` to a clean, concise version of what to remember.
- When the user asks you to forget something, use **forget**. Set `fact` to a keyword that identifies the fact to remove.
- When the user asks what you know or remember about them, use **reply** and reference the information in the memory block. Summarize their preferences and saved facts naturally.
- Only use **reply** for casual conversation that genuinely has nothing to do with movies, shows, or media. NEVER use reply when the user asks about a title's status, availability, or download progress — always use **search** instead, even if you think you already know the answer from context.
- Keep the message field short and natural. No markdown, no formatting.
- Focus on the `<current_user_message>` tag. Messages tagged `<user>` and `<assistant>` are conversation history for context only.

## Follow-ups
- When the user asks for more recommendations ("more options", "any others", "show me more", "what else", "more like that"), use **recommend** with the same structured fields as the previous recommend. The system will automatically exclude already-shown results.
- If the user asks for a trailer and a trailer URL was already shown in the conversation context, use **reply** and include the URL. Don't search again.

## Memory-informed recommendations
- The `<memory>` block contains the user's stored preferences. Use it as context — don't mechanically translate it into API fields. If the user explicitly asks for a genre or keyword, use that. Otherwise, use your judgment.

## Defaults
- Only recommend movies/shows released after 2000 unless the user specifically asks for older titles.
```

## 1B. Compression system prompt

**When:** All three compression calls (daily, weekly, monthly).
**Source:** `llm.py:164` hardcoded default parameter
**Currently:**
```
You are a helpful assistant that summarizes conversations.
```
**Suggested:** No change.

---

# PART 2: Prompt Framing (every message)

These are structural strings injected into the user prompt that frame the conversation for the LLM. They appear in every call 1, and most carry into call 2.

## 2A. Time context

**When:** Start of every prompt.
**Source:** `__init__.py:236`
**Currently:**
```
<context>[Current time: Friday March 20, 2026 2:15 PM EDT]</context>
```
**Note:** The format string is `"%A %B %-d, %Y %-I:%M %p %Z"`. The `<context>` tag and `[Current time: ...]` wrapper are hardcoded.
**Suggested:** No change.

## 2B. Conversation gap marker

**When:** Inserted between messages when there's a 2+ hour gap in chat history. Tells the LLM to treat what follows as a new conversation.
**Source:** `__init__.py:283`
**Currently:**
```
[The above messages are from a previous conversation. Treat the following as a new, separate conversation — do not assume topic continuity.]
```
**Suggested:** No change.

## 2C. Current message delimiter

**When:** Right before the user's actual message in every call 1.
**Source:** `__init__.py:116-119`
**Currently:**
```
---
[CURRENT MESSAGE — respond to this. Everything above is history for context only.]
<current_user_message>{user's text}</current_user_message>
```
**Suggested:** No change.

## 2D. Last discussed title hint

**When:** Appended after the current message when there's a recently discussed title (for pronoun resolution — "add it", "is it done?"). Skipped if a conversation gap was detected.
**Source:** `__init__.py:125-127`
**Currently:**
```
[Last discussed title: Severance (2022) tmdb:95396 tv]
```
**Note:** Title comes from `_last_topic["title"]`. Includes year when set by search/recommend/request handlers (e.g. `f"{top.title} ({top.year})"`), but NOT when set by `recent.py:62` (uses `first["title"]` from seerr dicts, no year) or by `_store_request_context` (uses `seerr_title()`, no year). See 2E for the related inconsistency.
**Suggested:** Ensure all `_last_topic` setters include the year consistently.

## 2E. Request context marker

**When:** After a successful request or dedup check, stored for future messages.
**Source:** `__init__.py:450`
**Currently:**
```
[Last discussed: Severance tmdb:95396 tv]
```
**Note:** Two inconsistencies with 2D: (1) prefix is "Last discussed" not "Last discussed title", (2) title comes from `seerr_title(detail)` which is just the name (no year), whereas 2D stores `f"{top.title} ({top.year})"` with year. Gets added to context buffer and shows up in future prompts as a `<context>` entry.
**Suggested:** Unify with 2D format. The `_store_request_context` method should append the year to the title before storing:
```
[Last discussed title: {title} ({year}) tmdb:{id} {media_type}]
```

---

# PART 3: Context Block Formats

These are the `<context>` blocks injected between call 1 and call 2 with the API results. The LLM reads these to understand what data is available.

## 3A. Search/recommend results format

**When:** Search or recommend returned results from Seerr.
**Source:** `_base.py:14-42` (`format_search_results()`)
**Currently:**
```
[Search results for 'severance':
1. Severance (2022) [TV] tmdb:95396 - Mark leads a team at Lumon whose memories are divided between work and personal lives (Status: available in library) Rating: 8.3/10 | RT: 97% certified_fresh | IMDB: 8.7 | Air date: S3E1 airs 2026-05-01 Trailer: https://youtube.com/watch?v=xxx
2. Severance (2006) [Movie] tmdb:11547 - A group of office workers go on a team-building weekend (Status: not in the library) Rating: 5.1/10
]
```
**Format per line:** `{i}. {title} ({year}) [{Movie|TV}] tmdb:{id} - {overview} (Status: {status_label}){rating}{air_date}{trailer}`
**Status comes from:** `types.py:status_label`
**Suggested:** No change.

## 3B. Server state format (recent)

**When:** User asked "what's new" and there's content on the server.
**Source:** `recent.py:30-54`
**Currently:**
```
[Server state (page 1):
Available on server:
1. The Bear (2022) [TV] tmdb:136315 - A chef returns to Chicago (Status: available)
2. Shogun (2024) [TV] tmdb:126308 - In feudal Japan (Status: available)
Requested:
1. Project Hail Mary (2026) [Movie] tmdb:823491 - A lone astronaut (Status: processing)
2. Hoppers (2026) [Movie] tmdb:456789 - A quantum physics adventure (Status: pending)
]
```
**BUG: Status comes from:** `seerr.py:649,669` — raw `MediaStatus.name.lower()` (e.g. "available", "processing", "pending"). `recent.py:40,51` passes these through as `item['status']` unchanged. Does NOT use `status_label`. This is why the LLM sees "processing" and says "downloading".
**Implementation note:** `seerr.py` returns plain dicts, not `SearchResult` objects, so it can't call `status_label` directly. Fix: extract the label mapping from `status_label` into a standalone function in `types.py` (e.g. `status_label_for(MediaStatus)`) that both `SearchResult.status_label` and `seerr.py` can use.
**Suggested:** Use labels from the STATUS LABELS table instead of raw enum names. Example corrected output:
```
[Server state (page 1):
Available on server:
1. The Bear (2022) [TV] tmdb:136315 - A chef returns to Chicago (Status: available in library)
2. Shogun (2024) [TV] tmdb:126308 - In feudal Japan (Status: available in library)
Requested:
1. Project Hail Mary (2026) [Movie] tmdb:823491 - A lone astronaut (Status: requested: waiting for release)
2. Hoppers (2026) [Movie] tmdb:456789 - A quantum physics adventure (Status: requested: waiting for admin approval)
]
```

## 3C. Dedup context strings

**When:** User tried to add something already on the server.
**Source:** `request.py:61-65`
**Currently (3 variants):**
```
[Request check: "Severance" is already available in library]
[Request check: "Severance" is already downloading]
[Request check: "Severance" is already requested, waiting on approval]
```
**BUG:** Two mismatches with the STATUS LABELS table:
- PROCESSING: code says `"already downloading"`, label says `"requested: waiting for release"`
- PENDING: code says `"already requested, waiting on approval"`, label says `"requested: waiting for admin approval"` (different wording + comma vs colon)
Hardcoded, doesn't use `status_label`.
**Note:** The "already" prefix is intentional context. The status portion after "already" should match the STATUS LABELS table.
**Suggested:** Use the dedup pattern from the STATUS LABELS table: `[Request check: "{title}" is already {label}]`

## 3D. Error/empty context strings

**When:** Various failure and edge cases. Each is a one-line context block.
**Source:** Various handlers — listed per string below.
**Currently:**
```
[Search for "severance": search failed]              # search.py:29
[Search for "asdfghjkl": no results found]           # search.py:36
[Recommendations: no search criteria provided]       # recommend.py:134
[Recommendations for "sci-fi 2030": no results found]  # recommend.py:163
[Similar to "Xyzzy": couldn't find the base title]   # recommend.py:217
[Similar to "Severance": no recommendations found]   # recommend.py:234
[Recommendations similar to Severance]               # recommend.py:245 (success header, not an error)
[Server state: no available or requested items found] # recent.py:27
[Remember action: no fact was provided]              # memory.py:17
[Forget action: no keyword was provided]             # memory.py:32
[Reply action: LLM returned empty message]           # __init__.py:336
```
**Note:** `[Recommendations similar to Severance]` is not an error — it's a success header injected before results in the similar-to flow. Grouped here because it's a context string, not a result format.
**Suggested:** No change.

---

# PART 4: Call 2 Instructions

These are appended at the very end of the prompt as `[INSTRUCTION: ...]` to tell the LLM how to format its response. This is the part that's most broken — one confusing blob repeated everywhere.

**Implementation note:** Currently 4A/4B/4C share one code path (intent="search"), 4D/4E/4F/4G/4H/4I share another (intent="recommend"), 4J/4K share one (intent="recent"), 4L has its own (intent="dedup"), and 4M/4N share one (intent=None). Five distinct intents total. Giving each scenario its own instruction means splitting these code paths — the handler needs to pass a more specific intent string so `_llm_respond` can pick the right instruction.

**What `multiple_results` controls:** This field determines poster behavior downstream. `true` = posters get numbered overlays and are sent as a gallery. `false` = a single poster is sent for the focused title. It does NOT affect the text response — only images.

## 4A. Search — results found

**When:** User asked about a specific title and Seerr returned results. Also used when the request handler needs to re-search.
**Context available:** 3A (search results with statuses, ratings, trailers, air dates)
**Source:** `__init__.py:182-189`
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present the search results to the user. If there's one clear match for what the user asked, focus on it — describe it, mention ratings, and note its status. If there are multiple plausible matches, present them numbered and ask which one. Use your judgment based on the query and results.
```
**Suggested:**
```
Do NOT use action=search or action=recommend. The results are already fetched. Use action=reply to present them. If the user is confirming they want a title, use action=request with the tmdb_id and media_type from the results. One clear match: focus on it — describe, mention ratings, and state its status exactly as shown. Multiple plausible matches: number them and ask which one. Set multiple_results=true when presenting numbered options, false when focusing on one title. Use the exact status wording from the results.
```

## 4B. Search — API error

**When:** Seerr API call failed.
**Context available:** 3D (`[Search for "X": search failed]`)
**Source:** `__init__.py:182-189` (same code path as 4A)
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present the search results to the user. If there's one clear match for what the user asked, focus on it — describe it, mention ratings, and note its status. If there are multiple plausible matches, present them numbered and ask which one. Use your judgment based on the query and results.
```
**Suggested:**
```
Use action=reply. The search failed due to a server error. Tell the user you couldn't look that up right now and to try again in a moment.
```

## 4C. Search — no results

**When:** Seerr returned zero results.
**Context available:** 3D (`[Search for "X": no results found]`)
**Source:** `__init__.py:182-189` (same code path as 4A)
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present the search results to the user. If there's one clear match for what the user asked, focus on it — describe it, mention ratings, and note its status. If there are multiple plausible matches, present them numbered and ask which one. Use your judgment based on the query and results.
```
**Suggested:**
```
Use action=reply. Nothing matched that search. Tell the user and suggest trying a different name or spelling.
```

## 4D. Recommend — results found

**When:** User asked for recommendations (genre, trending, keyword, etc.) and Seerr returned results.
**Context available:** 3A (search results formatted as recommendations)
**Source:** `__init__.py:190-195`
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present these as numbered picks for the user to browse. Mention ALL results shown with brief descriptions. End with asking if they want to add any.
```
**Suggested:**
```
Do NOT use action=search or action=recommend. Use action=reply. Set multiple_results=true. Present ALL results as numbered picks — do not skip any. Include a brief description and rating for each. When a result has a status, use the exact status wording from the results — do not rephrase or interpret it. Ask if they want to add any. If the user is confirming a title, use action=request with the tmdb_id and media_type from the results.
```

## 4E. Recommend — no search criteria

**When:** LLM said action=recommend but didn't provide any structured fields (genre, keyword, etc.)
**Context available:** 3D (`[Recommendations: no search criteria provided]`)
**Source:** `__init__.py:190-195` (same code path as 4D)
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present these as numbered picks for the user to browse. Mention ALL results shown with brief descriptions. End with asking if they want to add any.
```
**Suggested:**
```
Use action=reply. The recommendation search couldn't run because no criteria were provided. Ask the user what they're in the mood for — a genre, a type (movie or show), something similar to a title they like, etc.
```

## 4F. Recommend — no results

**When:** Seerr returned zero results for the recommendation query.
**Context available:** 3D (`[Recommendations for "X": no results found]`)
**Source:** `__init__.py:190-195` (same code path as 4D)
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present these as numbered picks for the user to browse. Mention ALL results shown with brief descriptions. End with asking if they want to add any.
```
**Suggested:**
```
Use action=reply. No recommendations matched those criteria. Tell the user nothing came up and suggest trying a broader genre, different keywords, or a wider year range.
```

## 4G. Similar-to — results found

**When:** User asked for "something like Severance" and Seerr returned similar titles.
**Context available:** 3D (`[Recommendations similar to Severance]`) + 3A (results)
**Source:** `__init__.py:190-195` (same code path as 4D)
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present these as numbered picks for the user to browse. Mention ALL results shown with brief descriptions. End with asking if they want to add any.
```
**Suggested:**
```
Do NOT use action=search or action=recommend. Use action=reply. Set multiple_results=true. Present ALL results as numbered picks — frame them as similar to the title the user mentioned. Include a brief description and rating for each. Use the exact status wording from the results. Ask if they want to add any. If the user is confirming a title, use action=request with the tmdb_id and media_type from the results.
```

## 4H. Similar-to — base title not found

**When:** User asked "something like Xyzzy" but Seerr couldn't find "Xyzzy".
**Context available:** 3D (`[Similar to "Xyzzy": couldn't find the base title]`)
**Source:** `__init__.py:190-195` (same code path as 4D)
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present these as numbered picks for the user to browse. Mention ALL results shown with brief descriptions. End with asking if they want to add any.
```
**Suggested:**
```
Use action=reply. Couldn't find the title the user mentioned, so there's nothing to base recommendations on. Tell them the title wasn't found and ask them to double-check the name or try a different one.
```

## 4I. Similar-to — no recommendations

**When:** Found the base title but Seerr had no similar titles.
**Context available:** 3D (`[Similar to "Severance": no recommendations found]`)
**Source:** `__init__.py:190-195` (same code path as 4D)
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present these as numbered picks for the user to browse. Mention ALL results shown with brief descriptions. End with asking if they want to add any.
```
**Suggested:**
```
Use action=reply. No similar titles were found for that one. Tell the user and suggest they try asking by genre or keyword instead.
```

## 4J. Recent — results found

**When:** User asked "what's new" and there's content on the server.
**Context available:** 3B (server state with available + requested sections)
**Source:** `__init__.py:196-200`
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present the server state. Group by what's available and what's been requested. Use the exact status from the results. Include brief descriptions.
```
**Suggested:**
```
Do NOT use action=search or action=recommend. Use action=reply. Set multiple_results=true. Present the server state to the user. Mention what's available and what's been requested. Use the exact status wording from the results — do not rephrase or interpret status labels. Include brief descriptions.
```

## 4K. Recent — nothing on server

**When:** No available or requested content.
**Context available:** 3D (`[Server state: no available or requested items found]`)
**Source:** `__init__.py:196-200` (same code path as 4J)
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Present the server state. Group by what's available and what's been requested. Use the exact status from the results. Include brief descriptions.
```
**Suggested:**
```
Use action=reply. Nothing is on the server right now — no available content and no pending requests. Tell the user.
```

## 4L. Dedup — already on server

**When:** User tried to add a title that's already available, downloading, or pending.
**Context available:** 3C (one of the three dedup strings). Each means something different to the user:
- "available in library" = good news, they can watch it now
- "requested: waiting for release" = they already asked, it's not out yet
- "requested: waiting for admin approval" = it's queued, waiting on admin
**Source:** `__init__.py:201-205`
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. The user wanted to add this title but it's already on the server. Inform them of its current status naturally.
```
**Suggested:**
```
Use action=reply. Do NOT use action=request. The user wanted to add this title but it's already on the server. Tell them its current status using the exact wording from the context — do not rephrase.
```

## 4M. Remember/forget — missing fact

**When:** LLM said remember or forget but didn't provide the `fact` field.
**Context available:** 3D (`[Remember action: no fact was provided]` or `[Forget action: no keyword was provided]`)
**Source:** `__init__.py:206-207`
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Write a reply message summarizing the results.
```
**Suggested:**
```
Use action=reply. The user wanted you to remember or forget something, but it wasn't clear what. Ask them to say specifically what they'd like you to remember or forget.
```

## 4N. Empty reply fallback

**When:** Call 1 returned action=reply but the message was empty. We re-call the LLM.
**Context available:** 3D (`[Reply action: LLM returned empty message]`)
**Source:** `__init__.py:206-207` (same code path as 4M)
**Currently:**
```
The results above have already been fetched. Do NOT search, recommend, or take any action. Use action=reply. If the user is confirming they want to add a title shown in the results, you may use action=request with the correct tmdb_id and media_type. Set multiple_results=true if you are presenting multiple numbered options, false if focusing on a single title. Write a reply message summarizing the results.
```
**Suggested:**
```
Use action=reply. Respond to the user's current message based on the conversation history. You must provide a non-empty message.
```

---

# PART 5: Compression Prompts

These run on a schedule (not during user messages). They summarize conversation history into the user's memory file.

## 5A. Daily compression

**When:** Once per day after the morning digest. Summarizes yesterday's chat.db messages.
**System prompt:** 1B ("You are a helpful assistant that summarizes conversations.")
**Source:** `compression.py:144-158`
**JSON schema fields returned:** summary, suggested_preferences, genres, avoid_genres, liked_movies, liked_shows, avoid_titles
**Currently:**
```
Summarize this day's iMessage conversation between a user and a media bot.

Rules:
- Always mention specific title names and outcomes (e.g. 'Requested Severance S3, added successfully')
- Keep it to 1-3 sentences. Skip conversation mechanics ('user asked', 'bot responded').
- Extract genres the user showed interest in for the genres array.
- Extract genres the user said they dislike or want to avoid for avoid_genres. Include reason in [brackets] if stated.
- Extract specific movies/shows the user requested or asked about for liked_movies/liked_shows. Include year.
- Extract titles the user rejected or disliked for avoid_titles. Include reason in [brackets] if stated.
- Only suggest preferences for genuine repeated patterns, not one-off requests.

Already known preferences (do NOT re-suggest):
{existing preferences from memory file}

Already known tastes (do NOT re-add):
{existing taste profile from memory file}

Recent summaries (for context, don't repeat):
{last few daily summaries}

Conversation:
User: recommend horror movies
Bot: Here are some picks: 1) Sinners (2025)...
User: add sinners
Bot: Added! It'll be ready soon.
```
**Suggested:** No change.

## 5B. Weekly compression

**When:** Rolls up daily summaries older than 7 days into a weekly summary.
**System prompt:** 1B ("You are a helpful assistant that summarizes conversations.")
**Source:** `compression.py:235-239`
**JSON schema fields returned:** summary only
**Currently:**
```
Combine these daily conversation summaries into one weekly summary. 1-2 sentences. Preserve specific title names. Focus on patterns and key events.

Daily summaries:
{list of daily summary entries being rolled up}
```
**Suggested:** No change.

## 5C. Monthly compression

**When:** Rolls up weekly summaries older than 4 weeks into a monthly summary.
**System prompt:** 1B ("You are a helpful assistant that summarizes conversations.")
**Source:** `compression.py:299-304`
**JSON schema fields returned:** summary only
**Currently:**
```
Combine these weekly conversation summaries into one monthly summary. 1-2 sentences. Preserve specific title names where notable. Focus on big-picture patterns and key events.

Weekly summaries:
{list of weekly summary entries being rolled up}
```
**Suggested:** No change.

---

# PART 6: Status Labels

Moved to the top of the document as the single source of truth. All 4 places (`types.py`, `seerr.py`, `request.py`, `_base.py`) will be consolidated to reference one shared dict in `prompts.py`.

---

# PART 7: Python Fallback Text

**Decision: Remove all Python fallback formatters.** When both LLM models fail, just send a generic error message instead of trying to format results in Python. The Python-formatted messages are confusing and inconsistent with the LLM's tone.

**Currently:** `format_single_result()`, `format_multiple_results()`, `format_recommendations()` in `_base.py` build plain-text responses as a last resort. There are also two hardcoded error strings sent directly to users when the LLM call itself fails:
- `_base.py:11`: `ERROR_GENERIC = "Server error, please try again later."` (used as `fallback=` default)
- `__init__.py:139`: `"Server error, please try again later."` (returned when call 1 throws an exception)

**Suggested:** Delete all three fallback formatters. Consolidate both error strings into one constant in `prompts.py`:

```python
# In prompts.py
ERROR_GENERIC = "Something went wrong, try again in a sec."
```

**Files affected:**
- `_base.py`: delete `format_single_result`, `format_multiple_results`, `format_recommendations`, and their helpers
- `search.py`: remove `fallback=` from `_send_with_poster` / `_llm_respond` calls
- `recommend.py`: remove `fallback=` from `_send_with_poster` / `_llm_respond` calls
- `recent.py`: remove `fallback=` from `_llm_respond` calls
- `__init__.py`: `_llm_respond` uses `ERROR_GENERIC` when LLM fails instead of caller-provided fallback

---

# PART 8: JSON Schemas + XML Tags

Schemas are passed via `--json-schema` and enforce the structure of what the LLM returns. XML tags wrap content in the prompt and form the contract that `instructions.md` references. Both go in `schemas.py`.

## 8X. XML wrapper tags

**When:** Every prompt. These wrap the different types of content so the LLM can distinguish history from context from memory.
**Source:** `__init__.py:170,236,241,286`
**Referenced by:** `instructions.md` ("Focus on the `<current_user_message>` tag", "Messages tagged `<user>` and `<assistant>` are conversation history")
**Currently:**
```
<context>...</context>     — wraps time, search results, API results, error messages
<memory>...</memory>       — wraps the user's stored preferences/facts
<user>...</user>           — wraps user messages from chat history
<assistant>...</assistant> — wraps bot messages from chat history
<current_user_message>...</current_user_message> — wraps the current message (inside 2C delimiter)
```
**Suggested:** No change to tag names. Move to `schemas.py` as constants so they're defined alongside the structural contracts they support.

---

The schemas below enforce the structure of what the LLM returns. Currently in `types.py` (call 1 and 2) and `compression.py` (daily compression).

## 8A. Call 1 schema (action routing)

**When:** Every call 1 — the LLM picks an action and fills in relevant fields.
**Source:** `types.py:127-153` (`LLM_JSON_SCHEMA`)
**Currently:**
```json
{
    "action": "search | request | recent | recommend | remember | forget | reply",
    "query": "string",
    "tmdb_id": "integer",
    "media_type": "movie | tv",
    "message": "string",
    "fact": "string",
    "genre": "string",
    "keyword": "string",
    "year": "integer",
    "year_end": "integer",
    "similar_to": "string",
    "trending": "boolean",
    "count": "integer",
    "page": "integer"
}
// required: action, message
// additionalProperties: false
```
**Suggested:** No change.

## 8B. Call 2 schema (respond)

**When:** Every call 2 — restricted to reply or request (follow-up "add it").
**Source:** `types.py:156-170` (`LLM_RESPOND_SCHEMA`)
**Note:** This is the structural enforcement behind "Do NOT search or recommend" — even if the LLM tries, the schema rejects it. The instructions are belt-and-suspenders on top.
**Currently:**
```json
{
    "action": "reply | request",
    "tmdb_id": "integer",
    "media_type": "movie | tv",
    "message": "string",
    "multiple_results": "boolean"
}
// required: action, message
// additionalProperties: false
```
**Suggested:** No change.

## 8C. Daily compression schema

**When:** Daily compression call — returns a summary plus structured taste extraction.
**Source:** `compression.py:25-65` (`COMPRESSION_SCHEMA`)
**Currently:**
```json
{
    "summary": "string — 1-3 sentence summary with specific title names and outcomes",
    "suggested_preferences": "string[] — genuine repeated patterns to add as preferences",
    "genres": "string[] — genres the user showed interest in (e.g. 'horror', 'sci-fi')",
    "avoid_genres": "string[] — genres to avoid, with reason in brackets (e.g. 'reality TV [finds it trashy]')",
    "liked_movies": "string[] — movies requested or interested in, with year (e.g. 'Sinners (2025)')",
    "liked_shows": "string[] — TV shows requested or interested in, with year (e.g. 'Severance (2022)')",
    "avoid_titles": "string[] — titles rejected/disliked, with year and reason (e.g. 'The Monkey (2025) [too campy]')"
}
// required: all fields
// additionalProperties: false
```
**Suggested:** No change.

## 8D. Weekly/monthly compression schema

**When:** Weekly and monthly rollup calls.
**Source:** `compression.py:243-248, 307-312` (inline)
**Currently:**
```json
{
    "summary": "string"
}
// required: summary
// additionalProperties: false
```
**Suggested:** No change.

---

# Implementation: `prompts.py` + `schemas.py`

After review, all LLM-facing text becomes `src/bluepopcorn/prompts.py` and all JSON schemas become `src/bluepopcorn/schemas.py`. The rest of the codebase imports from them — no prompt text or schema dicts live anywhere else (except `personality.md` and `instructions.md` which stay as files since they're the full system prompt).

```python
"""All LLM-facing text in one place. Edit here, changes propagate everywhere."""

from .types import MediaStatus

# ─── Status labels (single source of truth) ──────────────────────
# Used in: search/recommend context, recent context, dedup context, Python fallback
STATUS_LABELS: dict[MediaStatus, str] = {
    MediaStatus.AVAILABLE: "available in library",
    MediaStatus.PARTIALLY_AVAILABLE: "partially available in library",
    MediaStatus.PROCESSING: "requested: waiting for release",
    MediaStatus.PENDING: "requested: waiting for admin approval",
    MediaStatus.UNKNOWN: "not in the library",
    MediaStatus.NOT_TRACKED: "not in the library",
    MediaStatus.BLOCKLISTED: "blocked/unable to download",
    MediaStatus.DELETED: "not in the library",
}
DOWNLOADING_LABEL = "currently downloading ({progress})"  # PROCESSING + active download


# ─── Prompt framing (injected into every prompt) ─────────────────
TIME_CONTEXT = "<context>[Current time: {time}]</context>"
CONVERSATION_GAP = (
    "[The above messages are from a previous conversation. "
    "Treat the following as a new, separate conversation "
    "— do not assume topic continuity.]"
)
CURRENT_MESSAGE_DELIMITER = (
    "---\n"
    "[CURRENT MESSAGE — respond to this. Everything above is history for context only.]\n"
    "<current_user_message>{text}</current_user_message>"
)
LAST_DISCUSSED_TITLE = "[Last discussed title: {title} tmdb:{tmdb_id} {media_type}]"


# ─── Context block templates ─────────────────────────────────────
# Search/recommend results: built by format_search_results(), uses STATUS_LABELS
# Recent/server state: built by recent handler, uses STATUS_LABELS

CONTEXT_SEARCH_FAILED = '[Search for "{query}": search failed]'
CONTEXT_SEARCH_EMPTY = '[Search for "{query}": no results found]'
CONTEXT_RECOMMEND_NO_CRITERIA = "[Recommendations: no search criteria provided]"
CONTEXT_RECOMMEND_EMPTY = '[Recommendations for "{label}": no results found]'
CONTEXT_SIMILAR_HEADER = "[Recommendations similar to {title}]"
CONTEXT_SIMILAR_NOT_FOUND = '[Similar to "{title}": couldn\'t find the base title]'
CONTEXT_SIMILAR_EMPTY = '[Similar to "{title}": no recommendations found]'
CONTEXT_RECENT_EMPTY = "[Server state: no available or requested items found]"
CONTEXT_DEDUP = '[Request check: "{title}" is already {status}]'
CONTEXT_REMEMBER_MISSING = "[Remember action: no fact was provided]"
CONTEXT_FORGET_MISSING = "[Forget action: no keyword was provided]"
CONTEXT_EMPTY_REPLY = "[Reply action: LLM returned empty message]"


# ─── Call 2 instructions (one per scenario) ──────────────────────
# Each is the full [INSTRUCTION: ...] text appended after context.
# Keyed by specific scenario, not by shared intent.

INSTRUCTION = {
    "search_results": "...",
    "search_error": "...",
    "search_empty": "...",
    "recommend_results": "...",
    "recommend_no_criteria": "...",
    "recommend_empty": "...",
    "similar_results": "...",
    "similar_not_found": "...",
    "similar_empty": "...",
    "recent_results": "...",
    "recent_empty": "...",
    "dedup": "...",
    "remember_missing": "...",
    "empty_reply": "...",
}


# ─── Compression prompts ─────────────────────────────────────────
# ─── Error messages (sent directly to user) ──────────────────────
# Used when: LLM call fails, API errors, action failures
ERROR_GENERIC = "Something went wrong, try again in a sec."  # replaces current "Server error, please try again later."


# ─── Compression prompts ─────────────────────────────────────────
COMPRESSION_SYSTEM_PROMPT = "You are a helpful assistant that summarizes conversations."

COMPRESSION_DAILY = (
    "Summarize this day's iMessage conversation between a user and a media bot.\n\n"
    "Rules:\n"
    "- Always mention specific title names and outcomes ...\n"
    ...
)

COMPRESSION_WEEKLY = (
    "Combine these daily conversation summaries into one weekly summary. "
    "1-2 sentences. Preserve specific title names. "
    "Focus on patterns and key events.\n\n"
    "Daily summaries:\n{summaries}"
)

COMPRESSION_MONTHLY = (
    "Combine these weekly conversation summaries into one monthly summary. "
    "1-2 sentences. Preserve specific title names where notable. "
    "Focus on big-picture patterns and key events.\n\n"
    "Weekly summaries:\n{summaries}"
)
```

```python
# src/bluepopcorn/schemas.py
"""JSON schemas and XML tags for LLM communication. Edit here to change structure."""

# ─── XML wrapper tags ─────────────────────────────────────────────
# These wrap content types in the prompt. Referenced by instructions.md.
TAG_CONTEXT = "context"
TAG_MEMORY = "memory"
TAG_USER = "user"
TAG_ASSISTANT = "assistant"
TAG_CURRENT_MESSAGE = "current_user_message"

# ─── Call 1: action routing (all actions + all fields) ────────────
DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["search", "request", "recent", "recommend", "remember", "forget", "reply"],
        },
        "query": {"type": "string"},
        "tmdb_id": {"type": "integer"},
        "media_type": {"type": "string", "enum": ["movie", "tv"]},
        "message": {"type": "string"},
        "fact": {"type": "string"},
        "genre": {"type": "string"},
        "keyword": {"type": "string"},
        "year": {"type": "integer"},
        "year_end": {"type": "integer"},
        "similar_to": {"type": "string"},
        "trending": {"type": "boolean"},
        "count": {"type": "integer"},
        "page": {"type": "integer"},
    },
    "required": ["action", "message"],
    "additionalProperties": False,
}

# ─── Call 2: respond (restricted to reply or request follow-up) ───
RESPOND_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["reply", "request"],
        },
        "tmdb_id": {"type": "integer"},
        "media_type": {"type": "string", "enum": ["movie", "tv"]},
        "message": {"type": "string"},
        "multiple_results": {"type": "boolean"},
    },
    "required": ["action", "message"],
    "additionalProperties": False,
}

# ─── Daily compression (summary + taste extraction) ──────────────
COMPRESSION_DAILY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "1-3 sentence summary. Always include specific title names and outcomes.",
        },
        "suggested_preferences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genuine repeated patterns to add as preferences (empty if none)",
        },
        "genres": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genres the user showed interest in (e.g. 'horror', 'sci-fi', 'Korean drama')",
        },
        "avoid_genres": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genres the user said they dislike or want to avoid, with reason in brackets if known",
        },
        "liked_movies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Movies the user requested or expressed interest in, with year",
        },
        "liked_shows": {
            "type": "array",
            "items": {"type": "string"},
            "description": "TV shows the user requested or expressed interest in, with year",
        },
        "avoid_titles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Titles the user rejected or disliked, with year and reason in brackets if known",
        },
    },
    "required": ["summary", "suggested_preferences", "genres", "avoid_genres", "liked_movies", "liked_shows", "avoid_titles"],
    "additionalProperties": False,
}

# ─── Weekly/monthly compression (summary only) ───────────────────
COMPRESSION_ROLLUP_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
```

**Bug fixes included in this implementation:**

1. **Status labels in recent context (BUG 3B):** `seerr.py:649,669` uses `status.name.lower()` producing raw enum names (`"processing"`, `"pending"`) that `recent.py:40,51` passes through to the LLM. Fix: `seerr.py` imports `STATUS_LABELS` from `prompts.py` and uses `STATUS_LABELS[status]` instead of `status.name.lower()`.

2. **Dedup string inconsistency (BUG 3C):** `request.py:61-65` has hardcoded dedup strings that don't match STATUS LABELS — PROCESSING says `"already downloading"` (should be `"already requested: waiting for release"`), PENDING says `"already requested, waiting on approval"` (should be `"already requested: waiting for admin approval"`). Fix: `request.py` uses `CONTEXT_DEDUP.format(title=title, status=STATUS_LABELS[status])` instead of hardcoded strings.

3. **Last discussed title inconsistency (BUG 2D/2E):** `_last_topic["title"]` sometimes includes year (set by search/recommend/request as `f"{top.title} ({top.year})"`), sometimes doesn't (set by `recent.py:62` from seerr dicts, and by `_store_request_context` from `seerr_title()`). Also, the context marker prefix differs: `"Last discussed title"` (2D, `__init__.py:127`) vs `"Last discussed"` (2E, `__init__.py:450`). Fix: all `_last_topic` setters include year. `_store_request_context` uses `LAST_DISCUSSED_TITLE` template from `prompts.py` with year included. Unify both markers to `[Last discussed title: {title} ({year}) tmdb:{id} {media_type}]`.

**What changes in other files:**
- `types.py`: add `status_label_for(MediaStatus) -> str` standalone function using `STATUS_LABELS` from `prompts.py`. `status_label` property delegates to it. Remove `LLM_JSON_SCHEMA` and `LLM_RESPOND_SCHEMA` (moved to `schemas.py`)
- `seerr.py`: imports `status_label_for` from `types.py` for server state dicts instead of using `status.name.lower()` (fixes BUG 3B)
- `request.py`: imports `CONTEXT_DEDUP` template + `STATUS_LABELS` instead of hardcoded strings (fixes BUG 3C)
- `__init__.py`: imports `INSTRUCTION` dict, `LAST_DISCUSSED_TITLE`, `CONVERSATION_GAP`, etc. `_llm_respond` takes a scenario key instead of an intent string. `_store_request_context` uses `LAST_DISCUSSED_TITLE` with year (fixes BUG 2E)
- `recent.py`: `_last_topic` setter includes year from `item.get("year")` (fixes BUG 2D for recent)
- `_base.py`: delete `format_single_result`, `format_multiple_results`, `format_recommendations`. `ERROR_GENERIC` moves to `prompts.py`
- `compression.py`: imports `COMPRESSION_*` prompts from `prompts.py` and `COMPRESSION_DAILY_SCHEMA` / `COMPRESSION_ROLLUP_SCHEMA` from `schemas.py`. Remove inline `COMPRESSION_SCHEMA`
- `llm.py`: imports `DECIDE_SCHEMA` and `RESPOND_SCHEMA` from `schemas.py` instead of from `types.py`
- `search.py`, `recommend.py`, `recent.py`, `memory.py`: import `CONTEXT_*` templates, pass specific scenario keys to `_llm_respond`
