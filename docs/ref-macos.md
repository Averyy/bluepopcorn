# macOS iMessage Reference

Quick-reference for iMessage automation on macOS. Verified against the open-source ecosystem (March 2026).

## Core Facts

- **AppleScript sending:** `account`/`participant` pattern for Tahoe (26+), not old `service`/`buddy`. Synonyms still work but are deprecated. Confirmed by BlueBubbles version-aware scripts and Messages.sdef dictionary.
- **Typing indicator:** System Events keystroke into compose field via `imessage://` URL. Requires Accessibility. 10s script execution timeout; native indicator persists ~60s on recipient's end. Only 3 projects implement this (us, BlueBubbles Private API, CaptainATW).
- **chat.db dates:** Nanoseconds since 2001-01-01 (Core Foundation epoch) on macOS 10.13+. Plain seconds on older versions. imessage-exporter auto-detects: `if stamp >= 1_000_000_000_000` then nanoseconds, else seconds.
- **attributedBody:** Typedstream (NSArchiver format) fallback when `text` is NULL. NOT a binary plist. Extract via NSString marker + UTF-8 decode. Full deserialization requires a typedstream library (Rust: crabstep, JS: node-typedstream, Python: none mature).
- **launchd:** `BluePopcorn` Swift wrapper for macOS permission naming. FDA/Accessibility go to the compiled binary, not sshd-keygen-wrapper or Python.
- **Attachment sandbox:** Images must be in `~/Pictures/` for Messages.app to send them. imsg uses `~/Library/Messages/Attachments/imsg/` instead — both work, anything under `$HOME` that Messages.app can access.
- **Daemon reload:** `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist`

## Potential Improvements

Techniques from other projects that could improve BluePopcorn:

### Error dialog clearing (from CamHenlin/imessageclient)
AppleScript send failures can leave modal error dialogs in Messages.app that block ALL future sends. We retry with backoff but never clear the dialog. Add a dismiss-dialog step in the retry path. See `todo-futureimprovements.md` for full approach.

### Temp-file trick for AppleScript escaping (from imessage_tools)
Write message to temp file, then `read (POSIX file "...") as «class utf8»` in AppleScript. Message content never enters AppleScript string literals — eliminates all escaping edge cases. Use `tempfile.mkstemp()` for unique paths. See `todo-futureimprovements.md` for full approach.

### File-watcher on chat.db + WAL + SHM (from imsg)
Watch all 3 SQLite files (db, WAL, SHM) via kqueue VNODE events instead of polling. WAL fires on every INSERT/COMMIT; main db only fires during checkpoint. Python: raw `select.kqueue()` + `asyncio.add_reader()`, zero deps. BlueBubbles does NOT use file watching (correction). See `todo-futureimprovements.md` for full approach.

### ~~Dynamic column detection~~ (from imsg) — NOT NEEDED
imsg does `PRAGMA table_info(message)` on startup for cross-version compat. Not worth it for us — we target Tahoe 26 only, all columns we use have existed since 2016-2017, and Apple has never removed a column from chat.db (schema is append-only).

## Reference Repos

### iMessage bots / servers

