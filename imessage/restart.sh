#!/bin/bash
PLIST=~/Library/LaunchAgents/com.bluepopcorn.daemon.plist
LABEL=com.bluepopcorn.daemon

launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null

# Wait for the old instance to fully exit (graceful shutdown awaits
# in-flight sends) — bootstrapping immediately can briefly run two
# daemons polling the same last_rowid file.
for _ in $(seq 1 60); do
    launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
done

if launchctl bootstrap gui/$(id -u) "$PLIST"; then
    echo "Daemon restarted."
else
    echo "ERROR: bootstrap failed — daemon is NOT running." >&2
    exit 1
fi
