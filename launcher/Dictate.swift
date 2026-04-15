// Dictate.swift -- Native Launcher fuer Dictate.app.
//
// Warum nicht ein Shell-Script? macOS TCC verfolgt die Bundle-Identitaet ueber
// die Prozesskette. Sobald ein Shell-Script `exec` macht, geht die Bundle-ID
// verloren und TCC listet stattdessen den Namen der Final-Binary (z.B. "uv").
// Eine native Binary als Bundle-Executable behaelt die Identitaet, Childs
// erben sie via posix_spawn (Process) -- dann steht "Dictate" in den
// Permission-Listen.
//
// CLI-Modi (werden von dictate.py genutzt, um das Login-Item zu verwalten --
// SMAppService verlangt Bundle.main-Kontext, den hat nur diese Binary):
//   --login-item-status        gibt "enabled" / "disabled" / ... auf stdout
//   --register-login-item      registriert die App als Login Item
//   --unregister-login-item    entfernt sie wieder
// Ohne Argument laeuft die normale Launcher-Logik (spawnt Python).
//
// Build:
//   bash launcher/build.sh
// (kompiliert nach Dictate.app/Contents/MacOS/Dictate)

import Foundation
import ServiceManagement

// ---------------------------------------------------------------------------
// stderr-Helper
// ---------------------------------------------------------------------------

func writeStderr(_ s: String) {
    let line = s.hasSuffix("\n") ? s : s + "\n"
    if let data = line.data(using: .utf8) {
        FileHandle.standardError.write(data)
    }
}

// ---------------------------------------------------------------------------
// CLI-Modi: Login Item via SMAppService
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
        writeStderr("SMAppService-Fehler: \(error.localizedDescription)")
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
            // Unbekanntes Flag -> normale Launcher-Logik (Python starten).
            break
        }
    } else {
        switch flag {
        case "--login-item-status",
             "--register-login-item",
             "--unregister-login-item":
            writeStderr("Login Item braucht macOS 13+.")
            exit(2)
        default:
            break
        }
    }
}

// ---------------------------------------------------------------------------
// Normaler Launcher-Modus: Python-Child starten
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
log("Dictate gestartet: \(Date())")
log("==========================================")

// Projektverzeichnis = Eltern-Verzeichnis von Dictate.app
let bundleURL = URL(fileURLWithPath: Bundle.main.bundlePath)
let projectDir = bundleURL.deletingLastPathComponent()
log("Bundle: \(bundleURL.path)")
log("Project: \(projectDir.path)")

// uv-Pfad -- GUI-gestartete Apps haben kein .zshrc-PATH, deshalb absoluter
// Pfad. Mehrere Standardorte ausprobieren.
let uvCandidates = [
    NSString(string: "~/.local/bin/uv").expandingTildeInPath,
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
    "/usr/bin/uv",
]
guard let uvPath = uvCandidates.first(where: {
    FileManager.default.isExecutableFile(atPath: $0)
}) else {
    log("FEHLER: uv nicht gefunden in: \(uvCandidates.joined(separator: ", "))")
    exit(1)
}
log("uv: \(uvPath)")

// Logfile-Handle fuer Child stdout/stderr.
guard let childLog = try? FileHandle(forWritingTo: logURL) else {
    log("FEHLER: konnte Logfile fuer Child nicht oeffnen")
    exit(1)
}
childLog.seekToEndOfFile()

// Python-Script ueber uv starten.
let task = Process()
task.executableURL = URL(fileURLWithPath: uvPath)
task.arguments = ["run", "python", "dictate.py"]
task.currentDirectoryURL = projectDir
task.standardOutput = childLog
task.standardError = childLog

// PATH fuer den Child setzen, falls uv weitere Tools sucht.
var env = ProcessInfo.processInfo.environment
let homeBin = NSString(string: "~/.local/bin").expandingTildeInPath
let extraPaths = "\(homeBin):/opt/homebrew/bin:/usr/local/bin"
env["PATH"] = (env["PATH"] ?? "") + ":" + extraPaths
task.environment = env

do {
    try task.run()
    log("Child PID: \(task.processIdentifier)")
} catch {
    log("FEHLER beim Starten: \(error)")
    exit(1)
}

task.waitUntilExit()
log("Child beendet mit Status: \(task.terminationStatus)")
exit(task.terminationStatus)
