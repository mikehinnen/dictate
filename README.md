# dictate — local Whisper dictation for macOS

Menubar app for speech-to-text, running 100% locally on Apple Silicon.
No cloud service, no API key — just your microphone and MLX-Whisper.

## Usage

Double-click `Dictate.app` → 🎙 icon appears in the menu bar (top right).

| Action | How |
|---|---|
| Start/stop recording | Hotkey `⌘⇧9` **or** click 🎙 → "Start recording" |
| Cancel transcription | Press the hotkey again while ⏳ is running — result is discarded |
| Pick a post-processing mode | 🎙 → "Mode" → Plain / Emoji / Polish / Friendly |
| Switch language | 🎙 → "Language" → German / English / Auto-detect |
| Copy a past transcription | 🎙 → "History" → click entry (text is put on the clipboard) |
| Toggle start/stop sounds | 🎙 → "Play sounds" (off by default) |
| Launch at login | 🎙 → "Launch at login" (checkbox) |
| See state | Icon: 🎙 idle · 🔴 recording · ⏳ transcribing |
| Quit | 🎙 → "Quit" (or `⌘Q` while the menu is open) |

After each live transcription the text is pasted into the focused window via
`⌘V`. Example flow in TextEdit: place cursor → `⌘⇧9` → speak → `⌘⇧9` → text
appears.

**History entries don't auto-paste** — clicking them just copies the text to
the clipboard so you can `⌘V` it wherever you want.

## Modes (post-processing)

Between Whisper's raw transcription and the paste, you can apply a
transformation. Pick one under 🎙 → "Mode":

| Mode | What it does | Backend |
|---|---|---|
| **Plain** | Paste the raw transcription (default). | none |
| **Emoji** | Replace spoken keywords with emoji — "daumen hoch" → 👍, "feuer" → 🔥, "idee" → 💡, "party" → 🎉. German + English. | Rule-based, offline, instant |
| **Polish** | Clean up dictated speech into polished written text: fix grammar, strip fillers (ähm, halt, also), keep language. | Local LLM |
| **Friendly** | Soften angry / confrontational text into a more diplomatic version, keeping the core message. | Local LLM |

The LLM used for Polish and Friendly is
`mlx-community/Llama-3.2-3B-Instruct-4bit` (~2 GB, runs on Apple Silicon
via MLX). It **downloads lazily on first use** — the first Polish or
Friendly transcription will block for a minute or two while the model
fetches into the HuggingFace cache (`~/.cache/huggingface/hub/`), after
that it's ~1–2 s per transformation. Plain and Emoji never touch the
LLM.

Everything is offline — no cloud calls for any mode.

## First-time setup

```sh
uv sync                                      # install dependencies
uv run python dictate.py --download          # download Whisper model (~1.5 GB)
bash launcher/build.sh                       # compile the Swift launcher
open Dictate.app                             # first launch
```

The **Swift launcher** (`launcher/Dictate.swift`) is a tiny native binary
that lives at `Dictate.app/Contents/MacOS/Dictate`. It spawns `uv run python
dictate.py` as a child process. This matters for macOS TCC: a shell script as
the bundle executable would lose bundle identity on every `exec`, and the
Permissions panes would list `uv` or `python` instead of `Dictate`. A native
binary preserves the identity.

The binary is **not committed** (see `.gitignore`) — every checkout must run
`bash launcher/build.sh` once. Run the same command after editing
`Dictate.swift`.

## macOS permissions (one-time)

On first keystroke / first recording, macOS asks for three permissions. They
bind to **Dictate.app** — *not* to the Terminal.

| Permission | Why | When prompted |
|---|---|---|
| **Microphone** | Audio capture | First recording |
| **Accessibility** | Simulate `⌘V` | First paste |
| **Input Monitoring** | Global hotkey | First launch |

Direct links to the settings panes:
```sh
open "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
```

In each pane: `+` → pick `Dictate.app` from the project folder → toggle on.
Then fully quit the app (menu → Quit) and reopen it.

### If you move or rename the project folder