| Project | Lang | Stars | URL | Notes |
|---|---|---|---|---|
| **BlueBubbles** | TS + ObjC | 870 | https://github.com/BlueBubblesApp/bluebubbles-server | Most feature-complete. AppleScript + Private API (dylib injection via MacForge). Version-aware `account`/`participant` vs `service`/`buddy`. Polls chat.db (not file-watcher). Full typedstream parsing via node-typedstream. Private API helper: https://github.com/BlueBubblesApp/bluebubbles-helper |
| **Jared** | Swift | 571 | https://github.com/ZekeSnider/Jared | Native macOS app. Plugin architecture, webhooks, REST API. AppleScript sending, ROWID polling. No attributedBody handling. Author confirmed private APIs didn't work. |
| **imsg** | Swift | 882 | https://github.com/steipete/imsg | Swift CLI. DispatchSource file-watcher on db+WAL+SHM. NSAppleScript (faster than shelling osascript). Binary marker matching for attributedBody. Dynamic column detection. Stages attachments to `~/Library/Messages/Attachments/imsg/`. |
| **CamHenlin** | JS | 877 | https://github.com/CamHenlin/imessageclient | Old `service`/`buddy` pattern. Key insight: AppleScript failures leave modal dialogs that block future sends. `.r` command clears them. |
| **CaptainATW** | Python | 7 | https://github.com/alextyhwang/iMessages-Chatbot-Server | Python aiosqlite, `account`/`participant` pattern. Most detailed typing indicator impl. 0.3s async debounce for rapid messages. |
| **Py-iMessenger** | Python | 50 | https://github.com/VarunPatelius/Py-iMessenger | Old `service`/`buddy` pattern. Broken on Ventura+ (no attributedBody handling). |
| **Barcelona/Beeper** | Swift | 70 | https://github.com/beeper/barcelona | Direct IMCore private framework. Full feature parity (reactions, effects, mentions, edits). Requires SIP modification — not practical for us. |
| **pypush** | Python | 3,700 | https://github.com/JJTech0130/pypush | Protocol-level APNs reimplementation. Runs without macOS. Not relevant to our approach. Acquired by Beeper. |
| **imsg-bridge** | Python | 0 | https://github.com/heyfinal/imsg-bridge | Wraps imsg CLI as REST+WebSocket. Bearer token in Keychain. ROWID state file for crash recovery. launchd with auto-restart. |

### chat.db tools / parsers

| Project | Lang | Stars | URL | Notes |
|---|---|---|---|---|
| **imessage-exporter** | Rust | 5,000 | https://github.com/ReagentX/imessage-exporter | Gold standard. Full typedstream parsing via crabstep. 37+ test data files for attributedBody variants. Nanosecond/second auto-detection. Handles every iMessage feature as of Tahoe 26.3. |
| **imessage_tools** | Python | 124 | https://github.com/my-other-github-account/imessage_tools | Python. NSString/NSNumber/NSDictionary string-splitting for attributedBody (fragile). Temp-file trick for AppleScript escaping. |
| **imessage_reader** | Python | 117 | https://github.com/niftycode/imessage_reader | Read-only forensic tool. Basic SQL schema reference. No attributedBody parsing. |

### Seerr/media bots (non-iMessage, conceptual inspiration)

| Project | Lang | Stars | URL | Notes |
|---|---|---|---|---|
| **Requestrr** | C# | 901 | https://github.com/thomst08/requestrr | Discord bot for Sonarr/Radarr/Overseerr. Web config portal. Siri integration. |
| **Doplarr** | Clojure | 569 | https://github.com/kiranshila/Doplarr | Discord bot for Overseerr. Slash commands + components. |
| **Plex Concierge** | — | 53 | https://github.com/UnderwaterOverground/Plex-Concierge | ChatGPT Custom GPT + Overseerr OpenAPI schema. No code, just GPT instructions + YAML. Conceptual inspiration only. |
| **SuggestArr** | Python + Vue | 1,100 | https://github.com/giuseppe99barchetta/SuggestArr | Auto-recommendation engine. Gets watch history from Jellyfin/Plex/Emby, finds similar via TMDB, auto-requests in Seerr. AI-powered natural language to TMDB filters. |

### MCP servers (for reference)

| Project | Lang | Stars | URL |
|---|---|---|---|
| **imessage-mcp** | Deno/TS | 21 | https://github.com/wyattjoh/imessage-mcp |
| **imessage-mcp-server** | Node | 25 | https://github.com/marissamarym/imessage-mcp-server |
| **imessage-query-fastmcp** | Python | 77 | https://github.com/hannesrudolph/imessage-query-fastmcp-mcp-server |
