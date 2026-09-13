# CLAUDE.md

Guidance for AI agents working in this repo.

**This repo is the language exception.** Code comments, this file and the operator docs
(README.md) are English, while the workspace default is German: this is a public-shaped GitHub
project whose README is the install and troubleshooting doc. No em-dashes in prose, in any
file. The only em-dashes in the repo are the two empty-slot placeholder glyphs in the History
menu (dictate.py), which are UI typography, not prose.

## Read on demand

- `docs/reference/architecture.md` — the per-module walkthrough (dictate.py, audio.py,
  modes.py, the Swift launcher) and the data flow
- `docs/reference/gotchas.md` — TCC grants, Secure Input, runtime tunables, doc invariants

## Project

Local speech-to-text dictation for macOS, packaged as a menubar app. Runs 100% on-device on
Apple Silicon: microphone capture via CoreAudio, transcription via MLX-Whisper, optional
post-processing via a local MLX LLM. No cloud, no API key. The only network traffic is a
one-time model download from HuggingFace into `~/.cache/huggingface/hub/`.

Core workflow: press `Cmd+Shift+9` (or menubar > Start recording), speak, press again to stop.
The audio is transcribed and pasted at the cursor via a simulated `Cmd+V`. Pressing the hotkey
again while transcribing cancels and discards the result.

Bundle identity is `ch.hinn.dictate`. The app is `LSUIElement` (no Dock icon, menubar only).
Everything is Apple-Silicon-only (`arm64`, MLX).

## Commands

```bash
# First-time setup
uv sync                              # install pinned deps from uv.lock
uv run python dictate.py --download  # preload Whisper model (~1.5 GB)
bash launcher/build.sh               # compile the Swift launcher into the bundle
open Dictate.app                     # launch the menubar app

# Run directly (debugging / fallback; TCC then binds to the terminal, not the app)
uv run python dictate.py

# Hotkey event tracing (prints vk/modifier of every key event to stderr)
DICTATE_KEY_TRACE=1 uv run python dictate.py

# Login item status check (Swift launcher CLI)
./Dictate.app/Contents/MacOS/Dictate --login-item-status

# Logs (only when launched via Dictate.app; the Swift launcher redirects
# child stdout/stderr here)
tail -f ~/Library/Logs/Dictate.log

# Dependency maintenance
uv sync --upgrade                    # bump within pyproject constraints
uv add <pkg>@latest                  # bump one package
```

There is no test suite and no linter config. Verification is manual: run the app and watch
`~/Library/Logs/Dictate.log`, or run directly in a terminal and read stdout.

## Constraints

- Apple Silicon + macOS 13+ only. MLX has no CUDA/Intel path.
- Read-only-by-default posture from the workspace applies: do not push config, do not add
  cloud calls, keep everything on-device.
- No credentials anywhere; there are none to add.
- **Keep all MLX inference (Whisper + LLM) behind `MLX_LOCK`.** Any new code path that calls
  into MLX must take the lock.
- **Any worker that mutates UI state or pastes must respect the `is_stale()` / generation
  guard in `Dictation`.** It was the fix for the stuck-recording bug; do not remove it.
- **New tap installs must use the input node's native format**, never a forced 16 kHz mono
  format. A mismatched tap format raises an NSException inside CoreAudio.
- Rebuilding via `build.sh` invalidates all three TCC grants. Expect to re-add the app in the
  Privacy panes afterwards, see `docs/reference/gotchas.md`.
