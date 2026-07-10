# CLAUDE.md

Guidance for AI agents working in this repo. Written in English; keep code
comments and this file English. Operator docs (README.md) are German. No
em-dashes anywhere.

## Project

Local speech-to-text dictation for macOS, packaged as a menubar app. Runs
100% on-device on Apple Silicon: microphone capture via CoreAudio,
transcription via MLX-Whisper, optional post-processing via a local MLX LLM.
No cloud, no API key. The only network traffic is a one-time model download
from HuggingFace into `~/.cache/huggingface/hub/`.

Core workflow: press `Cmd+Shift+9` (or menubar > Start recording), speak,
press again to stop. The audio is transcribed and pasted at the cursor via a
simulated `Cmd+V`. Pressing the hotkey again while transcribing cancels and
discards the result.

Bundle identity is `ch.hinn.dictate`. The app is `LSUIElement` (no Dock icon,
menubar only). Everything is Apple-Silicon-only (`arm64`, MLX).

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

There is no test suite and no linter config. Verification is manual: run the
app and watch `~/Library/Logs/Dictate.log`, or run directly in a terminal and
read stdout.

## Architecture

Data flow: hotkey -> `Dictation.toggle()` -> `record_audio()` (AVAudioEngine)
-> `transcribe()` (MLX-Whisper) -> `safe_transform()` (mode, optional LLM) ->
ss-normalization -> `insert_text()` (clipboard + simulated Cmd+V).

### dictate.py

The single entrypoint and the bulk of the logic.

- Top-of-file `HIServices.AXIsProcessTrusted` shim: pynput 1.8.2 looks up
  `AXIsProcessTrusted` on `HIServices`, but pyobjc 12.x moved it to
  `ApplicationServices`. Without the shim the pynput listener thread crashes
  on start and the global hotkey silently never fires. Must run before
  `from pynput...`.
- `transcribe(audio, language)`: calls `mlx_whisper.transcribe` under the
  shared `MLX_LOCK` (from modes.py). MLX is not thread-safe for concurrent
  GPU eval, so Whisper and the LLM never run at the same time.
- `insert_text(text)`: saves the clipboard, writes the text, simulates Cmd+V
  via `pynput.Controller`, then restores the old clipboard (best effort,
  plain text only). `PASTE_DELAY_AFTER` (0.40 s) is deliberately generous
  because slow apps (Slack, Notion) drop the paste otherwise.
- Clipboard access uses `NSPasteboard` (via pyobjc, shipped with rumps) with
  a `pbcopy`/`pbpaste` subprocess fallback.
- `Dictation` class: the record/transcribe state machine, states
  `idle -> recording -> transcribing -> idle`. Each cycle runs in a daemon
  worker thread. Two mechanisms guard concurrency correctness:
  - `_stop_event` / `_cancel_event`: per-worker `threading.Event`s the
    instance re-points at the current worker so `toggle()` can signal it.
  - `_generation` counter: bumped on every `_start()`. A worker checks
    `is_stale()` (its gen != current gen) before mutating UI state or
    pasting. This prevents an abandoned worker (e.g. a still-running
    cancelled transcription) from stomping live state and leaving the app
    "stuck". This generation guard was the fix for the stuck-recording bug;
    do not remove it.
  - `_stop()` also force-resets to idle if a stop was already requested but
    the worker never returned, so a hung engine cannot wedge the keyboard.
- `_run()` worker guards against bad audio before transcribing: rejects
  recordings shorter than 0.3 s, and rejects `rms < 1e-5` (digital silence
  from a dead/virtual input device), because Whisper hallucinates
  training-data phrases on silence.
- Swiss-German normalization: `text.replace("ß","ss").replace("ẞ","SS")`
  applied last, after any mode, so it catches both raw Whisper output and
  LLM output. Swiss German does not use the eszett.
