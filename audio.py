"""AVAudioEngine-based microphone capture (replaces sounddevice/PortAudio).

Why not PortAudio: it snapshots the device list once per process (no
hotplug on macOS), so a long-running app keeps recording from a stale
default device after inputs change (Teams virtual audio, iPhone
continuity mic, USB webcams) -- yielding pure silence that Whisper
hallucinates on. Its stream teardown can also deadlock against
CoreAudio. AVAudioEngine resolves the *current* default input on every
engine start and has neither problem; it's what native dictation apps
(VoiceInk, Superwhisper) use.

A fresh engine is created per recording and discarded afterwards.
"""

from __future__ import annotations

import sys
import threading
import time

import numpy as np
import objc
from AVFoundation import AVAudioEngine, AVCaptureDevice, AVMediaTypeAudio

# Watchdog limit for any single engine start/stop call. If CoreAudio
# wedges, we abandon the engine instead of hanging the worker forever.
_ENGINE_OP_TIMEOUT = 5.0


class MicPermissionError(RuntimeError):
    """Microphone access denied/restricted by TCC."""


class AudioEngineError(RuntimeError):
    """AVAudioEngine failed or blocked; the engine was abandoned."""


# ============================================================================
# TCC (microphone permission)
# ============================================================================

def mic_authorization_status() -> str:
    """One of: not-determined, restricted, denied, authorized, unknown."""
    # AVAuthorizationStatus: 0=notDetermined 1=restricted 2=denied 3=authorized
    st = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio)
    return {
        0: "not-determined",
        1: "restricted",
        2: "denied",
        3: "authorized",
    }.get(int(st), "unknown")


def ensure_mic_access(timeout: float = 30.0) -> bool:
    """True if mic access is authorized; triggers the TCC prompt on first use.

    The generous timeout covers the user clicking through the system
    dialog. denied/restricted return False immediately.
    """
    st = mic_authorization_status()
    if st == "authorized":
        return True
    if st in ("denied", "restricted"):
        return False

    done = threading.Event()
    granted = [False]

    def completion(ok: bool) -> None:  # bridged to an ObjC block by PyObjC
        granted[0] = bool(ok)
        done.set()

    AVCaptureDevice.requestAccessForMediaType_completionHandler_(
        AVMediaTypeAudio, completion
    )
    done.wait(timeout)
    return granted[0]


def current_input_device_name() -> str:
    try:
        dev = AVCaptureDevice.defaultDeviceWithMediaType_(AVMediaTypeAudio)
        return str(dev.localizedName()) if dev is not None else "none"
    except Exception:  # noqa: BLE001
        return "unknown"


# ============================================================================
# Capture
# ============================================================================

def _with_watchdog(label: str, fn, timeout: float = _ENGINE_OP_TIMEOUT) -> None:
    """Run a potentially-blocking engine call on a disposable thread.

    On timeout the thread (and the engine it touched) is abandoned --
    daemon threads don't block process exit, and the next recording
    builds a fresh engine, so one wedged engine can't poison the app.
    """
    exc: list[BaseException | None] = [None]

    def runner() -> None:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            exc[0] = e

    t = threading.Thread(target=runner, daemon=True, name=f"audio-{label}")
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AudioEngineError(f"{label} blocked > {timeout}s; engine abandoned")
    if exc[0] is not None:
        raise exc[0]


