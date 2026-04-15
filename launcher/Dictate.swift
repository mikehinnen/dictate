// Dictate.swift -- Native launcher for Dictate.app.
//
// Why not a shell script? macOS TCC tracks the bundle identity through the
// process chain. As soon as a shell script `exec`s the final binary, the
// bundle identity is lost and TCC lists that binary's name (e.g. "uv")
// instead of the app. A native binary as Bundle-Executable keeps the
// identity, and children inherit it via posix_spawn (Process) -- so
// "Dictate" shows up cleanly in the Permissions panes.
//
// CLI modes (invoked by dictate.py to manage the login item -- SMAppService
// requires Bundle.main context, which only this binary has):
//   --login-item-status        prints "enabled" / "disabled" / ... to stdout
//   --register-login-item      registers the app as a login item
//   --unregister-login-item    removes it again
// Without arguments, the normal launcher logic runs (spawns Python).
//
// Build:
//   bash launcher/build.sh
// (compiles to Dictate.app/Contents/MacOS/Dictate)

import Foundation
import ServiceManagement

// ---------------------------------------------------------------------------
// stderr helper
// ---------------------------------------------------------------------------

func writeStderr(_ s: String) {
    let line = s.hasSuffix("\n") ? s : s + "\n"
    if let data = line.data(using: .utf8) {
        FileHandle.standardError.write(data)
    }
}

// ---------------------------------------------------------------------------
// CLI modes: login item via SMAppService
// ---------------------------------------------------------------------------

@available(macOS 13.0, *)
func loginItemStatusName() -> String {
    switch SMAppService.mainApp.status {
    case .notRegistered: return "disabled"
    case .enabled:       return "enabled"
    case .requiresApproval: return "requires-approval"
    case .notFound:      return "not-found"
    @unknown default:    return "unknown"
    }
}

@available(macOS 13.0, *)
func registerLoginItem(_ register: Bool) -> Int32 {
    do {
        if register {
            try SMAppService.mainApp.register()
        } else {
            try SMAppService.mainApp.unregister()
        }
        return 0
    } catch {
        writeStderr("SMAppService error: \(error.localizedDescription)")
        return 1
    }
}

let args = CommandLine.arguments
if args.count > 1 {
    let flag = args[1]
    if #available(macOS 13.0, *) {
        switch flag {
        case "--login-item-status":
            print(loginItemStatusName())
            exit(0)
        case "--register-login-item":
            exit(registerLoginItem(true))
        case "--unregister-login-item":
            exit(registerLoginItem(false))
        default:
            // Unknown flag -> fall through to the normal launcher logic.
            break
        }
    } else {
        switch flag {
        case "--login-item-status",
             "--register-login-item",
             "--unregister-login-item":
            writeStderr("Login item management requires macOS 13+.")
            exit(2)
        default:
            break
        }
    }
}

// ---------------------------------------------------------------------------
// Normal launcher mode: spawn the Python child
// ---------------------------------------------------------------------------

// Logging
let logDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Logs")
try? FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
let logURL = logDir.appendingPathComponent("Dictate.log")
if !FileManager.default.fileExists(atPath: logURL.path) {
    FileManager.default.createFile(atPath: logURL.path, contents: nil)
}

func log(_ message: String) {
    guard let handle = try? FileHandle(forWritingTo: logURL) else { return }
    defer { try? handle.close() }
    handle.seekToEndOfFile()
    let line = message.hasSuffix("\n") ? message : message + "\n"
    if let data = line.data(using: .utf8) {
        handle.write(data)
    }
}

log("\n==========================================")
log("Dictate launched: \(Date())")
log("==========================================")

// Project directory = parent of Dictate.app
let bundleURL = URL(fileURLWithPath: Bundle.main.bundlePath)
let projectDir = bundleURL.deletingLastPathComponent()
log("Bundle: \(bundleURL.path)")
log("Project: \(projectDir.path)")

// uv path -- GUI-launched apps don't inherit .zshrc PATH, so we need an
// absolute path. Try several standard locations.
let uvCandidates = [
    NSString(string: "~/.local/bin/uv").expandingTildeInPath,
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
    "/usr/bin/uv",
]
guard let uvPath = uvCandidates.first(where: {
    FileManager.default.isExecutableFile(atPath: $0)
}) else {
    log("ERROR: uv not found in: \(uvCandidates.joined(separator: ", "))")
    exit(1)
}
log("uv: \(uvPath)")

// Log file handle for child stdout/stderr.
guard let childLog = try? FileHandle(forWritingTo: logURL) else {
    log("ERROR: could not open log file for child")
    exit(1)
}
childLog.seekToEndOfFile()

// Run the Python script through uv.
let task = Process()
task.executableURL = URL(fileURLWithPath: uvPath)
task.arguments = ["run", "python", "dictate.py"]
task.currentDirectoryURL = projectDir
task.standardOutput = childLog
task.standardError = childLog

// Set PATH for the child in case uv needs to resolve further tools.
var env = ProcessInfo.processInfo.environment
let homeBin = NSString(string: "~/.local/bin").expandingTildeInPath
let extraPaths = "\(homeBin):/opt/homebrew/bin:/usr/local/bin"
env["PATH"] = (env["PATH"] ?? "") + ":" + extraPaths
task.environment = env

do {
    try task.run()
    log("Child PID: \(task.processIdentifier)")
} catch {
    log("ERROR starting child: \(error)")
    exit(1)
}

task.waitUntilExit()
log("Child exited with status: \(task.terminationStatus)")
exit(task.terminationStatus)
