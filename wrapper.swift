import Foundation

let task = Process()
task.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/uv")
task.arguments = ["run", "-m", "imessagarr"]
task.currentDirectoryURL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
task.environment = ProcessInfo.processInfo.environment

signal(SIGINT) { _ in exit(0) }
signal(SIGTERM) { _ in exit(0) }

do {
    try task.run()
    task.waitUntilExit()
    exit(task.terminationStatus)
} catch {
    fputs("Failed to launch: \(error)\n", stderr)
    exit(1)
}
