import Foundation

// Resolve project root from binary location:
// 4 path components: iMessagarr → MacOS → Contents → iMessagarr.app
let binary = URL(fileURLWithPath: ProcessInfo.processInfo.arguments[0]).resolvingSymlinksInPath()
let projectRoot = binary.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

let task = Process()
task.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/uv")
task.arguments = ["run", "-m", "imessagarr"]
task.currentDirectoryURL = projectRoot
task.environment = ProcessInfo.processInfo.environment

// Forward signals to child process instead of orphaning it
let sigintSrc = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
let sigtermSrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)

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
