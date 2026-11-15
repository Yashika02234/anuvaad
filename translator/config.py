"""Settings from environment variables + provider factories.

Every stage is swappable with one env var, so you can run on free tiers:
    STT_PROVIDER = groq (Whisper, free tier) | sarvam (Indian-English model)
    LLM_PROVIDER = groq (Qwen / gpt-oss, free tier) | gemini (free tier)
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
    # Qwen with thinking off: 48/50 on the prompt-lab cases at ~0.5 s per reply
    groq_llm_model: str = _env("GROQ_LLM_MODEL", "qwen/qwen3.8-27b")
    llm_max_tokens: int = int(_env("LLM_MAX_TOKENS", "200"))  # cap per reply (ACTION/EN/HINDI)
    gemini_model: str = _env("GEMINI_MODEL", "")  # empty = LiveKit plugin default (a Flash model)
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


def make_llm(s: Settings):
    if s.llm_provider == "groq":
        # replies are ~40-100 tokens; without a cap the plugin asks for 2048, which Groq
        # reserves against the free tier's output-tokens-per-minute limit
        kwargs = {"model": s.groq_llm_model, "temperature": 0.2,
                  "max_completion_tokens": s.llm_max_tokens}
        # reasoning models: keep thinking minimal so the first Hindi token comes fast
        if "gpt-oss" in s.groq_llm_model:
            kwargs["reasoning_effort"] = "low"  # lowest gpt-oss allows
        elif "qwen" in s.groq_llm_model:
            kwargs["reasoning_effort"] = "none"  # Qwen can switch thinking off fully
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
