#!/bin/bash
PLIST=~/Library/LaunchAgents/com.bluepopcorn.daemon.plist
launchctl bootout gui/$(id -u) "$PLIST" 2>/dev/null
launchctl bootstrap gui/$(id -u) "$PLIST"
echo "Daemon restarted."
