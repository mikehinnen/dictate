# Technical notes and gotchas

Split out of `CLAUDE.md` on 2026-09-13, content unchanged.

- **TCC grants on this unsigned app are path-bound AND cdhash-bound.** Moving or renaming the
  `.app` (or a parent folder) silently invalidates Microphone / Accessibility / Input
  Monitoring, with no re-prompt. So does rebuilding via `build.sh` (fresh cdhash). Fix each
  time: remove and re-add `Dictate.app` in the three Privacy panes, then restart. Nuclear
  reset: `tccutil reset ListenEvent ch.hinn.dictate` (and `Accessibility`, `Microphone`).
- **Three separate TCC categories are needed and granting one does not grant another:**
  Microphone (capture), Accessibility (simulate Cmd+V), Input Monitoring (global hotkey).
  Startup logs which are missing.
- History is in-memory only (`deque`, size `HISTORY_SIZE`), cleared on restart. Clicking a
  history entry copies to clipboard; it does not auto-paste.
- Cancel-during-transcription is best-effort: MLX cannot be stopped mid-generation, so the
  result is computed and then discarded.
- The `initial_prompt` cuts both ways: it fixes domain spellings, and on audio without speech
  Whisper continues it, so the transcript becomes a mangled re-listing of `VOCABULARY`. Same
  failure class as the silence hallucination. The `rms < 1e-5` guard in `_worker()` does **not**
  cover it: that only catches digital silence, while a long recording of room noise (rms
  ~0.002) passes and got a pasted vocabulary list ending in a "podcast, podcast, ..." loop.
  `transcribe()` now drops segments with `compression_ratio > 2.4` (the loops) and runs with
  `condition_on_previous_text=False` so a loop cannot carry into the next 30 s window. A short
  echo of the list on a speechless window still gets through; `no_speech_prob` cannot catch
  it, it stays at 0.00 with a forced language and a prompt. Keep the list bare terms, never sentences, and under the 224-token prompt
  window. `DICTATE_NO_VOCAB=1` turns it off for an A/B.
- Runtime tunables are constants at the top of dictate.py (`MODEL`, `DEFAULT_LANGUAGE`,
  `VOCABULARY`, `COMPRESSION_RATIO_MAX`, `MAX_RECORDING_SECONDS`, `HISTORY_SIZE`,
  `HOTKEY_MODIFIERS`, `HOTKEY_TRIGGER`, `PASTE_DELAY_BEFORE` / `PASTE_DELAY_AFTER`, `SOUND_START` / `SOUND_STOP` / `SOUND_CANCEL`)
  and `LLM_MODEL` in modes.py. Any `mlx-community/*` model works.
- Menubar icons are emoji by default. If all three of `menubar-idle.png`,
  `menubar-recording.png` and `menubar-transcribing.png` exist in
  `Dictate.app/Contents/Resources/`, `DictateApp` switches to them as template images
  (`_use_png_icons`, logged at startup as `[app] Icons:`). The directory does not exist in the
  committed bundle, so the default path is emoji.
- **A dead global hotkey with a working menubar Start/Stop is usually not a bug in this
  repo:** macOS Secure Input locks the keyboard process-wide.
  `ioreg -l -w 0 | grep kCGSSessionSecureInputPID` names the holder. Seen: Terminal with
  Secure Keyboard Entry on, and a `loginwindow` wedged since boot by Jamf Connect (clears with
  `sudo killall loginwindow`, which logs the GUI session out). **Check this before touching
  listener code.**

## Known issues / doc drift

None open. The two former entries (LLM download size, the move from
`/Users/hinn/code/claude/dictate`) are resolved: sizes are consistent across `modes.py`,
`dictate.py` and README.md, and `launcher/build.sh` derives every path from its own location
instead of hardcoding a checkout. The TCC consequence of that move is not a doc issue, it is
the permanent gotcha documented above.

Doc invariants worth re-checking when the code changes: the constants table in README.md
"Customization", the menu list in the `dictate.py` module docstring, and the model sizes in
`modes.py` / README.md.

⚠️ **Removed on 2026-09-13: an instruction to run `python3 ~/workspace/scripts/gen_agents.py`
after touching `CLAUDE.md`, because `AGENTS.md` was generated from it.** That has been dead
since 2026-08-24: the generator is scoped to `SCOPE = "work"` and there is no `AGENTS.md`
anywhere under `personal/`. The sentence was also missing its closing parenthesis.
