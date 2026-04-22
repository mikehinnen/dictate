"""
Post-processing modes for the dictation pipeline.

A Mode takes the raw Whisper transcription text and returns a transformed
version, which is then pasted into the focused app. Modes are selected at
runtime via the menubar and are mutually exclusive.

Built-in modes:
    Plain     -- no transformation (default)
    Translate -- translates any source language to English (LLM)

LLM backend: mlx-lm with a local 4-bit model (~2 GB). Downloads lazily on
first use of a mode that needs it. No cloud calls.
"""

from __future__ import annotations

import sys
from typing import ClassVar

# ============================================================================
# Local LLM (lazy-loaded, shared across LLM-backed modes)
# ============================================================================

LLM_MODEL = "mlx-community/Meta-Llama-3.1-8B-Instruct-4bit"

_llm_model = None
_llm_tokenizer = None
_llm_loaded = False


def _ensure_llm() -> tuple[object, object]:
    """Load the LLM on first call. Subsequent calls return the cached
    model/tokenizer pair. Raises on download/load failure."""
    global _llm_model, _llm_tokenizer, _llm_loaded
    if _llm_loaded:
        return _llm_model, _llm_tokenizer  # type: ignore[return-value]

    import mlx_lm  # noqa: F401  (lazy import; ~1s)

    print(f"[llm] loading {LLM_MODEL} (first use -- may download ~2 GB)...")
    from mlx_lm import load as _mlx_load  # type: ignore[import-not-found]

    model, tokenizer = _mlx_load(LLM_MODEL)
    _llm_model = model
    _llm_tokenizer = tokenizer
    _llm_loaded = True
    print("[llm] ready.")
    return model, tokenizer


def preload_llm() -> None:
    """Trigger LLM load. Safe to call repeatedly -- a no-op if already
    loaded. Intended for background warmup when the user switches to an
    LLM-backed mode, so the first real transcription doesn't pay cold-start."""
    try:
        _ensure_llm()
    except Exception as e:  # noqa: BLE001
        print(f"[llm] preload error: {e}", file=sys.stderr)


def run_llm(system: str, user: str, max_tokens: int = 512) -> str:
    """Run a chat completion against the local LLM. Returns the assistant
    response string (trimmed)."""
    from mlx_lm import generate as _mlx_generate  # type: ignore[import-not-found]

    model, tokenizer = _ensure_llm()

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True
    )
    # mlx-lm.generate: synchronous, returns the generated text only
    # (without the prompt). verbose=False silences its progress prints.
    response = _mlx_generate(
        model,
        tokenizer,
        prompt=prompt,
        max_tokens=max_tokens,
        verbose=False,
    )
    return (response or "").strip()


# ============================================================================
# Mode abstraction
# ============================================================================

class Mode:
    """Base class for post-processing modes."""
    id: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def transform(self, text: str) -> str:  # noqa: ARG002
        raise NotImplementedError

    @staticmethod
    def _max_tokens(text: str) -> int:
        """Shared token budget for LLM-backed modes: generous floor so short
        inputs don't truncate mid-word; 6x words so long inputs have headroom
        for punctuation / emoji / restructuring."""
        return max(512, len(text.split()) * 6)


# ============================================================================
# Plain
# ============================================================================

class PlainMode(Mode):
    id = "plain"
    label = "Plain (no processing)"

    def transform(self, text: str) -> str:
        return text


# ============================================================================
# Translate -- any language → English, via LLM
# ============================================================================

_TRANSLATE_SYSTEM = """You are a translator. Your ONLY job is to output English.

The user's text may be in German, Swiss German, English, or another language. Whatever the input language, the output MUST be English — always, no exceptions.

Rules:
- The OUTPUT LANGUAGE IS ENGLISH. Never return German, never return the source text unchanged (unless it is already English).
- Translate meaning, tone, and register (formal vs casual) faithfully. Don't paraphrase more than necessary.
- Keep proper nouns, names, technical terms, code, URLs, and numbers as-is.
- Do NOT add content, commentary, bullet lists, or explanation. Do NOT summarize or shorten.
- Return ONLY the English translation. No preamble, no quotes, no source text, no notes.

Examples:

Input:  "Ich gehe morgen mit meinen Kindern in den Zoo."
Output: "I'm going to the zoo with my kids tomorrow."

Input:  "Kannst du bitte das Meeting auf nächste Woche verschieben?"
Output: "Can you please move the meeting to next week?"

Input:  "Das Projekt läuft gut, aber wir haben noch ein paar offene Fragen."
Output: "The project is going well, but we still have a few open questions."

Input:  "Let me know if that works for you."
Output: "Let me know if that works for you."
"""


class TranslateMode(Mode):
    id = "translate"
    label = "Translate (→ English)"

    def transform(self, text: str) -> str:
        return run_llm(_TRANSLATE_SYSTEM, text, max_tokens=self._max_tokens(text))


# ============================================================================
# Public registry
# ============================================================================

MODES: list[Mode] = [
    PlainMode(),
    TranslateMode(),
]


def safe_transform(mode: Mode, text: str) -> str:
    """Apply a mode with a fallback to the original text on error.
    LLM-backed modes can fail (first-run download interrupted, model
    missing, OOM, etc.); we never want to silently lose the transcription."""
    if mode.id == "plain" or not text:
        return text
    try:
        out = mode.transform(text)
        if not out:
            print(
                f"[mode] {mode.id} returned empty, falling back to original",
                file=sys.stderr,
            )
            return text
        return out
    except Exception as e:  # noqa: BLE001
        print(
            f"[mode] {mode.id} failed ({e}), falling back to original",
            file=sys.stderr,
        )
        return text
