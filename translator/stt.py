"""Groq Whisper with a noise filter based on Whisper's own confidence.

LiveKit's Groq STT asks for the plain "json" format (text only), so every transcript,
noise included, reaches the LLM. This subclass asks for "verbose_json", which gives
per-segment confidence, and drops segments Whisper itself flags as non-speech:

    no_speech_prob > STT_NO_SPEECH_PROB  and  avg_logprob < STT_MIN_LOGPROB   (silence / noise)
    compression_ratio > STT_MAX_COMPRESSION                                  (looped junk: "Me. Me. Me.")

(the same rules Whisper uses internally). If nothing is left, the transcript is empty and
LiveKit ignores it: no turn, no preemptive LLM call, no Groq tokens spent.
"""

from __future__ import annotations

import logging
import math

import openai

try:  # this livekit/openai stack ships httpx as "httpx2" (same import the plugin uses)
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    import httpx
from livekit import rtc
from livekit.agents import APIConnectionError, APIConnectOptions, APIStatusError, APITimeoutError, stt
from livekit.agents.language import LanguageCode
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer
from livekit.plugins import groq

logger = logging.getLogger("translator.stt")


def keep_segment(seg, no_speech: float, min_logprob: float, max_compression: float) -> bool:
    """Whisper's own rules: drop silence/noise segments and repetition loops."""
    if seg.no_speech_prob > no_speech and seg.avg_logprob < min_logprob:
        return False
    if seg.compression_ratio > max_compression:
        return False
    return True


class ConfidentGroqSTT(groq.STT):
    def __init__(
        self,
        *,
        model: str,
        language: str = "en",
        no_speech_prob: float = 0.6,
        min_logprob: float = -1.0,
        max_compression: float = 2.4,
    ):
        super().__init__(model=model, language=language)
        self._model_name = model
        self._lang = language
        self._no_speech = no_speech_prob
        self._min_logprob = min_logprob
        self._max_compression = max_compression

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        try:
            data = rtc.combine_audio_frames(buffer).to_wav_bytes()
            resp = await self._client.audio.transcriptions.create(
                file=("file.wav", data, "audio/wav"),
                model=self._model_name,
                language=self._lang,
                response_format="verbose_json",
                timeout=httpx.Timeout(30, connect=conn_options.timeout),
            )
        except openai.APITimeoutError:
            raise APITimeoutError() from None
        except openai.APIStatusError as e:
            raise APIStatusError(
                e.message, status_code=e.status_code, request_id=e.request_id, body=e.body
            ) from None
        except Exception as e:
            raise APIConnectionError() from e

        segments = getattr(resp, "segments", None) or []
        if segments:
            kept = [s for s in segments
                    if keep_segment(s, self._no_speech, self._min_logprob, self._max_compression)]
            for s in segments:  # every segment is logged, so thresholds can be tuned from real sessions
                logger.info(
                    "whisper %s %r (no_speech=%.2f logprob=%.2f compression=%.2f)",
                    "KEPT" if s in kept else "DROPPED", s.text.strip(),
                    s.no_speech_prob, s.avg_logprob, s.compression_ratio,
                )
            text = " ".join(s.text.strip() for s in kept).strip()
            # LiveKit tracks a confidence per transcript: use Whisper's average word probability
            conf = math.exp(sum(s.avg_logprob for s in kept) / len(kept)) if kept else 0.0
        else:  # no segment info (shouldn't happen with verbose_json): keep the text as is
            text, conf = (resp.text or "").strip(), 1.0

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(text=text, language=LanguageCode(self._lang), confidence=conf)],
        )