def record_audio(
    stop_event: threading.Event,
    sample_rate: int = 16_000,
    max_seconds: float = 120.0,
) -> np.ndarray:
    """Record from the CURRENT default input until stop_event (or cap).

    Returns mono float32 at `sample_rate`. Raises MicPermissionError /
    AudioEngineError instead of silently recording zeros.
    """
    if not ensure_mic_access():
        raise MicPermissionError(
            "Microphone access denied. Fix: System Settings > Privacy & "
            "Security > Microphone, enable Dictate, restart the app."
        )

    # Worker threads have no NSAutoreleasePool; without one, autoreleased
    # ObjC objects from the calls below would leak.
    with objc.autorelease_pool():
        print(f"[audio] recording from: {current_input_device_name()}")
        engine = AVAudioEngine.alloc().init()
        input_node = engine.inputNode()  # resolves the current default input
        fmt = input_node.outputFormatForBus_(0)  # native HW format
        native_rate = float(fmt.sampleRate())
        if native_rate <= 0 or int(fmt.channelCount()) == 0:
            raise AudioEngineError(
                "input node has no usable format (no input device?)"
            )

        chunks: list[np.ndarray] = []
        lock = threading.Lock()

        def tap(buf, _when) -> None:  # (AVAudioPCMBuffer, AVAudioTime)
            try:
                frames = int(buf.frameLength())
                if frames == 0:
                    return
                # pyobjc 12.x: tuple of per-channel objc.varlist; varlist
                # .as_buffer() takes an ITEM count (float32 items), not bytes.
                ptrs = buf.floatChannelData()
                raw = ptrs[0].as_buffer(frames)  # channel 0
                # Copy is mandatory: CoreAudio recycles the buffer.
                mono = np.frombuffer(raw, dtype=np.float32).copy()
                with lock:
                    chunks.append(mono)
            except Exception as e:  # noqa: BLE001
                print(f"[audio] tap error: {e}", file=sys.stderr)

        # The tap format on the macOS input node MUST be the node's own
        # format (or None) -- a mismatched format (e.g. 16 kHz mono)
        # raises an NSException inside CoreAudio. bufferSize is advisory.
        input_node.installTapOnBus_bufferSize_format_block_(0, 1024, fmt, tap)
        try:
            def _start() -> None:
                engine.prepare()
                ok, err = engine.startAndReturnError_(None)
                if not ok:
                    raise AudioEngineError(f"AVAudioEngine start failed: {err}")

            _with_watchdog("engine-start", _start)

            t0 = time.monotonic()
            while not stop_event.is_set():
                if time.monotonic() - t0 > max_seconds:
                    print(f"[recorder] {max_seconds:.0f}s cap reached, stopping.")
                    break
                time.sleep(0.05)
        finally:
            def _teardown() -> None:
                try:
                    input_node.removeTapOnBus_(0)
                finally:
                    engine.stop()

            _with_watchdog("engine-stop", _teardown)

        with lock:
            if not chunks:
                return np.zeros(0, dtype=np.float32)
            native = np.concatenate(chunks)

    return _resample(native, native_rate, sample_rate)


# ============================================================================
# Resampling (native rate, e.g. 48 kHz -> 16 kHz for Whisper)
# ============================================================================

def _resample(x: np.ndarray, in_rate: float, out_rate: int) -> np.ndarray:
    """Windowed-sinc anti-alias filter + linear interpolation.

    Done once on the full recording (~23 MB max at 48 kHz / 120 s), not
    per tap buffer. Plenty of quality headroom for Whisper's 16 kHz
    log-mel front end; avoids a scipy dependency and the fragile PyObjC
    bridging of AVAudioConverter's input-block API.
    """
    if len(x) == 0 or abs(in_rate - out_rate) < 1.0:
        return x.astype(np.float32, copy=False)
    # Anti-alias FIR, cutoff a bit under the target Nyquist (7.2 kHz @ 16 k).
    cutoff = 0.45 * out_rate
    numtaps = 101
    t = np.arange(numtaps) - (numtaps - 1) / 2
    h = np.sinc(2 * cutoff / in_rate * t) * np.hamming(numtaps)
    h /= h.sum()
    filtered = np.convolve(x, h, mode="same")
    n_out = int(round(len(x) * out_rate / in_rate))
    src_t = np.arange(len(x)) / in_rate
    dst_t = np.arange(n_out) / out_rate
    return np.interp(dst_t, src_t, filtered).astype(np.float32)
