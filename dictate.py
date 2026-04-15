"""
Local speech-to-text dictation for macOS -- menubar app.

Workflow:
    Press Cmd+Shift+9 (or menubar > Start recording)
    speak
    Press Cmd+Shift+9 again (or menubar > Stop recording)
    -> text is transcribed and inserted at the cursor (Cmd+V).

Hotkey pressed during transcription = cancel (result is discarded).

Menubar icon shows state:
    idle           🎙  (or Resources/menubar-idle.png)
    recording      🔴  (or Resources/menubar-recording.png)
    transcribing   ⏳  (or Resources/menubar-transcribing.png)

Menu contains: Start/Stop toggle, History (last 5 -- click copies to
clipboard), Language (German/English/Auto-detect), "Launch at login"
(macOS 13+), and a model info line.

First-time setup:
    uv sync                                    # install dependencies
    uv run python dictate.py --download        # preload model (~1.5 GB)
    bash launcher/build.sh                     # compile Swift launcher
    open Dictate.app                           # start the app
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np
import rumps
import sounddevice as sd
from pynput.keyboard import Controller, HotKey, Key, KeyCode, Listener

# ============================================================================
# Configuration
# ============================================================================

MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_LANGUAGE: str | None = "de"  # None = auto-detect

# (Menu label, Whisper language code or None for auto)
AVAILABLE_LANGUAGES: list[tuple[str, str | None]] = [
    ("German", "de"),
    ("English", "en"),
    ("Auto-detect", None),
]

SAMPLE_RATE = 16_000
CHANNELS = 1
MAX_RECORDING_SECONDS = 120
HISTORY_SIZE = 5

HOTKEY_MODIFIERS: frozenset = frozenset({Key.cmd, Key.shift})
HOTKEY_TRIGGER = KeyCode.from_char("9")

SOUND_START = "/System/Library/Sounds/Tink.aiff"
SOUND_STOP = "/System/Library/Sounds/Pop.aiff"
SOUND_CANCEL = "/System/Library/Sounds/Funk.aiff"

PASTE_DELAY_BEFORE = 0.05
PASTE_DELAY_AFTER = 0.40  # generous: slow apps (Slack, Notion) need this

# Emoji fallback for the menubar icon (used when no PNGs are bundled).
ICON_IDLE_EMOJI = "🎙"
ICON_RECORDING_EMOJI = "🔴"
ICON_TRANSCRIBING_EMOJI = "⏳"

# Optional template PNGs. If present, they are used as template images
# (auto-adapt to Dark/Light mode). Otherwise emoji fallback.
_PROJECT_DIR = Path(__file__).resolve().parent
_RESOURCES = _PROJECT_DIR / "Dictate.app" / "Contents" / "Resources"
ICON_IDLE_PNG = _RESOURCES / "menubar-idle.png"
ICON_RECORDING_PNG = _RESOURCES / "menubar-recording.png"
ICON_TRANSCRIBING_PNG = _RESOURCES / "menubar-transcribing.png"

# Swift launcher binary inside the bundle -- used for login item management,
# because SMAppService requires the caller's Bundle.main to be the .app
# (a Python child process doesn't satisfy that; the Swift launcher does).
_LAUNCHER_BIN = _PROJECT_DIR / "Dictate.app" / "Contents" / "MacOS" / "Dictate"


# ============================================================================
# NSPasteboard (via pyobjc, shipped with rumps)
# ============================================================================

try:
    from AppKit import NSPasteboard, NSPasteboardTypeString

    _PASTEBOARD = NSPasteboard.generalPasteboard()
except Exception as _e:  # noqa: BLE001
    print(f"[clipboard] NSPasteboard unavailable ({_e}), falling back to pbcopy/pbpaste.")
    _PASTEBOARD = None
    NSPasteboardTypeString = None  # type: ignore[assignment]


def _clipboard_read() -> str:
    if _PASTEBOARD is not None:
        return _PASTEBOARD.stringForType_(NSPasteboardTypeString) or ""
    try:
        return subprocess.run(
            ["pbpaste"], capture_output=True, text=True, check=False
        ).stdout
    except Exception:  # noqa: BLE001
        return ""


def _clipboard_write(text: str) -> None:
    if _PASTEBOARD is not None:
        _PASTEBOARD.clearContents()
        _PASTEBOARD.setString_forType_(text, NSPasteboardTypeString)
        return
    subprocess.run(["pbcopy"], input=text, text=True, check=True)


# ============================================================================
# Audio recording
# ============================================================================

def record_audio(stop_event: threading.Event) -> np.ndarray:
    chunks: list[np.ndarray] = []
    start_time = time.monotonic()

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        chunks.append(indata.copy())

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        callback=callback,
    ):
        while not stop_event.is_set():
            if time.monotonic() - start_time > MAX_RECORDING_SECONDS:
                print(f"[recorder] {MAX_RECORDING_SECONDS}s cap reached, stopping.")
                break
            time.sleep(0.05)

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks, axis=0).flatten()


# ============================================================================
# Transcription
# ============================================================================

def transcribe(audio: np.ndarray, language: str | None) -> str:
    import mlx_whisper

    kwargs: dict = {"path_or_hf_repo": MODEL}
    if language is not None:
        kwargs["language"] = language

    result = mlx_whisper.transcribe(audio, **kwargs)
    return result.get("text", "").strip()


# ============================================================================
# Insert text
# ============================================================================

def insert_text(text: str) -> None:
    if not text:
        print("[paste] Empty text, skipping.")
        return

    old = _clipboard_read()
    _clipboard_write(text)
    time.sleep(PASTE_DELAY_BEFORE)

    kb = Controller()
    with kb.pressed(Key.cmd):
        kb.press("v")
        kb.release("v")

    time.sleep(PASTE_DELAY_AFTER)

    try:
        _clipboard_write(old)
    except Exception as e:  # noqa: BLE001
        print(f"[paste] Clipboard restore failed: {e}", file=sys.stderr)


# ============================================================================
# Permission diagnostics
# ============================================================================

def check_accessibility_permission() -> bool | None:
    """Returns True if Accessibility is granted, False if not, None if the
    check API is unavailable. Accessibility is required for simulating ⌘V
    via pynput.Controller (paste-after-transcription).
    """
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore[import-not-found]
        return bool(AXIsProcessTrusted())
    except Exception as e:  # noqa: BLE001
        print(f"[permissions] Accessibility check unavailable: {e}", file=sys.stderr)
        return None


# ============================================================================
# Sound feedback
# ============================================================================

def play_sound(path: str) -> None:
    if not Path(path).exists():
        return
    subprocess.Popen(
        ["afplay", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ============================================================================
# Login Item (via the Swift launcher which has Bundle.main context)
# ============================================================================

def login_item_available() -> bool:
    return _LAUNCHER_BIN.exists()


def login_item_status() -> str:
    """Returns 'enabled', 'disabled', 'requires-approval', 'not-found',
    'unknown', or 'unavailable'."""
    if not login_item_available():
        return "unavailable"
    try:
        r = subprocess.run(
            [str(_LAUNCHER_BIN), "--login-item-status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            return (r.stdout.strip() or "unknown")
        return "unavailable"
    except Exception as e:  # noqa: BLE001
        print(f"[loginitem] status error: {e}", file=sys.stderr)
        return "unavailable"


def set_login_item(enable: bool) -> tuple[bool, str]:
    if not login_item_available():
        return False, "Launcher binary not found (run bash launcher/build.sh)."
    flag = "--register-login-item" if enable else "--unregister-login-item"
    try:
        r = subprocess.run(
            [str(_LAUNCHER_BIN), flag],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "unknown error").strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ============================================================================
# Dictation engine
# ============================================================================

class Dictation:
    """Manages one record / transcribe cycle.

    States: 'idle' -> 'recording' -> 'transcribing' -> 'idle'.
    Toggle while 'transcribing' = cancel (result is discarded).
    """

    def __init__(
        self,
        state_callback: Callable[[str], None],
        language_getter: Callable[[], str | None],
        history_callback: Callable[[str], None],
    ) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._state = "idle"
        self._state_callback = state_callback
        self._language_getter = language_getter
        self._history_callback = history_callback

    @property
    def state(self) -> str:
        return self._state

    def toggle(self) -> None:
        with self._lock:
            if self._state == "recording":
                self._stop()
            elif self._state == "transcribing":
                self._cancel()
            else:
                self._start()

    def _emit(self, state: str) -> None:
        self._state = state
        try:
            self._state_callback(state)
        except Exception as e:  # noqa: BLE001
            print(f"[state] callback error: {e}", file=sys.stderr)

    def _start(self) -> None:
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()
        play_sound(SOUND_START)
        print("[rec] Recording started.")
        self._emit("recording")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _stop(self) -> None:
        print("[rec] Stop signal sent.")
        self._stop_event.set()

    def _cancel(self) -> None:
        print("[rec] Cancel during transcription.")
        self._cancel_event.set()
        play_sound(SOUND_CANCEL)
        # UI goes back to idle immediately; the transcription thread keeps
        # running in the background but its result is discarded.
        self._emit("idle")

    def _run(self) -> None:
        try:
            audio = record_audio(self._stop_event)
            play_sound(SOUND_STOP)
            if self._cancel_event.is_set():
                print("[rec] Cancelled before transcription.")
                return
            duration = len(audio) / SAMPLE_RATE
            print(f"[rec] Recording ended ({duration:.1f}s). Transcribing...")
            if duration < 0.3:
                print("[rec] Too short, ignoring.")
                return
            self._emit("transcribing")
            text = transcribe(audio, self._language_getter())
            if self._cancel_event.is_set():
                print(f"[rec] Result discarded (cancelled): {text!r}")
                return
            print(f"[text] {text!r}")
            if text:
                self._history_callback(text)
                insert_text(text)
        except Exception as e:  # noqa: BLE001
            print(f"[rec] Error: {e}", file=sys.stderr)
        finally:
            # Only emit idle if we haven't already switched via _cancel().
            if self._state != "idle":
                self._emit("idle")


# ============================================================================
# Hotkey listener
# ============================================================================

def _hotkey_spec() -> str:
    """Builds pynput's HotKey-parse string from HOTKEY_MODIFIERS + TRIGGER."""
    parts: list[str] = []
    if Key.cmd in HOTKEY_MODIFIERS:
        parts.append("<cmd>")
    if Key.shift in HOTKEY_MODIFIERS:
        parts.append("<shift>")
    if Key.ctrl in HOTKEY_MODIFIERS:
        parts.append("<ctrl>")
    if Key.alt in HOTKEY_MODIFIERS:
        parts.append("<alt>")
    trigger_char = getattr(HOTKEY_TRIGGER, "char", None)
    if trigger_char is None:
        raise RuntimeError("HOTKEY_TRIGGER must be a character KeyCode")
    parts.append(trigger_char)
    return "+".join(parts)


