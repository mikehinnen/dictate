#!/bin/bash
# Kompiliert den Swift-Launcher und ersetzt das Shell-Script in Dictate.app.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_BIN="$PROJECT_DIR/Dictate.app/Contents/MacOS/Dictate"

mkdir -p "$(dirname "$APP_BIN")"

echo "Kompiliere $SCRIPT_DIR/Dictate.swift -> $APP_BIN"
swiftc \
    -O \
    -target arm64-apple-macos13 \
    -o "$APP_BIN" \
    "$SCRIPT_DIR/Dictate.swift"

chmod +x "$APP_BIN"

# Launch Services neu registrieren, damit macOS die Aenderung mitkriegt.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$PROJECT_DIR/Dictate.app"

echo "OK -- Dictate.app neu gebaut."
echo
echo "WICHTIG: Die alten TCC-Eintraege (uv) muessen jetzt geloescht werden:"
echo "  System Settings > Privacy & Security > Input Monitoring  -> 'uv' anwaehlen + '-'"
echo "  System Settings > Privacy & Security > Accessibility     -> 'uv' anwaehlen + '-'"
echo "  System Settings > Privacy & Security > Microphone        -> 'uv' anwaehlen + '-'"
echo "Beim naechsten Start fragt macOS neu nach -- diesmal als 'Dictate'."
