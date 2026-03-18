---
name: reload
description: Reload the BluePopcorn daemon after code changes
disable-model-invocation: true
---

Reload the BluePopcorn launchd daemon:

1. Run: `launchctl unload ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist`
2. Wait 2 seconds
3. Kill any process on port 8095: `lsof -ti:8095 | xargs kill 2>/dev/null`
4. Wait 1 second
5. Run: `launchctl load ~/Library/LaunchAgents/com.bluepopcorn.daemon.plist`
6. Wait 3 seconds
7. Show the last 10 lines of `bluepopcorn.log` to verify startup