def run_listener(dictation: Dictation) -> None:
    # Why HotKey + canonical() and not our own modifier-tracking?
    # On German QWERTZ, Shift+9 produces "(" -- a plain char equality
    # against "9" would never match, which is exactly the bug that made
    # ⌘⇧9 silently do nothing. pynput's `listener.canonical(key)` maps
    # the physical key back to its layout-independent form, so modifier
    # combos work across keyboard layouts.

    def on_activate() -> None:
        print("[hotkey] combo detected, toggling dictation")
        dictation.toggle()

    spec = _hotkey_spec()
    hotkey = HotKey(HotKey.parse(spec), on_activate)

    # Debug: count key events so we can confirm the listener is alive.
    # DICTATE_KEY_TRACE=1 logs every canonical keypress to stderr
    # (diagnostic only -- leaks keystrokes; only enable while debugging).
    import os
    trace = os.environ.get("DICTATE_KEY_TRACE") == "1"

    def for_canonical(handler):
        def wrapped(k):
            try:
                canonical = listener.canonical(k)
            except Exception as e:  # noqa: BLE001
                print(f"[hotkey] canonical() error: {e}", file=sys.stderr)
                return
            if trace:
                print(f"[hotkey] key={k!r} canonical={canonical!r}", file=sys.stderr)
            handler(canonical)
        return wrapped

    print(f"[hotkey] listener starting; waiting for {spec}")
    with Listener(
        on_press=for_canonical(hotkey.press),
        on_release=for_canonical(hotkey.release),
    ) as listener:
        print("[hotkey] listener running")
        listener.join()


