"""
Post-processing modes for the dictation pipeline.

A Mode takes the raw Whisper transcription text and returns a transformed
version, which is then pasted into the focused app. Modes are selected at
runtime via the menubar and are mutually exclusive.

Built-in modes:
    Plain     -- no transformation (default)
    Emoji     -- replaces keywords with emoji (rule-based, offline, instant)
    Polish    -- converts spoken/dictated text to written text (LLM)
    Friendly  -- softens the tone of rage/angry text (LLM)

LLM backend: mlx-lm with a local 4-bit model (~2 GB). Downloads lazily on
first use of a mode that needs it. No cloud calls.
"""

from __future__ import annotations

import sys
from typing import ClassVar

# ============================================================================
# Local LLM (lazy-loaded, shared across LLM-backed modes)
# ============================================================================

LLM_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"

_llm_model = None
_llm_tokenizer = None
_llm_lock_loaded = False


def _ensure_llm() -> tuple[object, object]:
    """Load the LLM on first call. Subsequent calls return the cached
    model/tokenizer pair. Raises on download/load failure."""
    global _llm_model, _llm_tokenizer, _llm_lock_loaded
    if _llm_lock_loaded:
        return _llm_model, _llm_tokenizer  # type: ignore[return-value]

    import mlx_lm  # noqa: F401  (lazy import; ~1s)

    print(f"[llm] loading {LLM_MODEL} (first use -- may download ~2 GB)...")
    from mlx_lm import load as _mlx_load  # type: ignore[import-not-found]

    model, tokenizer = _mlx_load(LLM_MODEL)
    _llm_model = model
    _llm_tokenizer = tokenizer
    _llm_lock_loaded = True
    print("[llm] ready.")
    return model, tokenizer


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


# ============================================================================
# Plain
# ============================================================================

class PlainMode(Mode):
    id = "plain"
    label = "Plain (no processing)"

    def transform(self, text: str) -> str:
        return text


# ============================================================================
# Emoji -- contextual emoji insertion via LLM
# ============================================================================

# Earlier iteration was rule-based (keyword -> emoji dict). Didn't survive
# real use: German inflection means "herz" doesn't match "Herzen", and
# natural dictation like "Ich liebe dich von ganzem Herzen" expects a ❤️
# somewhere even though no literal emoji keyword appears. Moved to LLM.

_EMOJI_SYSTEM = """You are a text editor. Enhance the user's text with emojis.

Rules:
- Replace explicit emoji-words with the corresponding emoji:
  daumen hoch / thumbs up -> 👍
  daumen runter / thumbs down -> 👎
  herz / heart -> ❤️
  feuer / fire -> 🔥
  lachen / laughing -> 😄
  rakete / rocket -> 🚀
  party -> 🎉
  idee / idea -> 💡
  haken / check -> ✅
  stern / star -> ⭐
- Also insert contextually fitting emojis (0–3 per message) next to relevant words or at sentence ends. Be tasteful, not noisy.
- Do NOT remove, add, rephrase, or translate any other words. Preserve original word forms (including German inflection) exactly.
- Keep punctuation and sentence structure.
- Keep the language of the input (German or English).
- Return ONLY the modified text. No preamble, no quotes, no explanation."""


class EmojiMode(Mode):
    id = "emoji"
    label = "Emoji"

    def transform(self, text: str) -> str:
        # Headroom: each word might get an emoji appended. 4x tokens covers it.
        max_tokens = max(128, len(text.split()) * 4)
        return run_llm(_EMOJI_SYSTEM, text, max_tokens=max_tokens)


# ============================================================================
# Polish -- spoken → written, via LLM
# ============================================================================

_POLISH_SYSTEM = """You are a text polisher. The user dictated the message below into a speech-to-text engine. Convert the raw transcription into polished written text.

Rules:
- Fix grammar, capitalization, and punctuation.
- Remove filler words and hesitations (um, uh, ähm, äh, halt, also, eigentlich, ja, you know).
- Preserve the meaning, register, and language (German or English) EXACTLY as the input. If the input is German, the output must be German.
- Do NOT add new content, rephrase substantially, translate, or summarize.
- Return ONLY the polished text. No preamble, no quotes, no meta-commentary."""


class PolishMode(Mode):
    id = "polish"
    label = "Polish (spoken → written)"

    def transform(self, text: str) -> str:
        # Allow generous headroom (3x input tokens) for punctuation + fixes.
        max_tokens = max(128, len(text.split()) * 4)
        return run_llm(_POLISH_SYSTEM, text, max_tokens=max_tokens)


# ============================================================================
# Friendly -- soften rage / harsh tone
# ============================================================================

_FRIENDLY_SYSTEM = """You are a diplomatic rewriter. The user wrote the message below when frustrated or angry. Rewrite it to be friendlier, more polite, and less confrontational.

Rules:
- Keep the core message and information intact.
- Soften emotional language; remove profanity and insults.
- Do NOT become sycophantic, overly apologetic, or add fake niceties. Stay natural.
- Keep the language (German or English) the same as the input.
- Return ONLY the rewritten text. No preamble, no quotes, no meta-commentary."""


class FriendlyMode(Mode):
    id = "friendly"
    label = "Friendly (soften tone)"

    def transform(self, text: str) -> str:
        max_tokens = max(128, len(text.split()) * 4)
        return run_llm(_FRIENDLY_SYSTEM, text, max_tokens=max_tokens)


# ============================================================================
# Public registry
# ============================================================================

MODES: list[Mode] = [
    PlainMode(),
    EmojiMode(),
    PolishMode(),
    FriendlyMode(),
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
