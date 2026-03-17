# App Bundle + macOS Hardening

## The Problem

macOS Sequoia+ blocks local network connections from ad-hoc signed binaries (like uv's Python 3.12). This causes `errno 65 No route to host` when Python tries to connect to Seerr at 192.168.2.15:5055. Curl and system Python work fine because they're system binaries (exempt).

The daemon currently works because the Swift wrapper is the "responsible code" and child processes (uv -> Python) inherit its permission via launchd. But macOS can't properly track this permission without a bundle ID, making it fragile across reboots.

## The Fix

Wrap the existing Swift binary in a `.app` bundle. This gives macOS a bundle ID to track all permissions (Local Network, Full Disk Access, Automation) permanently — just like any real app. Also harden the wrapper and plist to behave like a proper macOS daemon.

**Nothing changes about how the bot works.** The bundle is just a folder structure around the same binary. Python code changes don't require rebuilding the bundle.

## Steps

### 1. Create the app bundle folder structure

```bash
mkdir -p /Users/avery/Code/imessagarr/iMessagarr.app/Contents/MacOS
```

### 2. Create Info.plist

```bash
cat > /Users/avery/Code/imessagarr/iMessagarr.app/Contents/Info.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>com.imessagarr.daemon</string>
    <key>CFBundleName</key>
    <string>iMessagarr</string>
    <key>CFBundleExecutable</key>
    <string>iMessagarr</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>CFBundleShortVersionString</key>
    <string>0.1.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSLocalNetworkUsageDescription</key>
    <string>iMessagarr needs local network access to communicate with Seerr for media requests.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>iMessagarr needs to control Messages.app to send iMessages and show typing indicators.</string>
</dict>
</plist>
EOF
```

Notes on the plist keys:
- `LSUIElement` — hides from Dock (background agent)
- `NSLocalNetworkUsageDescription` — triggers the Local Network permission prompt
- `NSAppleEventsUsageDescription` — triggers the Automation permission prompt when the bot sends AppleScript commands to Messages.app and System Events. Without this, macOS shows a generic prompt
- `CFBundleVersion`/`CFBundleShortVersionString` — some macOS APIs expect these to exist. Keeps version in sync with pyproject.toml

### 3. Rewrite wrapper.swift

Update `wrapper.swift` with three fixes:

**a) Signal forwarding via DispatchSource** — the current `signal()` closures can't safely capture `task` and call `exit(0)` without giving the child a chance to shut down. Use `DispatchSource` instead:

```swift
import Foundation

let task = Process()
task.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/uv")
task.arguments = ["run", "-m", "imessagarr"]
task.environment = ProcessInfo.processInfo.environment

// Resolve working directory from bundle location so the wrapper works
// regardless of how it's launched (launchd, manual, etc.)
let execPath = URL(fileURLWithPath: ProcessInfo.processInfo.arguments[0])
let bundleDir = execPath
    .deletingLastPathComponent()   // MacOS/
    .deletingLastPathComponent()   // Contents/
    .deletingLastPathComponent()   // iMessagarr.app/
task.currentDirectoryURL = bundleDir

// Forward signals to child process — DispatchSource is safe unlike signal()
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let sigintSrc = DispatchSource.makeSignalSource(signal: SIGINT)
let sigtermSrc = DispatchSource.makeSignalSource(signal: SIGTERM)
sigintSrc.setEventHandler { task.terminate() }
sigtermSrc.setEventHandler { task.terminate() }
sigintSrc.resume()
sigtermSrc.resume()

do {
    try task.run()
    task.waitUntilExit()
    exit(task.terminationStatus)
} catch {
    fputs("Failed to launch: \(error)\n", stderr)
    exit(1)
}
```

**b) Bundle-relative working directory** — instead of relying on `FileManager.default.currentDirectoryPath` (which depends on launchd's `WorkingDirectory`), the wrapper resolves the project root from its own location inside the bundle. Works correctly whether launched by launchd, manually, or via `open`.

**c) Clean child termination** — `task.terminate()` sends SIGTERM to the Python process, which already handles it gracefully. `waitUntilExit()` then returns naturally with the child's exit status. No orphaned processes.

### 4. Compile Swift wrapper into the bundle

```bash
cd /Users/avery/Code/imessagarr
swiftc -o iMessagarr.app/Contents/MacOS/iMessagarr wrapper.swift
```

### 5. Sign the bundle

```bash
codesign --force --sign - iMessagarr.app
```

### 6. Make log path configurable

The Python `RotatingFileHandler` path is hardcoded as a relative `"imessagarr.log"`. Make it a config setting so it's explicit and works regardless of how the process is launched.

**a) Add `log_path` to config.toml:**

```toml
[paths]
log_path = "imessagarr.log"
```

**b) Update Settings and setup_logging in Python:**

Add `log_path` to the `Settings` dataclass and `load_settings()`. Update `setup_logging()` to use `settings.resolve_path(settings.log_path)` instead of the hardcoded string. Ensure the parent directory is created at startup (`Path(log_path).parent.mkdir(parents=True, exist_ok=True)`).

### 7. Update the launchd plist

Update both `~/Library/LaunchAgents/com.imessagarr.daemon.plist` and the example in the repo. Changes:

**a) Binary path** — point to the bundle executable:
```xml
<key>ProgramArguments</key>
<array>
    <string>/Users/avery/Code/imessagarr/iMessagarr.app/Contents/MacOS/iMessagarr</string>
</array>
```

**b) Process type** — tell macOS this is a background daemon for proper CPU/IO scheduling:
```xml
<key>ProcessType</key>
<string>Background</string>
```

**c) Exit timeout** — give the process 30 seconds to shut down gracefully before launchd kills it (the Python side handles SIGTERM cleanly):
```xml
<key>ExitTimeOut</key>
<integer>30</integer>
```

**d) WorkingDirectory** — can be removed since the wrapper now resolves its own working directory from the bundle path. Keeping it is harmless but redundant.

### 8. Reload the daemon

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.imessagarr.daemon.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.imessagarr.daemon.plist
```

macOS should show permission prompts for:
- "iMessagarr wants to access devices on your local network" — Allow
- "iMessagarr wants to control Messages.app" — Allow (on first AppleScript call)

### 9. Verify

```bash
launchctl list | grep imessagarr                    # should show PID
tail -20 imessagarr.log                              # should show "Seerr authentication successful"
```

Send a test iMessage to confirm the bot responds.

## Ongoing Maintenance

- **Python code changes:** Nothing to do. The bundle just runs `uv run -m imessagarr`.
- **wrapper.swift changes:** Recompile into the bundle: `swiftc -o iMessagarr.app/Contents/MacOS/iMessagarr wrapper.swift && codesign --force --sign - iMessagarr.app`
- **After recompiling:** Reload daemon: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.imessagarr.daemon.plist && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.imessagarr.daemon.plist`

## .gitignore

Add the compiled binary (not the plist/folder structure):

```
iMessagarr.app/Contents/MacOS/iMessagarr
```

## Why This Works

Apple TN3179: macOS requires a bundle with `NSLocalNetworkUsageDescription` to present and persist the Local Network permission prompt. The bundle ID (`com.imessagarr.daemon`) lets macOS track all permissions (Local Network, Full Disk Access, Automation) permanently. All child processes (uv, Python) inherit the bundle's permissions via the "responsible code" chain.