- `run_listener()`: the global hotkey. Uses pynput's macOS-only
  `darwin_intercept` (Quartz hook) to both detect AND suppress the event, so
  the focused app never sees `Cmd+Shift+9` and macOS does not play the
  system alert beep. Matching is by virtual-key-code + exact modifier mask
  (`_MACOS_VK` maps chars to physical vk), which is keyboard-layout
  independent. This fixes the German QWERTZ bug where `Shift+9` yields `(`.
  `_run_listener_fallback()` (char-based, no suppression) is used only when
  Quartz cannot be imported.
- `check_accessibility_permission()` / `check_input_monitoring_permission()`:
  TCC diagnostics printed at startup. Accessibility is required to simulate
  Cmd+V; Input Monitoring (queried via `IOHIDCheckAccess`) is a separate TCC
  category required for the listener to receive real key events. Missing
  either fails silently at runtime, hence the explicit startup logging.
- Login item helpers (`login_item_status`, `set_login_item`): shell out to
  the Swift launcher binary, because `SMAppService` needs `Bundle.main` to be
  the `.app`, which a Python child process is not.
- `DictateApp(rumps.App)`: builds the menu (Start/Stop toggle, History,
  Mode, Language, Play sounds, Launch at login, model info, Quit). Menu items
  have no `key=` equivalents because NSStatusItem key equivalents only fire
  while the menu is open; the real hotkey is the pynput listener. Starts the
  listener thread and a background model preload thread. The "Launch at
  login" item is omitted entirely when the launcher binary is absent.
- `main()`: `--download` warms up the model and exits; otherwise runs the
  menubar app.

### audio.py

Microphone capture via AVAudioEngine, deliberately not sounddevice/PortAudio.
PortAudio snapshots the device list once per process (no hotplug on macOS), so
a long-running app keeps recording from a stale default device after inputs
change (Teams virtual audio, iPhone continuity mic, USB webcams), yielding
silence. AVAudioEngine resolves the current default input on every start.

- A fresh engine is created and discarded per recording.
- `mic_authorization_status()` / `ensure_mic_access()`: TCC microphone state
  and prompt via `AVCaptureDevice`. A denial yields silent zero-filled audio,
  never an error, so this is checked explicitly.
- `record_audio(stop_event, ...)`: installs a tap on the input node at the
  node's native format (a mismatched tap format raises an NSException inside
  CoreAudio), accumulates float32 chunks under a lock, then resamples once at
  the end. Wrapped in `objc.autorelease_pool()` because worker threads have
  no autorelease pool. Raises `MicPermissionError` / `AudioEngineError`
  instead of returning zeros.
- `_with_watchdog()`: runs each engine start/stop on a disposable daemon
  thread with a 5 s timeout. If CoreAudio wedges, the engine is abandoned
  rather than hanging the worker; the next recording builds a fresh one.
- `_resample()`: windowed-sinc anti-alias FIR + linear interpolation, native
  rate (e.g. 48 kHz) down to 16 kHz for Whisper. Done once on the full
  buffer to avoid a scipy dependency and the fragile PyObjC bridging of
  `AVAudioConverter`.

### modes.py

Post-processing applied between raw transcription and paste. Modes are
mutually exclusive, selected at runtime via the menubar.

