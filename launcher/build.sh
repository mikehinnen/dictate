#!/bin/bash
# Compiles the Swift launcher and replaces the binary inside Dictate.app.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_BIN="$PROJECT_DIR/Dictate.app/Contents/MacOS/Dictate"

mkdir -p "$(dirname "$APP_BIN")"

echo "Compiling $SCRIPT_DIR/Dictate.swift -> $APP_BIN"
swiftc \
    -O \
    -target arm64-apple-macos13 \
    -o "$APP_BIN" \
    "$SCRIPT_DIR/Dictate.swift"

chmod +x "$APP_BIN"

# Re-register with Launch Services so macOS picks up the change.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$PROJECT_DIR/Dictate.app"

echo "OK -- Dictate.app rebuilt."
echo
echo "NOTE: If old TCC entries (e.g. 'uv') are still around, remove them:"
echo "  System Settings > Privacy & Security > Input Monitoring  -> select 'uv' + '-'"
echo "  System Settings > Privacy & Security > Accessibility     -> select 'uv' + '-'"
echo "  System Settings > Privacy & Security > Microphone        -> select 'uv' + '-'"
echo "On next launch, macOS will prompt again -- this time as 'Dictate'."
