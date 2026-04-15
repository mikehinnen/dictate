"""
Lokales Speech-to-Text-Diktat fuer macOS -- Menubar-App.

Workflow:
    Cmd+Shift+9 druecken (oder Menubar > Aufnahme starten)
    sprechen
    Cmd+Shift+9 druecken (oder Menubar > Aufnahme stoppen)
    -> Text wird transkribiert und am Cursor eingefuegt (Cmd+V)

Hotkey waehrend Transkription laufend = Abbrechen (Text wird verworfen).

Menubar-Icon zeigt Status:
    idle           🎙  (oder Resources/menubar-idle.png)
    nimmt auf      🔴  (oder Resources/menubar-recording.png)
    transkribiert  ⏳  (oder Resources/menubar-transcribing.png)

Menue enthaelt: Aufnahme-Toggle, Verlauf (letzte 5), Sprache (DE/EN/Auto),
"Beim Login starten" (macOS 13+), und Info-Zeilen zu Hotkey/Modell.

Erste Einrichtung:
    uv sync                                    # Abhaengigkeiten
    uv run python dictate.py --download        # Modell (~1.5 GB) laden
    bash launcher/build.sh                     # Swift-Launcher kompilieren
    open Dictate.app                           # App starten (Doppelklick)
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
from pynput.keyboard import Controller, Key, KeyCode, Listener

# ============================================================================
# Konfiguration
# ============================================================================

MODEL = "mlx-community/whisper-large-v3-turbo"
DEFAULT_LANGUAGE: str | None = "de"  # None = auto-detect

# (Label im Menue, Whisper-Code oder None fuer Auto)
AVAILABLE_LANGUAGES: list[tuple[str, str | None]] = [
    ("Deutsch", "de"),
    ("English", "en"),
    ("Auto-Erkennung", None),
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
PASTE_DELAY_AFTER = 0.40  # grosszuegig: langsame Apps (Slack, Notion) brauchen das

# Menubar-Icons (Emoji-Fallback, falls keine PNGs im Bundle liegen)
ICON_IDLE_EMOJI = "🎙"
ICON_RECORDING_EMOJI = "🔴"
ICON_TRANSCRIBING_EMOJI = "⏳"

# Optionale Template-PNGs. Falls vorhanden -> werden als template image genutzt
# und passen sich Dark/Light Mode an. Sonst Emoji-Fallback.
_PROJECT_DIR = Path(__file__).resolve().parent
_RESOURCES = _PROJECT_DIR / "Dictate.app" / "Contents" / "Resources"
ICON_IDLE_PNG = _RESOURCES / "menubar-idle.png"
ICON_RECORDING_PNG = _RESOURCES / "menubar-recording.png"
ICON_TRANSCRIBING_PNG = _RESOURCES / "menubar-transcribing.png"

# Swift-Launcher-Binary im Bundle -- wird fuer Login-Item-Verwaltung genutzt,
# weil SMAppService an die Bundle-Identitaet gebunden ist (ein Python-Child
# hat die nicht; der Swift-Launcher schon).
_LAUNCHER_BIN = _PROJECT_DIR / "Dictate.app" / "Contents" / "MacOS" / "Dictate"


# ============================================================================
# NSPasteboard (via pyobjc, kommt mit rumps)
# ============================================================================

try:
    from AppKit import NSPasteboard, NSPasteboardTypeString

    _PASTEBOARD = NSPasteboard.generalPasteboard()
except Exception as _e:  # noqa: BLE001
    print(f"[clipboard] NSPasteboard nicht verfuegbar ({_e}), falle auf pbcopy/pbpaste zurueck.")
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
# Audio-Aufnahme
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
                print(f"[recorder] {MAX_RECORDING_SECONDS}s-Cap erreicht, stoppe.")
                break
            time.sleep(0.05)

    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks, axis=0).flatten()


# ============================================================================
# Transkription
# ============================================================================

def transcribe(audio: np.ndarray, language: str | None) -> str:
    import mlx_whisper

    kwargs: dict = {"path_or_hf_repo": MODEL}
    if language is not None:
        kwargs["language"] = language

    result = mlx_whisper.transcribe(audio, **kwargs)
    return result.get("text", "").strip()


# ============================================================================
# Text einfuegen
# ============================================================================

def insert_text(text: str) -> None:
    if not text:
        print("[paste] Leerer Text, ueberspringe.")
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
        print(f"[paste] Clipboard-Restore fehlgeschlagen: {e}", file=sys.stderr)


# ============================================================================
# Sound-Feedback
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
# Login Item (via Swift-Launcher, der Bundle.main-Kontext hat)
# ============================================================================

def login_item_available() -> bool:
    return _LAUNCHER_BIN.exists()


def login_item_status() -> str:
    """Gibt 'enabled', 'disabled', 'requires-approval', 'not-found',
    'unknown' oder 'unavailable' zurueck."""
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
        return False, "Launcher-Binary nicht gefunden (bash launcher/build.sh ausfuehren)."
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
        return False, (r.stderr or r.stdout or "unbekannter Fehler").strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


# ============================================================================
# Dictation-Engine
# ============================================================================

class Dictation:
    """Verwaltet einen Aufnahme-/Transkriptions-Zyklus.

    Zustaende: 'idle' -> 'recording' -> 'transcribing' -> 'idle'.
    Toggle waehrend 'transcribing' = Abbrechen (Ergebnis wird verworfen).
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
        print("[rec] Aufnahme laeuft.")
        self._emit("recording")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _stop(self) -> None:
        print("[rec] Stop-Signal gesendet.")
        self._stop_event.set()

    def _cancel(self) -> None:
        print("[rec] Abbrechen waehrend Transkription.")
        self._cancel_event.set()
        play_sound(SOUND_CANCEL)
        # UI sofort zurueck -- die Transkription laeuft noch im Hintergrund
        # weiter, aber das Ergebnis wird verworfen.
        self._emit("idle")

    def _run(self) -> None:
        try:
            audio = record_audio(self._stop_event)
            play_sound(SOUND_STOP)
            if self._cancel_event.is_set():
                print("[rec] Abgebrochen vor Transkription.")
                return
            duration = len(audio) / SAMPLE_RATE
            print(f"[rec] Beendet ({duration:.1f}s). Transkribiere...")
            if duration < 0.3:
                print("[rec] Zu kurz, ignoriere.")
                return
            self._emit("transcribing")
            text = transcribe(audio, self._language_getter())
            if self._cancel_event.is_set():
                print(f"[rec] Ergebnis verworfen (Abbruch): {text!r}")
                return
            print(f"[text] {text!r}")
            if text:
                self._history_callback(text)
                insert_text(text)
        except Exception as e:  # noqa: BLE001
            print(f"[rec] Fehler: {e}", file=sys.stderr)
        finally:
            # Nur emittieren, wenn wir nicht schon via _cancel() auf idle
            # geschaltet haben.
            if self._state != "idle":
                self._emit("idle")


