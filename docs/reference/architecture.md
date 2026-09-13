<!-- no-umlaut-check: this file quotes a source line whose literal IS the eszett
     character (the Swiss-German normalization in dictate.py). The guard would
     otherwise block every edit to this file. Not a licence for the prose here. -->

# Architecture

Split out of `CLAUDE.md` on 2026-09-13, content unchanged.

Data flow: hotkey -> `Dictation.toggle()` -> `record_audio()` (AVAudioEngine) ->
`transcribe()` (MLX-Whisper) -> `safe_transform()` (mode, optional LLM) -> ss-normalization ->
`insert_text()` (clipboard + simulated Cmd+V).

## dictate.py

The single entrypoint and the bulk of the logic.

- Top-of-file `HIServices.AXIsProcessTrusted` shim: pynput 1.8.2 looks up
  `AXIsProcessTrusted` on `HIServices`. pyobjc 12.2.1 exposed it only on
  `ApplicationServices`, and without the shim the pynput listener thread crashed on start, so
  the global hotkey silently never fired. pyobjc 12.2.2 (current pin) has it on `HIServices`
  again, so the shim is a no-op today. Keep it: it costs nothing and it is the difference
  between a working hotkey and a silent failure if the symbol moves again. Must run before
  `from pynput...`.
- `transcribe(audio, language)`: calls `mlx_whisper.transcribe` under the shared `MLX_LOCK`
  (from modes.py). MLX is not thread-safe for concurrent GPU eval, so Whisper and the LLM
  never run at the same time.
- `insert_text(text)`: saves the clipboard, writes the text, simulates Cmd+V via
  `pynput.Controller`, then restores the old clipboard (best effort, plain text only).
  `PASTE_DELAY_AFTER` (0.40 s) is deliberately generous because slow apps (Slack, Notion) drop
  the paste otherwise.
- Clipboard access uses `NSPasteboard` (via pyobjc, shipped with rumps) with a
  `pbcopy`/`pbpaste` subprocess fallback.
- `Dictation` class: the record/transcribe state machine, states
  `idle -> recording -> transcribing -> idle`. Each cycle runs in a daemon worker thread. Two
  mechanisms guard concurrency correctness:
  - `_stop_event` / `_cancel_event`: per-worker `threading.Event`s the instance re-points at
    the current worker so `toggle()` can signal it.
  - `_generation` counter: bumped on every `_start()`. A worker checks `is_stale()` (its gen
    != current gen) before mutating UI state or pasting. This prevents an abandoned worker
    (e.g. a still-running cancelled transcription) from stomping live state and leaving the
    app "stuck". This generation guard was the fix for the stuck-recording bug; do not remove
    it.
  - `_stop()` also force-resets to idle if a stop was already requested but the worker never
    returned, so a hung engine cannot wedge the keyboard.
- `_run()` worker guards against bad audio before transcribing: rejects recordings shorter
  than 0.3 s, and rejects `rms < 1e-5` (digital silence from a dead or virtual input device),
  because Whisper hallucinates training-data phrases on silence.
- Swiss-German normalization: the eszett is replaced by `ss` (and its capital form by `SS`)
  as the very last step, after any mode, so it catches both raw Whisper output and LLM output.
  Swiss German does not use the eszett.
- `run_listener()`: the global hotkey. Uses pynput's macOS-only `darwin_intercept` (Quartz
  hook) to both detect AND suppress the event, so the focused app never sees `Cmd+Shift+9` and
  macOS does not play the system alert beep. Matching is by virtual-key-code + exact modifier
  mask (`_MACOS_VK` maps chars to physical vk), which is keyboard-layout independent. This
  fixes the German QWERTZ bug where `Shift+9` yields `(`. `_run_listener_fallback()`
  (char-based, no suppression) is used only when Quartz cannot be imported.
- `check_accessibility_permission()` / `check_input_monitoring_permission()`: TCC diagnostics
  printed at startup. Accessibility is required to simulate Cmd+V; Input Monitoring (queried
  via `IOHIDCheckAccess`) is a separate TCC category required for the listener to receive real
  key events. Missing either fails silently at runtime, hence the explicit startup logging.
- Login item helpers (`login_item_status`, `set_login_item`): shell out to the Swift launcher
  binary, because `SMAppService` needs `Bundle.main` to be the `.app`, which a Python child
  process is not.