# ============================================================================
# Menubar app
# ============================================================================

def _hotkey_label() -> str:
    parts = []
    if Key.cmd in HOTKEY_MODIFIERS:
        parts.append("⌘")
    if Key.shift in HOTKEY_MODIFIERS:
        parts.append("⇧")
    if Key.ctrl in HOTKEY_MODIFIERS:
        parts.append("⌃")
    if Key.alt in HOTKEY_MODIFIERS:
        parts.append("⌥")
    parts.append(getattr(HOTKEY_TRIGGER, "char", str(HOTKEY_TRIGGER)))
    return "".join(parts)


def _truncate(text: str, max_len: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


class DictateApp(rumps.App):
    def __init__(self) -> None:
        # Title / icon are set in _apply_appearance().
        super().__init__(ICON_IDLE_EMOJI, quit_button=None)

        self._language: str | None = DEFAULT_LANGUAGE
        self._history: deque[str] = deque(maxlen=HISTORY_SIZE)
        self._use_png_icons = all(
            p.exists()
            for p in (ICON_IDLE_PNG, ICON_RECORDING_PNG, ICON_TRANSCRIBING_PNG)
        )

        # ---- Build menu ----
        # No key= attribute here: NSStatusItem menus only fire keyEquivalents
        # while the menu is open. The real global hotkey is ⌘⇧9 (pynput).
        # Surface it inline in the item title instead.
        self._toggle_item = rumps.MenuItem(
            f"Start recording  ({_hotkey_label()})",
            callback=self._on_toggle_clicked,
        )

        # History: fixed slots; we update title + callback on each new entry.
        self._history_menu = rumps.MenuItem("History")
        self._history_items: list[rumps.MenuItem] = []
        for _ in range(HISTORY_SIZE):
            item = rumps.MenuItem("—")
            self._history_items.append(item)
            self._history_menu.add(item)
        self._refresh_history_menu()

        # Language
        self._language_items: dict[str | None, rumps.MenuItem] = {}
        language_menu = rumps.MenuItem("Language")
        for label, code in AVAILABLE_LANGUAGES:
            item = rumps.MenuItem(
                label, callback=self._make_language_setter(code)
            )
            if code == self._language:
                item.state = 1
            self._language_items[code] = item
            language_menu.add(item)

        # Login Item (only if the launcher can service it)
        self._login_item: rumps.MenuItem | None = None
        initial_status = login_item_status()
        if initial_status != "unavailable":
            self._login_item = rumps.MenuItem(
                "Launch at login", callback=self._on_login_toggle
            )
            self._login_item.state = 1 if initial_status == "enabled" else 0

        # Assemble menu
        menu: list = [
            self._toggle_item,
            None,
            self._history_menu,
            language_menu,
        ]
        if self._login_item is not None:
            menu += [None, self._login_item]
        menu += [
            None,
            # The global hotkey is already shown in the toggle item; an info
            # row would be duplicate.
            rumps.MenuItem(f"Model: {MODEL.split('/')[-1]}"),
            None,
            rumps.MenuItem(
                "Quit", callback=self._on_quit_clicked, key="q"
            ),
        ]
        self.menu = menu

        # ---- Start dictation + listener ----
        self.dictation = Dictation(
            state_callback=self._on_state_change,
            language_getter=lambda: self._language,
            history_callback=self._add_to_history,
        )
        self._listener_thread = threading.Thread(
            target=run_listener, args=(self.dictation,), daemon=True
        )
        self._listener_thread.start()

        # Apply initial appearance.
        self._apply_appearance("idle")

        # Preload the model in the background so the first real transcription
        # doesn't eat the MLX cold-start.
        self._preload_thread = threading.Thread(
            target=self._preload_model, daemon=True
        )
        self._preload_thread.start()

        print(f"[app] Menubar app started. Hotkey: {_hotkey_label()}")
        print(f"[app] Login item status: {initial_status}")
        print(f"[app] Icons: {'PNG' if self._use_png_icons else 'emoji'}")

        # Accessibility permission diagnostic -- needed to simulate ⌘V.
        # Without it, the paste-after-transcription silently no-ops.
        ax = check_accessibility_permission()
        if ax is True:
            print("[permissions] Accessibility: granted (paste will work)")
        elif ax is False:
            print(
                "[permissions] Accessibility: MISSING -- paste will silently "
                "fail. Fix: System Settings > Privacy & Security > "
                "Accessibility, add Dictate.app, toggle on, restart app."
            )
        else:
            print("[permissions] Accessibility: unable to verify")

    # ---------- Appearance ----------

    def _apply_appearance(self, state: str) -> None:
        if self._use_png_icons:
            icon_path = {
                "recording": ICON_RECORDING_PNG,
                "transcribing": ICON_TRANSCRIBING_PNG,
            }.get(state, ICON_IDLE_PNG)
            # rumps: the icon property sets NSStatusItem.button.image as
            # template (auto Dark/Light).
            self.title = None
            self.icon = str(icon_path)
            self.template = True
        else:
            emoji = {
                "recording": ICON_RECORDING_EMOJI,
                "transcribing": ICON_TRANSCRIBING_EMOJI,
            }.get(state, ICON_IDLE_EMOJI)
            self.icon = None
            self.title = emoji

        hk = _hotkey_label()
        if state == "recording":
            self._toggle_item.title = f"Stop recording  ({hk})"
        elif state == "transcribing":
            self._toggle_item.title = f"Cancel  ({hk})"
        else:
            self._toggle_item.title = f"Start recording  ({hk})"

    def _on_state_change(self, state: str) -> None:
        # Updating rumps properties from worker threads works in practice for
        # title/icon (AppKit queues the change).
        self._apply_appearance(state)

    # ---------- Preload ----------

    def _preload_model(self) -> None:
        try:
            silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
            transcribe(silence, self._language)
            print("[preload] Model loaded; first transcription will be fast.")
        except Exception as e:  # noqa: BLE001
            print(f"[preload] Error: {e}", file=sys.stderr)

    # ---------- Callbacks ----------

    def _on_toggle_clicked(self, _sender) -> None:
        self.dictation.toggle()

    def _on_quit_clicked(self, _sender) -> None:
        rumps.quit_application()

    def _make_language_setter(self, code: str | None):
        def handler(_sender) -> None:
            self._language = code
            for c, item in self._language_items.items():
                item.state = 1 if c == code else 0
            label = {"de": "German", "en": "English", None: "Auto"}.get(code, str(code))
            print(f"[lang] switched to {label} ({code!r})")
        return handler

    def _on_login_toggle(self, sender) -> None:
        assert self._login_item is not None
        enable = sender.state == 0  # currently off -> turn on
        ok, err = set_login_item(enable)
        if ok:
            sender.state = 1 if enable else 0
            print(f"[loginitem] {'enabled' if enable else 'disabled'}")
        else:
            print(f"[loginitem] failed: {err}", file=sys.stderr)
            rumps.alert(
                title="Launch at login",
                message=f"Could not {'enable' if enable else 'disable'} login item:\n{err}",
            )

    # ---------- History ----------

    def _add_to_history(self, text: str) -> None:
        self._history.appendleft(text)
        self._refresh_history_menu()

    def _refresh_history_menu(self) -> None:
        for i, item in enumerate(self._history_items):
            if i < len(self._history):
                entry = self._history[i]
                item.title = _truncate(entry, 60)
                item.set_callback(self._make_history_copier(entry))
            else:
                item.title = "—" if i == 0 else ""
                item.set_callback(None)
        # Informative line when the history is empty.
        if len(self._history) == 0:
            self._history_items[0].title = "(empty)"

    def _make_history_copier(self, text: str):
        """History click = copy to clipboard only. User then presses ⌘V
        wherever they want. This is simpler and safer than auto-paste
        (which depends on the focused app and TCC Accessibility)."""
        def handler(_sender) -> None:
            _clipboard_write(text)
            print(f"[history] copied to clipboard: {text!r}")
        return handler


# ============================================================================
# CLI
# ============================================================================

def warmup_download() -> None:
    print(f"Downloading model {MODEL} (~1.5 GB on first run)...")
    silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
    text = transcribe(silence, DEFAULT_LANGUAGE)
    print(f"Model ready. (silence -> {text!r})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Whisper dictation for macOS")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download the model and exit (one-off warmup).",
    )
    args = parser.parse_args()

    if args.download:
        warmup_download()
        return

    DictateApp().run()


if __name__ == "__main__":
    main()
