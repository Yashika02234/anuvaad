"""Settings from environment variables and the STT / LLM / TTS factories."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import llm

# Plugins must be registered on the main thread at startup.
from livekit.plugins import elevenlabs, google, groq, sarvam  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass
class Settings:
    stt_provider: str = _env("STT_PROVIDER", "groq").lower()  # groq | sarvam
    llm_provider: str = _env("LLM_PROVIDER", "groq").lower()
    tts_provider: str = _env("TTS_PROVIDER", "elevenlabs").lower()  # elevenlabs | sarvam

    groq_stt_model: str = _env("GROQ_STT_MODEL", "whisper-large-v3-turbo")
    # Translation models in fallback order; a model picked in the page moves to the front.
    llm_fallback_order: str = _env("LLM_FALLBACK_ORDER", "gpt-oss-120b,qwen,gpt-oss-20b,gemini")
    llm_max_tokens: int = int(_env("LLM_MAX_TOKENS", "200"))
    # gpt-oss reasoning tokens count against the cap, so it gets more room.
    llm_max_tokens_reasoning: int = int(_env("LLM_MAX_TOKENS_REASONING", "300"))
    gemini_model: str = _env("GEMINI_MODEL", "gemini-3.5-flash")
    gemini_thinking_level: str = _env("GEMINI_THINKING_LEVEL", "minimal")
    eleven_voice_id: str = _env("ELEVEN_VOICE_ID", "")
    eleven_model: str = _env("ELEVEN_MODEL", "eleven_flash_v2_5")
    sarvam_speaker: str = _env("SARVAM_SPEAKER", "")
    # Whisper noise filter thresholds (translator/stt.py)
    stt_no_speech_prob: float = float(_env("STT_NO_SPEECH_PROB", "0.6"))
    stt_min_logprob: float = float(_env("STT_MIN_LOGPROB", "-1.0"))
    stt_max_compression: float = float(_env("STT_MAX_COMPRESSION", "2.4"))
    tts_speed: float = float(_env("TTS_SPEED", "1.05"))

    # turn taking
    turn_detector: str = _env("TURN_DETECTOR", "multilingual").lower()  # multilingual | vad
    min_silence: float = float(_env("VAD_MIN_SILENCE", "0.45"))
    min_endpoint_delay: float = float(_env("MIN_ENDPOINT_DELAY", "0.8"))
    max_endpoint_delay: float = float(_env("MAX_ENDPOINT_DELAY", "2.5"))
    # interrupt: speaking stops the Hindi playback | queue: translations are never cut off
    overlap_mode: str = _env("OVERLAP_MODE", "interrupt").lower()


SETTINGS = Settings()


def make_stt(s: Settings):
    if s.stt_provider == "sarvam":
        return sarvam.STT(language="en-IN")
    from .stt import ConfidentGroqSTT

    return ConfidentGroqSTT(
        model=s.groq_stt_model, language="en", no_speech_prob=s.stt_no_speech_prob,
        min_logprob=s.stt_min_logprob, max_compression=s.stt_max_compression,
    )


# short name (used by the page's model menu) -> Groq model id
GROQ_MODELS = {
    "qwen": "qwen/qwen3.8-27b",
    "gpt-oss-20b": "openai/gpt-oss-20b",
    "gpt-oss-120b": "openai/gpt-oss-120b",
}


def make_model(s: Settings, name: str) -> llm.LLM | None:
    """One translation model, or None if it can't be used (unknown name / no API key)."""
    if name in GROQ_MODELS:
        model = GROQ_MODELS[name]
        cap = s.llm_max_tokens_reasoning if "gpt-oss" in model else s.llm_max_tokens
        kwargs = {"model": model, "temperature": 0.2, "max_completion_tokens": cap}
        kwargs["reasoning_effort"] = "none" if "qwen" in model else "low"
        return groq.LLM(**kwargs)
    if name == "gemini":
        if not _env("GOOGLE_API_KEY"):
            return None
        return google.LLM(
            model=s.gemini_model, temperature=0.2, max_output_tokens=s.llm_max_tokens,
            thinking_config={"thinking_level": s.gemini_thinking_level},
        )
    return None


def build_model_chains(s: Settings):
    """All usable translation models in LLM_FALLBACK_ORDER, with fallback and recovery."""
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
