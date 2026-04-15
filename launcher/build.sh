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

# Ad-hoc sign the bundle. Doesn't stabilise cdhash (swiftc output differs
# every build anyway), but gives a deterministic signature handle and
# avoids "damaged app" warnings on some systems.
codesign --force --sign - "$PROJECT_DIR/Dictate.app" 2>/dev/null || true

# Re-register with Launch Services so macOS picks up the change.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
    -f "$PROJECT_DIR/Dictate.app"

echo "OK -- Dictate.app rebuilt."
echo
echo "============================================================"
echo "IMPORTANT: this rebuild changed the launcher's cdhash, which"
echo "invalidates your existing TCC grants. You WILL need to re-add"
echo "Dictate.app to at least these two System Settings panes:"
echo
echo "  Privacy & Security > Input Monitoring"
echo "  Privacy & Security > Accessibility"
echo
echo "In each pane: select any existing Dictate entry, press '-',"
echo "then '+' and pick /Users/hinn/code/claude/dictate/Dictate.app"
echo "(or wherever your checkout lives) and toggle it on. Restart"
echo "Dictate.app afterwards. Until then, the global hotkey and"
echo "paste-into-focused-app will silently fail."
echo "============================================================"
