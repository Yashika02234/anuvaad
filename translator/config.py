"""Settings from environment variables + provider factories.

Every stage is swappable with one env var, so you can run on free tiers:
    STT_PROVIDER = groq (Whisper, free tier) | sarvam (Indian-English model)
    LLM_PROVIDER = gemini (free tier)        | groq (Llama, free tier)
    TTS_PROVIDER = elevenlabs                | sarvam (Bulbul, Indian voices)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

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
    groq_llm_model: str = _env("GROQ_LLM_MODEL", "openai/gpt-oss-120b")
    gemini_model: str = _env("GEMINI_MODEL", "")  # empty = LiveKit plugin default (a Flash model)
    eleven_voice_id: str = _env("ELEVEN_VOICE_ID", "")
    eleven_model: str = _env("ELEVEN_MODEL", "eleven_flash_v2_5")  # lowest-latency, speaks Hindi
    sarvam_speaker: str = _env("SARVAM_SPEAKER", "")
    tts_speed: float = float(_env("TTS_SPEED", "1.05"))  # slightly faster speech helps latency

    # turn taking
    turn_detector: str = _env("TURN_DETECTOR", "multilingual").lower()  # multilingual | vad
    min_silence: float = float(_env("VAD_MIN_SILENCE", "0.45"))
    min_endpoint_delay: float = float(_env("MIN_ENDPOINT_DELAY", "0.4"))
    max_endpoint_delay: float = float(_env("MAX_ENDPOINT_DELAY", "2.5"))
    incomplete_hold_s: float = float(_env("INCOMPLETE_HOLD_SECONDS", "1.2"))
    # "interrupt": user speech stops Hindi playback (barge-in, default)
    # "queue":     interpreter style, translations queue up and are never cut off
    overlap_mode: str = _env("OVERLAP_MODE", "interrupt").lower()

    glossary_path: Path = Path(_env("GLOSSARY_PATH", str(ROOT / "glossary.json")))


SETTINGS = Settings()


def make_stt(s: Settings, vocab_prompt: str):
    if s.stt_provider == "sarvam":
        # Sarvam's model is trained on Indian-accented speech: good for strong accents.
        return sarvam.STT(language="en-IN", prompt=vocab_prompt)
    # The prompt biases Whisper toward the right spelling of names (Google Meet, Asterisk).
    return groq.STT(model=s.groq_stt_model, language="en", prompt=vocab_prompt)


def make_llm(s: Settings):
    if s.llm_provider == "groq":
        kwargs = {"model": s.groq_llm_model, "temperature": 0.2}
        if "gpt-oss" in s.groq_llm_model:
            # reasoning model: keep thinking minimal so the first Hindi token comes fast
            kwargs["reasoning_effort"] = "low"
        return groq.LLM(**kwargs)
    kwargs = {"temperature": 0.2}
    if s.gemini_model:
        kwargs["model"] = s.gemini_model
    return google.LLM(**kwargs)


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
