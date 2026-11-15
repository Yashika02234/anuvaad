"""Settings from environment variables + provider factories.

Every stage is swappable with one env var, so you can run on free tiers:
    STT_PROVIDER = groq (Whisper, free tier) | sarvam (Indian-English model)
    LLM_FALLBACK_ORDER = gpt-oss-120b,qwen,gpt-oss-20b,gemini  (first available model answers)
    TTS_PROVIDER = elevenlabs                | sarvam (Bulbul, Indian voices)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import llm

# LiveKit plugins must be imported (registered) on the main thread at startup.
# Importing them lazily inside a job crashes on Windows: "Plugins must be registered
# on the main thread".
from livekit.plugins import elevenlabs, google, groq, sarvam  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass
class Settings:
    stt_provider: str = _env("STT_PROVIDER", "groq").lower()
    llm_provider: str = _env("LLM_PROVIDER", "groq").lower()
    tts_provider: str = _env("TTS_PROVIDER", "elevenlabs").lower()

    groq_stt_model: str = _env("GROQ_STT_MODEL", "whisper-large-v3-turbo")
    # LLM fallback order (translator/models.py): when a model is busy (rate limit) or slow,
    # the next one answers. Each provider/model has its own free-tier limits. A model picked
    # in the web page moves to the front; the rest keep this order.
    # gpt-oss-120b first, then Qwen (fastest, ~0.5 s per reply on Groq); Gemini last (~1.3 s,
    # lower free limits)
    llm_fallback_order: str = _env("LLM_FALLBACK_ORDER", "gpt-oss-120b,qwen,gpt-oss-20b,gemini")
    llm_max_tokens: int = int(_env("LLM_MAX_TOKENS", "200"))  # cap per reply (ACTION/EN/HINDI)
    # gpt-oss always thinks a little ("low" is its minimum) and those tokens count against the
    # cap: 200 cut long sentences off before the Hindi line (167 thinking + 33 answer tokens)
    llm_max_tokens_reasoning: int = int(_env("LLM_MAX_TOKENS_REASONING", "300"))
    # 3.5: works with minimal thinking (~1.2 s to first token). 3.8 rejects "minimal" (use
    # GEMINI_THINKING_LEVEL=low) and was often overloaded (503/504) when tested.
    gemini_model: str = _env("GEMINI_MODEL", "gemini-3.5-flash")
    gemini_thinking_level: str = _env("GEMINI_THINKING_LEVEL", "minimal")
    eleven_voice_id: str = _env("ELEVEN_VOICE_ID", "")
    eleven_model: str = _env("ELEVEN_MODEL", "eleven_flash_v2_5")  # lowest-latency, speaks Hindi
    sarvam_speaker: str = _env("SARVAM_SPEAKER", "")
    # Whisper noise filter (translator/stt.py): drop segments Whisper flags as non-speech
    stt_no_speech_prob: float = float(_env("STT_NO_SPEECH_PROB", "0.6"))
    stt_min_logprob: float = float(_env("STT_MIN_LOGPROB", "-1.0"))
    stt_max_compression: float = float(_env("STT_MAX_COMPRESSION", "2.4"))
    tts_speed: float = float(_env("TTS_SPEED", "1.05"))  # slightly faster speech helps latency

    # turn taking
    turn_detector: str = _env("TURN_DETECTOR", "multilingual").lower()  # multilingual | vad
    min_silence: float = float(_env("VAD_MIN_SILENCE", "0.45"))
    min_endpoint_delay: float = float(_env("MIN_ENDPOINT_DELAY", "0.8"))
    # ceiling when the turn detector thinks the sentence is unfinished; half sentences that
    # still get through are completed by the LLM on the next turn (prompt: Continuation)
    max_endpoint_delay: float = float(_env("MAX_ENDPOINT_DELAY", "2.5"))
    # "interrupt": user speech stops Hindi playback (barge-in, default)
    # "queue":     interpreter style, translations queue up and are never cut off
    overlap_mode: str = _env("OVERLAP_MODE", "interrupt").lower()


SETTINGS = Settings()


def make_stt(s: Settings):
    # No vocabulary prompt: Whisper echoed it back on silence, and the LLM now fixes
    # mis-heard names itself ("google meat" -> Google Meet).
    if s.stt_provider == "sarvam":
        # Sarvam's model is trained on Indian-accented speech: good for strong accents.
        return sarvam.STT(language="en-IN")
    from .stt import ConfidentGroqSTT  # Whisper + its own confidence as a noise filter

    return ConfidentGroqSTT(
        model=s.groq_stt_model, language="en", no_speech_prob=s.stt_no_speech_prob,
        min_logprob=s.stt_min_logprob, max_compression=s.stt_max_compression,
    )


# short name (used by the web page's model menu) -> Groq model id
GROQ_MODELS = {
    "qwen": "qwen/qwen3.8-27b",
    "gpt-oss-20b": "openai/gpt-oss-20b",
    "gpt-oss-120b": "openai/gpt-oss-120b",
}


def make_model(s: Settings, name: str) -> llm.LLM | None:
    """One translation model, or None if it can't be used (unknown name / no API key)."""
    if name in GROQ_MODELS:
        model = GROQ_MODELS[name]
        # replies are ~40-100 tokens; without a cap the plugin asks for 2048, which Groq
        # reserves against the free tier's output-tokens-per-minute limit
        cap = s.llm_max_tokens_reasoning if "gpt-oss" in model else s.llm_max_tokens
        kwargs = {"model": model, "temperature": 0.2, "max_completion_tokens": cap}
        # reasoning models: keep thinking minimal so the first Hindi token comes fast
        kwargs["reasoning_effort"] = "none" if "qwen" in model else "low"  # gpt-oss minimum: low
        return groq.LLM(**kwargs)
    if name == "gemini":
        if not _env("GOOGLE_API_KEY"):
            return None
        return google.LLM(
            model=s.gemini_model, temperature=0.2, max_output_tokens=s.llm_max_tokens,
            thinking_config={"thinking_level": s.gemini_thinking_level},  # as low as the model allows
        )
    return None


def build_model_chains(s: Settings):
    """All usable translation models in LLM_FALLBACK_ORDER, with fallback + recovery."""
    from .models import ModelChains

    models = {}
    for name in (n.strip() for n in s.llm_fallback_order.split(",")):
        m = make_model(s, name) if name and name not in models else None
        if m is not None:
            models[name] = m
    if not models:
        raise ValueError(f"no usable LLM in LLM_FALLBACK_ORDER={s.llm_fallback_order!r}")
    return ModelChains(models, groq_ids=GROQ_MODELS)


def make_tts(s: Settings):
    if s.tts_provider == "sarvam":
        kw = {"target_language_code": "hi-IN", "pace": s.tts_speed}
        if s.sarvam_speaker:
            kw["speaker"] = s.sarvam_speaker
        return sarvam.TTS(**kw)
    kw = {"model": s.eleven_model, "language": "hi"}
    if s.eleven_voice_id:
        kw["voice_id"] = s.eleven_voice_id
    try:
        kw["voice_settings"] = elevenlabs.VoiceSettings(
            stability=0.5, similarity_boost=0.75, speed=s.tts_speed
        )
    except TypeError:  # older plugin without `speed`
        pass
    return elevenlabs.TTS(**kw)