TCC permissions on **unsigned** apps are path-bound. Moving or renaming the
`.app` (or any parent folder) invalidates the grants silently — macOS keeps
the stale entry, doesn't re-prompt, and the app just silently lacks the
permission. Symptom: the hotkey stops firing, or `⌘V` no longer simulates.

Fix: in each of the three panes above, select the stale `Dictate` entry, hit
`−`, then `+` to re-add it from the new path and toggle it on. Restart the
app once afterwards.

## Customization

Constants at the top of `dictate.py`:

| Constant | Default | Notes |
|---|---|---|
| `MODEL` | `mlx-community/whisper-large-v3-turbo` | try `whisper-large-v3-turbo-german-f16`, `whisper-medium-mlx`, `whisper-small-mlx` |
| `DEFAULT_LANGUAGE` | `"de"` | `"en"`, `None` (auto) — also switchable at runtime via menu |
| `MAX_RECORDING_SECONDS` | `120` | arbitrary |
| `HISTORY_SIZE` | `5` | how many history entries to keep |
| `HOTKEY_MODIFIERS` | `{Key.cmd, Key.shift}` | `⌘`+`⇧` is the most robust combo on macOS |
| `HOTKEY_TRIGGER` | `KeyCode.from_char("9")` | any letter/digit |

### Custom menubar icon (optional)

Drop three PNGs into `Dictate.app/Contents/Resources/` and the app will use
them as template images (auto-adapt to Dark/Light mode):

- `menubar-idle.png`
- `menubar-recording.png`
- `menubar-transcribing.png`

Recommended: 22×22 or 44×44 PNG, black on transparent, alpha channel defines
the shape. macOS inverts them automatically in Dark mode.

## Logs

`Dictate.app` writes to `~/Library/Logs/Dictate.log`. Live tail:

```sh
tail -f ~/Library/Logs/Dictate.log
```

Or open `Console.app`.

## Troubleshooting

- **Icon doesn't appear** → check the log (`tail -50 ~/Library/Logs/Dictate.log`).
  Most common cause: `uv` isn't in `~/.local/bin`, `/opt/homebrew/bin`, or
  `/usr/local/bin`. Add your path to `uvCandidates` in `launcher/Dictate.swift`
  and re-run `bash launcher/build.sh`.
- **Hotkey doesn't fire** → check *Input Monitoring*; `Dictate.app` must be
  in the list and toggled on. Restart the app after granting. If the folder
  was moved or renamed, see *"If you move or rename the project folder"*.
- **`⌘V` doesn't paste after transcription** → *Accessibility* permission
  missing (see above). If it still fails in slow apps (Slack, Notion),
  increase `PASTE_DELAY_AFTER` in `dictate.py`.
- **Wrong German words in the transcript** → switch `MODEL` to
  `mlx-community/whisper-large-v3-turbo-german-f16` (DE-finetuned).
- **First transcription after startup is fast** → the model is preloaded in
  the background; look for `[preload] Model loaded` in the log.
- **Clear the model cache** → `rm -rf ~/.cache/huggingface/hub/models--mlx-community--whisper-*`
- **"Launch at login" doesn't work** → requires macOS 13+. Check status from
  the terminal: `./Dictate.app/Contents/MacOS/Dictate --login-item-status`.
- **Permissions panes list "uv" or "python" instead of "Dictate"** → old
  shell-script launcher still in the bundle. Fix: `bash launcher/build.sh`,
  then remove the stale entries from the three permission panes (select +
  `−`), restart `Dictate.app`.

## Terminal mode (fallback / debugging)

If the .app route acts up, the direct path still works:

```sh
uv run python dictate.py
```

Permissions then bind to the Terminal binary instead of `Dictate.app`. The
"Launch at login" menu item is disabled in this mode because `SMAppService`
needs bundle context.

## Limits

- Recordings are capped at 120 s (`MAX_RECORDING_SECONDS`).
- Cancel-during-transcription is *best effort* — MLX can't be stopped
  mid-flight; the result is simply discarded.
- When pasting, the clipboard is briefly overwritten and then restored
  (best effort, plain text only — images/rich text may be lost).
- The history is in-memory only; it's empty after each app restart.

## License

MIT — see [`LICENSE`](LICENSE).
