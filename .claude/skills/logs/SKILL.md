---
name: logs
description: Show recent daemon logs
disable-model-invocation: true
argument-hint: "[lines]"
---

Show recent BluePopcorn daemon logs.

1. Run: `tail -${ARGUMENTS:-30} bluepopcorn.log`
2. If the user specified a number, use that instead of the default 30 lines
3. Highlight any ERROR or WARNING lines in your response