- `MLX_LOCK`: the single process-wide lock serializing ALL MLX work (LLM load
  and generate here, Whisper transcription in dictate.py). Imported by
  dictate.py. MLX is not thread-safe for concurrent GPU eval (mlx#2133).
- `_ensure_llm()` / `preload_llm()` / `run_llm()`: lazy-loaded local LLM
  (`LLM_MODEL`), cached after first load. `preload_llm()` is fired on a
  background thread when the user switches to an LLM-backed mode so the first
  recording does not pay the cold start (or first-run download).
- `Mode` base class, `PlainMode` (no-op default), `TranslateMode`
  (any language -> English via the LLM). `MODES` is the ordered registry the
  menu renders; `MODES[0]` (Plain) is the default.
- `safe_transform(mode, text)`: applies the mode but always falls back to the
  original text on any error or empty output. A failed LLM download or OOM
  must never silently lose a transcription.
- Translate forces Whisper into auto-detect (see dictate.py `_run`, `lang =
  None if mode.id == "translate"`), so speaking English while Language is set
  to German does not produce garbled forced-German before the LLM sees it.

### launcher/Dictate.swift + launcher/build.sh

Native Swift launcher, the bundle's `CFBundleExecutable`. Not a shell script
on purpose: macOS TCC tracks bundle identity through the process chain, and a
shell script loses that identity as soon as it `exec`s, so the Permissions
panes would list `uv` or `python` instead of `Dictate`. The native binary
keeps the identity; the Python child inherits it via posix_spawn.

- Normal mode: resolves an absolute `uv` path (GUI apps do not inherit shell
  PATH; `uvCandidates` lists the standard locations), then runs
  `uv run python dictate.py` from the project dir (parent of the `.app`),
  redirecting child stdout/stderr to `~/Library/Logs/Dictate.log` with
  `PYTHONUNBUFFERED=1`.
- CLI modes `--login-item-status` / `--register-login-item` /
  `--unregister-login-item`: SMAppService login item management (macOS 13+),
  called by dictate.py.
- `build.sh`: `swiftc -O -target arm64-apple-macos13`, ad-hoc codesign,
  `lsregister`. The binary is gitignored and must be rebuilt on every
  checkout and after any edit to `Dictate.swift`.

## Constraints

- Apple Silicon + macOS 13+ only. MLX has no CUDA/Intel path.
- Read-only-by-default posture from the workspace applies: do not push config,
  do not add cloud calls, keep everything on-device.
- No credentials anywhere; there are none to add.
- Keep all MLX inference (Whisper + LLM) behind `MLX_LOCK`. Any new code path
  that calls into MLX must take the lock.
- Any worker that mutates UI state or pastes must respect the `is_stale()` /
  generation guard in `Dictation`.
- New tap installs must use the input node's native format, never a forced
  16 kHz mono format.

## Technical notes / gotchas

- TCC grants on this unsigned app are path-bound AND cdhash-bound. Moving or
  renaming the `.app` (or a parent folder) silently invalidates Microphone /
  Accessibility / Input Monitoring, with no re-prompt. So does rebuilding via
  `build.sh` (fresh cdhash). Fix each time: remove and re-add `Dictate.app`
  in the three Privacy panes, then restart. Nuclear reset:
  `tccutil reset ListenEvent ch.hinn.dictate` (and `Accessibility`,
  `Microphone`).
- Three separate TCC categories are needed and granting one does not grant
  another: Microphone (capture), Accessibility (simulate Cmd+V), Input
  Monitoring (global hotkey). Startup logs which are missing.
- History is in-memory only (`deque`, size `HISTORY_SIZE`), cleared on
  restart. Clicking a history entry copies to clipboard; it does not
  auto-paste.
- Cancel-during-transcription is best-effort: MLX cannot be stopped
  mid-generation, so the result is computed and then discarded.
- Runtime tunables are constants at the top of dictate.py (`MODEL`,
  `DEFAULT_LANGUAGE`, `MAX_RECORDING_SECONDS`, `HISTORY_SIZE`,
  `HOTKEY_MODIFIERS`, `HOTKEY_TRIGGER`) and `LLM_MODEL` in modes.py. Any
  `mlx-community/*` model works.

## Known issues / doc drift

- LLM download size is now consistent at ~5 GB across `modes.py`, `dictate.py`
  and README.md (`Meta-Llama-3.1-8B-Instruct-4bit`, roughly 4.5 to 5 GB).
- The project was moved from `/Users/hinn/code/claude/dictate` to
  `/Users/hinn/code/personal/tools/dictate`. The stale paths in
  `launcher/build.sh` and `.claude/settings.local.json` have been corrected,
  but the move itself (new path AND new parent folder, see the TCC note above)
  will have invalidated the Microphone / Accessibility / Input Monitoring
  grants. Re-add `Dictate.app` in the three Privacy panes if the global hotkey
  or paste-into-focused-app stopped working.