# ============================================================================
# Hotkey-Listener
# ============================================================================

def _normalize(key) -> object:
    if key in (Key.cmd_l, Key.cmd_r):
        return Key.cmd
    if key in (Key.shift_l, Key.shift_r):
        return Key.shift
    if key in (Key.ctrl_l, Key.ctrl_r):
        return Key.ctrl
    if key in (Key.alt_l, Key.alt_r):
        return Key.alt
    return key


def run_listener(dictation: Dictation) -> None:
    pressed: set = set()

    def on_press(key) -> None:
        k = _normalize(key)
        pressed.add(k)
        if HOTKEY_MODIFIERS.issubset(pressed) and k == HOTKEY_TRIGGER:
            dictation.toggle()

    def on_release(key) -> None:
        k = _normalize(key)
        pressed.discard(k)

    with Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()


# ============================================================================
# Menubar-App
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
        # Titel / Icon werden in _apply_appearance gesetzt.
        super().__init__(ICON_IDLE_EMOJI, quit_button=None)

        self._language: str | None = DEFAULT_LANGUAGE
        self._history: deque[str] = deque(maxlen=HISTORY_SIZE)
        self._use_png_icons = all(
            p.exists()
            for p in (ICON_IDLE_PNG, ICON_RECORDING_PNG, ICON_TRANSCRIBING_PNG)
        )

        # ---- Menue aufbauen ----
        # Kein key= mehr: NSStatusItem-Menues feuern keyEquivalents nur
        # waehrend das Menu offen ist. Der globale Hotkey ist ⌘⇧9 (pynput).
        # Stattdessen den Hotkey direkt im Titel anzeigen.
        self._toggle_item = rumps.MenuItem(
            f"Aufnahme starten  ({_hotkey_label()})",
            callback=self._on_toggle_clicked,
        )

        # Verlauf: fixe Anzahl Platzhalter, wir updaten nur Titel + Callback.
        self._history_menu = rumps.MenuItem("Verlauf")
        self._history_items: list[rumps.MenuItem] = []
        for _ in range(HISTORY_SIZE):
            item = rumps.MenuItem("—")
            self._history_items.append(item)
            self._history_menu.add(item)
        self._refresh_history_menu()

        # Sprache
        self._language_items: dict[str | None, rumps.MenuItem] = {}
        language_menu = rumps.MenuItem("Sprache")
        for label, code in AVAILABLE_LANGUAGES:
            item = rumps.MenuItem(
                label, callback=self._make_language_setter(code)
            )
            if code == self._language:
                item.state = 1
            self._language_items[code] = item
            language_menu.add(item)

        # Login Item (nur wenn verfuegbar)
        self._login_item: rumps.MenuItem | None = None
        initial_status = login_item_status()
        if initial_status != "unavailable":
            self._login_item = rumps.MenuItem(
                "Beim Login starten", callback=self._on_login_toggle
            )
            self._login_item.state = 1 if initial_status == "enabled" else 0

        # Menue zusammensetzen
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
            # Hotkey steht direkt im Toggle-Item -- Info-Zeile waere Doppelung.
            rumps.MenuItem(f"Modell: {MODEL.split('/')[-1]}"),
            None,
            rumps.MenuItem(
                "Beenden", callback=self._on_quit_clicked, key="q"
            ),
        ]
        self.menu = menu

        # ---- Dictation + Listener starten ----
        self.dictation = Dictation(
            state_callback=self._on_state_change,
            language_getter=lambda: self._language,
            history_callback=self._add_to_history,
        )
        self._listener_thread = threading.Thread(
            target=run_listener, args=(self.dictation,), daemon=True
        )
        self._listener_thread.start()

        # Initiales Erscheinungsbild setzen.
        self._apply_appearance("idle")

        # Modell im Hintergrund vorwaermen, damit die erste echte Aufnahme
        # nicht den MLX-Kaltstart mitschleppt.
        self._preload_thread = threading.Thread(
            target=self._preload_model, daemon=True
        )
        self._preload_thread.start()

        print(f"[app] Menubar-App gestartet. Hotkey: {_hotkey_label()}")
        print(f"[app] Login-Item-Status: {initial_status}")
        print(f"[app] Icons: {'PNG' if self._use_png_icons else 'Emoji'}")

    # ---------- Appearance ----------

    def _apply_appearance(self, state: str) -> None:
        if self._use_png_icons:
            icon_path = {
                "recording": ICON_RECORDING_PNG,
                "transcribing": ICON_TRANSCRIBING_PNG,
            }.get(state, ICON_IDLE_PNG)
            # rumps: icon-Property setzt NSStatusItem.button.image als Template.
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
            self._toggle_item.title = f"Aufnahme stoppen  ({hk})"
        elif state == "transcribing":
            self._toggle_item.title = f"Abbrechen  ({hk})"
        else:
            self._toggle_item.title = f"Aufnahme starten  ({hk})"

    def _on_state_change(self, state: str) -> None:
        # rumps-Property-Updates aus Worker-Threads funktionieren fuer
        # title/icon in der Praxis (AppKit queued die Aenderung).
        self._apply_appearance(state)

    # ---------- Preload ----------

    def _preload_model(self) -> None:
        try:
            silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
            transcribe(silence, self._language)
            print("[preload] Modell geladen, erste Transkription ist schnell.")
        except Exception as e:  # noqa: BLE001
            print(f"[preload] Fehler: {e}", file=sys.stderr)

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
            label = {"de": "Deutsch", "en": "English", None: "Auto"}.get(code, str(code))
            print(f"[lang] gewechselt zu {label} ({code!r})")
        return handler

    def _on_login_toggle(self, sender) -> None:
        assert self._login_item is not None
        enable = sender.state == 0  # aktuell aus -> einschalten
        ok, err = set_login_item(enable)
        if ok:
            sender.state = 1 if enable else 0
            print(f"[loginitem] {'aktiviert' if enable else 'deaktiviert'}")
        else:
            print(f"[loginitem] fehlgeschlagen: {err}", file=sys.stderr)
            rumps.alert(
                title="Login-Item",
                message=f"Konnte nicht {'aktiviert' if enable else 'deaktiviert'} werden:\n{err}",
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
                item.set_callback(self._make_history_paster(entry))
            else:
                item.title = "—" if i == 0 else ""
                item.set_callback(None)
        # Wenn Verlauf leer ist, zeige eine informative Zeile.
        if len(self._history) == 0:
            self._history_items[0].title = "(noch leer)"

    def _make_history_paster(self, text: str):
        def handler(_sender) -> None:
            print(f"[history] erneut einfuegen: {text!r}")
            threading.Thread(
                target=insert_text, args=(text,), daemon=True
            ).start()
        return handler


# ============================================================================
# CLI
# ============================================================================

def warmup_download() -> None:
    print(f"Lade Modell {MODEL} (~1.5 GB beim ersten Mal)...")
    silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
    text = transcribe(silence, DEFAULT_LANGUAGE)
    print(f"Modell bereit. (Stille -> {text!r})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lokales Whisper-Diktat fuer macOS")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Modell herunterladen und beenden (einmaliger Warmup).",
    )
    args = parser.parse_args()

    if args.download:
        warmup_download()
        return

    DictateApp().run()


if __name__ == "__main__":
    main()