- `DictateApp(rumps.App)`: builds the menu (Start/Stop toggle, History, Mode, Language, Play
  sounds, Launch at login, model info, Quit). Menu items have no `key=` equivalents because
  NSStatusItem key equivalents only fire while the menu is open; the real hotkey is the pynput
  listener. Starts the listener thread and a background model preload thread. The "Launch at
  login" item is omitted entirely when the launcher binary is absent.
- `main()`: `--download` warms up the model and exits; otherwise runs the menubar app.

## audio.py

Microphone capture via AVAudioEngine, deliberately not sounddevice/PortAudio. PortAudio
snapshots the device list once per process (no hotplug on macOS), so a long-running app keeps
recording from a stale default device after inputs change (Teams virtual audio, iPhone
continuity mic, USB webcams), yielding silence. AVAudioEngine resolves the current default
input on every start.

- A fresh engine is created and discarded per recording.
- `mic_authorization_status()` / `ensure_mic_access()`: TCC microphone state and prompt via
  `AVCaptureDevice`. A denial yields silent zero-filled audio, never an error, so this is
  checked explicitly.
- `record_audio(stop_event, ...)`: installs a tap on the input node at the node's native
  format (a mismatched tap format raises an NSException inside CoreAudio), accumulates float32
  chunks under a lock, then resamples once at the end. Wrapped in `objc.autorelease_pool()`
  because worker threads have no autorelease pool. Raises `MicPermissionError` /
  `AudioEngineError` instead of returning zeros.
- `_with_watchdog()`: runs each engine start/stop on a disposable daemon thread with a 5 s
  timeout. If CoreAudio wedges, the engine is abandoned rather than hanging the worker; the
  next recording builds a fresh one.
- `_resample()`: windowed-sinc anti-alias FIR + linear interpolation, native rate (e.g. 48
  kHz) down to 16 kHz for Whisper. Done once on the full buffer to avoid a scipy dependency
  and the fragile PyObjC bridging of `AVAudioConverter`.

## modes.py

Post-processing applied between raw transcription and paste. Modes are mutually exclusive,
selected at runtime via the menubar.

- `MLX_LOCK`: the single process-wide lock serializing ALL MLX work (LLM load and generate
  here, Whisper transcription in dictate.py). Imported by dictate.py. MLX is not thread-safe
  for concurrent GPU eval (mlx#2133).
- `_ensure_llm()` / `preload_llm()` / `run_llm()`: lazy-loaded local LLM (`LLM_MODEL`), cached
  after first load. `preload_llm()` is fired on a background thread when the user switches to
  an LLM-backed mode so the first recording does not pay the cold start (or first-run
  download).
- `Mode` base class, `PlainMode` (no-op default), `TranslateMode` (any language to English via
  the LLM). `MODES` is the ordered registry the menu renders; `MODES[0]` (Plain) is the
  default.
- `safe_transform(mode, text)`: applies the mode but always falls back to the original text on
  any error or empty output. A failed LLM download or OOM must never silently lose a
  transcription.
- Translate forces Whisper into auto-detect (see dictate.py `_run`,
  `lang = None if mode.id == "translate"`), so speaking English while Language is set to
  German does not produce garbled forced-German before the LLM sees it.

## launcher/Dictate.swift + launcher/build.sh

Native Swift launcher, the bundle's `CFBundleExecutable`. Not a shell script on purpose: macOS
TCC tracks bundle identity through the process chain, and a shell script loses that identity
as soon as it `exec`s, so the Permissions panes would list `uv` or `python` instead of
`Dictate`. The native binary keeps the identity; the Python child inherits it via posix_spawn.

- Normal mode: resolves an absolute `uv` path (GUI apps do not inherit shell PATH;
  `uvCandidates` lists the standard locations), then runs `uv run python dictate.py` from the
  project dir (parent of the `.app`), redirecting child stdout/stderr to
  `~/Library/Logs/Dictate.log` with `PYTHONUNBUFFERED=1`.
- CLI modes `--login-item-status` / `--register-login-item` / `--unregister-login-item`:
  SMAppService login item management (macOS 13+), called by dictate.py.
- `build.sh`: `swiftc -O -target arm64-apple-macos13`, ad-hoc codesign, `lsregister`. The
  binary is gitignored and must be rebuilt on every checkout and after any edit to
  `Dictate.swift`.
