# dictate — lokales Whisper-Diktat für macOS

Menubar-App für Speech-to-Text, läuft zu 100 % lokal auf Apple Silicon.
Kein Cloud-Service, kein API-Key — nur dein Mikro und MLX-Whisper.

## Benutzung

Doppelklick auf `Dictate.app` → 🎙-Icon erscheint oben rechts in der Menüleiste.

| Aktion | Wie |
|---|---|
| Aufnahme starten/stoppen | Hotkey `⌘⇧9` **oder** Klick auf 🎙 → "Aufnahme starten" |
| Transkription abbrechen | Hotkey nochmal drücken, solange ⏳ läuft — Ergebnis wird verworfen |
| Sprache wechseln | Icon → "Sprache" → Deutsch / English / Auto-Erkennung |
| Letzte 5 Transkriptionen erneut einfügen | Icon → "Verlauf" → Eintrag anklicken |
| Bei Anmeldung starten | Icon → "Beim Login starten" (Checkbox) |
| Status sehen | Icon: 🎙 idle · 🔴 nimmt auf · ⏳ transkribiert |
| Beenden | Icon → "Beenden" (oder `⌘Q` im Menü) |

Nach der Transkription wird der Text per `⌘V` ins gerade aktive Fenster
eingefügt. Ablauf in TextEdit: Cursor reinsetzen → `⌘⇧9` → sprechen → `⌘⇧9`
→ Text erscheint.

## Setup (einmalig)

```sh
uv sync                                      # Dependencies installieren
uv run python dictate.py --download          # Whisper-Modell (~1.5 GB) laden
bash launcher/build.sh                       # Swift-Launcher kompilieren
open Dictate.app                             # erstes Mal starten
```

Der **Swift-Launcher** (`launcher/Dictate.swift`) ist eine winzige native Binary,
die als `Dictate.app/Contents/MacOS/Dictate` lebt. Sie startet `uv run python
dictate.py` als Child-Prozess. Wichtig wegen macOS TCC: ein Shell-Script als
Bundle-Executable würde die Bundle-Identität bei jedem `exec` verlieren — dann
zeigt macOS in den Permissions-Listen `uv` oder `python` statt `Dictate`. Eine
native Binary behält die Identität.

Die Binary ist **nicht im Repo** (per `.gitignore` ausgeschlossen) — jeder Checkout
muss `bash launcher/build.sh` einmal laufen lassen. Derselbe Befehl auch, wenn du
`Dictate.swift` änderst.

## macOS-Permissions (einmalig)

Beim ersten Tastendruck / ersten Recording fragt macOS drei Berechtigungen ab.
Diese werden an **Dictate.app** gebunden — *nicht* mehr ans Terminal.

| Permission | Wofür | Wann gefragt |
|---|---|---|
| **Microphone** | Audio-Aufnahme | Beim ersten Recording |
| **Accessibility** | `⌘V` simulieren | Beim ersten Einfügen |
| **Input Monitoring** | Globaler Hotkey | Beim ersten App-Start |

Direktlinks zu den Settings-Panes:
```sh
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
```

In jedem Pane: `+` → `Dictate.app` aus dem Projektordner hinzufügen → Toggle
einschalten. Danach `Dictate.app` einmal komplett quitten (Menü → Beenden) und
neu öffnen.

## Anpassen

Konstanten oben in `dictate.py`:

| Konstante | Default | Mögliche Werte |
|---|---|---|
| `MODEL` | `mlx-community/whisper-large-v3-turbo` | `whisper-large-v3-turbo-german-f16`, `whisper-medium-mlx`, `whisper-small-mlx` |
| `DEFAULT_LANGUAGE` | `"de"` | `"en"`, `None` (auto) — zur Laufzeit via Menü wechselbar |
| `MAX_RECORDING_SECONDS` | `120` | beliebig |
| `HISTORY_SIZE` | `5` | wie viele Einträge der Verlauf hält |
| `HOTKEY_MODIFIERS` | `{Key.cmd, Key.shift}` | `⌘`+`⇧` sind auf macOS am stabilsten |
| `HOTKEY_TRIGGER` | `KeyCode.from_char("9")` | jede Buchstaben-/Zifferntaste |

### Eigenes Menubar-Icon (optional)

Drei PNGs in `Dictate.app/Contents/Resources/` ablegen, dann nutzt die App
automatisch Template-Bilder statt Emoji (passen sich Dark/Light Mode an):

- `menubar-idle.png`
- `menubar-recording.png`
- `menubar-transcribing.png`

Empfohlen: 22×22 oder 44×44 PNG, schwarz auf transparent, Alpha-Kanal = Form.
macOS invertiert sie automatisch in Dark Mode.

## Logs

`Dictate.app` schreibt nach `~/Library/Logs/Dictate.log`. Live mitlesen:

```sh
tail -f ~/Library/Logs/Dictate.log
```

Oder via Console.app öffnen.

## Troubleshooting

- **Icon erscheint nicht** → Log checken (`tail -50 ~/Library/Logs/Dictate.log`).
  Häufigste Ursache: `uv` ist nicht in `~/.local/bin`, `/opt/homebrew/bin` oder
  `/usr/local/bin`. Pfad in `launcher/Dictate.swift` (`uvCandidates`) ergänzen
  und `bash launcher/build.sh` neu laufen lassen.
- **Hotkey reagiert nicht** → *Input Monitoring* checken; `Dictate.app` muss
  drin sein und Toggle an. App neu starten nach Permission-Erteilung.
- **Cmd+V fügt nichts ein** → *Accessibility*-Permission fehlt; siehe oben.
  Falls es bei langsamen Apps (Slack, Notion) trotzdem nicht klappt:
  `PASTE_DELAY_AFTER` in `dictate.py` weiter erhöhen.
- **Falsche deutsche Wörter** → `MODEL` auf
  `mlx-community/whisper-large-v3-turbo-german-f16` (DE-finetuned) wechseln.
- **Erste Transkription nach Start ist schnell** → Modell wird beim App-Start
  im Hintergrund vorgewärmt; sichtbar im Log als `[preload] Modell geladen`.
- **Modell-Cache löschen** → `rm -rf ~/.cache/huggingface/hub/models--mlx-community--whisper-*`
- **"Beim Login starten" greift nicht** → braucht macOS 13+. Status im
  Terminal prüfen: `./Dictate.app/Contents/MacOS/Dictate --login-item-status`.
- **Permissions zeigen "uv" oder "python" statt "Dictate"** → alter
  Shell-Launcher noch im Bundle. Lösung: `bash launcher/build.sh`, dann alte
  Einträge aus den drei Permission-Panes löschen (Anwählen + `−`),
  `Dictate.app` neu starten.

## Im Terminal starten (Fallback / Debugging)

Wenn die App-Variante zickt, geht der direkte Weg weiter:

```sh
uv run python dictate.py
```

Permissions werden dann an die Terminal-Binary gebunden statt an `Dictate.app`.
Das Login-Item-Menü ist in diesem Modus deaktiviert, weil `SMAppService`
Bundle-Kontext braucht.

## Limits

- Aufnahmen sind auf 120 s gekappt (`MAX_RECORDING_SECONDS`).
- Abbrechen während Transkription ist *best effort* — MLX lässt sich nicht
  mid-flight stoppen; das Ergebnis wird nur verworfen.
- Beim Einfügen wird die Zwischenablage kurz überschrieben und danach restored
  (best effort, nur Plain Text — Bilder/Rich-Text können verloren gehen).
- Verlauf ist nur in-memory; beim App-Neustart ist er leer.

## Lizenz

MIT — siehe [`LICENSE`](LICENSE).
