"""LiveKit voice agent: English speech in -> Hindi speech out.

    mic --WebRTC--> [Silero VAD + turn detector] -> [STT: Whisper/Sarvam]
        -> on_user_turn_completed  (drop fillers-only / hold half sentences)
        -> llm_node                (clean + translate, entity masking, context)
        -> tts_node                (numbers -> Hindi words) -> [Hindi TTS] --WebRTC--> speaker

Run:  python -m translator.agent dev
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any, AsyncIterable
from urllib.parse import urlparse

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    ModelSettings,
    StopResponse,
    cli,
    llm,
)
from livekit.agents import inference
from livekit.agents.types import NOT_GIVEN
from livekit.plugins import silero

from .config import SETTINGS, Settings, make_llm, make_stt, make_tts
from .prompts import FEW_SHOT, SYSTEM_PROMPT, build_user_message
from .text_processing import (
    Glossary,
    PreparedTurn,
    StreamingTTSNormalizer,
    StreamingUnmasker,
    prepare_turn,
)

logger = logging.getLogger("translator")



TOPIC_EVENTS = "translator.events"  # data-channel topic the web UI listens to
CONTEXT_PAIRS = 2  # how many previous (EN, HI) pairs the LLM sees


@dataclass
class ControlState:
    """Things the web UI can change live over RPC."""

    glossary: Glossary
    tone_mode: str = "auto"  # auto | formal | casual
    input_mode: str = "auto"  # auto (always listening, VAD) | ptt (push-to-talk)
    extra: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# helpers (pure, unit-tested)
# --------------------------------------------------------------------------- #


def split_history(messages: list[llm.ChatMessage]) -> tuple[str, list[tuple[str, str]]]:
    """Return (latest user text, previous (user, assistant) pairs) from the chat history.

    Only user messages that actually got a translation are paired, so utterances that
    were dropped or held back don't pollute the context."""
    last_user = ""
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            last_user, last_user_idx = (messages[i].text_content or ""), i
            break

    pairs: list[tuple[str, str]] = []
    pending_user: str | None = None
    for i, m in enumerate(messages):
        if i == last_user_idx:
            break
        if m.role == "user":
            pending_user = m.text_content or ""
        elif m.role == "assistant" and pending_user and (m.text_content or "").strip():
            pairs.append((pending_user, m.text_content or ""))
            pending_user = None
    return last_user, pairs


def build_translation_ctx(turn: PreparedTurn, context: list[tuple[str, str]]) -> llm.ChatContext:
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="system", content=SYSTEM_PROMPT)
    for u, a in FEW_SHOT:
        ctx.add_message(role="user", content=u)
        ctx.add_message(role="assistant", content=a)
    ctx.add_message(role="user", content=build_user_message(turn, context))
    return ctx


def turn_event(turn: PreparedTurn, **extra: Any) -> dict[str, Any]:
    return {
        "type": "turn",
        "raw": turn.raw,
        "cleaned": turn.cleaned,
        "tone": turn.tone,
        "entities": list(turn.entities.values()),
        "hints": [f"{en} → {hi}" for en, hi in turn.hints],
        "fillers": turn.removed_fillers,
        "asr_fixes": turn.asr_fixes,
        **extra,
    }


# --------------------------------------------------------------------------- #
# The agent
# --------------------------------------------------------------------------- #


class InterpreterAgent(Agent):
    def __init__(self, state: ControlState, settings: Settings, room: rtc.Room):
        super().__init__(instructions=SYSTEM_PROMPT)
        self.state = state
        self.settings = settings
        self.room = room
        self._held_text = ""
        self._hold_task: asyncio.Task | None = None

    # ---- UI events ------------------------------------------------------- #
    async def emit(self, payload: dict[str, Any]) -> None:
        try:
            await self.room.local_participant.publish_data(
                json.dumps(payload, ensure_ascii=False).encode(),
                reliable=True,
                topic=TOPIC_EVENTS,
            )
        except Exception:  # UI events must never break the audio pipeline
            logger.debug("publish_data failed", exc_info=True)

    def _prev_user_texts(self) -> list[str]:
        _, pairs = split_history(self.session.history.messages())
        return [u for u, _ in pairs]

    # ---- stage 2.5: gate the finished user turn --------------------------- #
    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        raw = new_message.text_content or ""
        if self._hold_task:
            self._hold_task.cancel()
            self._hold_task = None
        text = f"{self._held_text} {raw}".strip() if self._held_text else raw
        self._held_text = ""

        turn = prepare_turn(text, self.state.glossary, self.state.tone_mode, self._prev_user_texts())

        if turn.skip:
            # only fillers ("uh, um") or a Whisper hallucination on noise: say nothing
            await self.emit(
                {"type": "dropped", "raw": raw, "fillers": turn.removed_fillers, "reason": turn.skip_reason}
            )
            raise StopResponse()

        if turn.incomplete and self.state.input_mode == "auto":
            # "Can we move the meeting to..." -> wait briefly for the rest of the sentence
            self._held_text = text
            self._hold_task = asyncio.create_task(self._flush_held_after_timeout())
            await self.emit({"type": "held", "text": turn.cleaned})
            raise StopResponse()

        # make the chat history hold the merged utterance that we actually translate
        new_message.content = [text]
        await self.emit(turn_event(turn, id=new_message.id))

    async def _flush_held_after_timeout(self) -> None:
        """The speaker really stopped mid-sentence: translate what we have anyway."""
        try:
            await asyncio.sleep(self.settings.incomplete_hold_s)
            while self.session.user_state == "speaking":  # they continued: the next turn merges
                await asyncio.sleep(0.1)
            text, self._held_text = self._held_text, ""
            self._hold_task = None
            if not text:
                return
            turn = prepare_turn(text, self.state.glossary, self.state.tone_mode, self._prev_user_texts())
            await self.emit(turn_event(turn, forced=True))
            self.session.generate_reply(user_input=text)
        except asyncio.CancelledError:
            pass

    # ---- stage 3: clean + translate --------------------------------------- #
    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        # Stateless on purpose: everything is derived from chat_ctx, so LiveKit's
        # preemptive generation and interruptions can't leave us in a stale state.
        user_text, pairs = split_history(chat_ctx.messages())
        if not user_text.strip():
            return
        prev = [u for u, _ in pairs]
        turn = prepare_turn(user_text, self.state.glossary, self.state.tone_mode, prev)
        context = [
            (prepare_turn(u, self.state.glossary).cleaned, h) for u, h in pairs[-CONTEXT_PAIRS:]
        ]
        ctx = build_translation_ctx(turn, context)

        unmasker = StreamingUnmasker(turn.entities)
        quotes = " \n\"'“”"  # some models wrap the answer in quotes: strip them
        started = False
        held = ""  # trailing quote/space chars, released only if more text follows
        async for chunk in Agent.default.llm_node(self, ctx, [], model_settings):
            if isinstance(chunk, str):
                delta = chunk
            elif isinstance(chunk, llm.ChatChunk):
                delta = chunk.delta.content if chunk.delta else None
            else:  # FlushSentinel etc.
                yield chunk
                continue
            if not delta:
                continue
            out = unmasker.feed(delta)
            if not started:
                out = out.lstrip(quotes)
                started = bool(out)
            out = held + out
            body = out.rstrip(quotes)
            held = out[len(body):]
            if body:
                yield body
        tail = (held + unmasker.flush()).rstrip(quotes)
        if tail.strip(quotes):
            yield tail

    # ---- stage 4: speech -------------------------------------------------- #
    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        # The transcript keeps "शाम 5:30 बजे", "₹500", "HR"; only the AUDIO gets the spoken
        # Hindi form ("शाम साढ़े पाँच बजे", "पाँच सौ रुपये", "एचआर"), otherwise the voice
        # reads digits/symbols/acronyms the English way.
        async def verbalized() -> AsyncIterable[str]:
            v = StreamingTTSNormalizer(self.state.glossary.pronunciations)
            async for chunk in text:
                out = v.feed(chunk)
                if out:
                    yield out
            tail = v.flush()
            if tail:
                yield tail

        async for frame in Agent.default.tts_node(self, verbalized(), model_settings):
            yield frame


# --------------------------------------------------------------------------- #
# Worker / session wiring
# --------------------------------------------------------------------------- #


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=SETTINGS.min_silence)


def _livekit_http_proxy():
    """LiveKit's worker uses HTTPS_PROXY even for a local server; respect NO_PROXY instead."""
    host = urlparse(os.getenv("LIVEKIT_URL", "")).hostname or ""
    if host in ("localhost", "127.0.0.1", "::1") or (host and urllib.request.proxy_bypass(host)):
        return None
    return NOT_GIVEN


server = AgentServer(setup_fnc=prewarm, http_proxy=_livekit_http_proxy())


def _turn_detector(settings: Settings):
    if settings.turn_detector != "vad":
        try:
            # LiveKit's end-of-turn model: predicts "is the speaker done?", so it waits
            # longer after "move the meeting to..." than after "...tomorrow?".
            # Runs on LiveKit Cloud with the project keys; falls back to a local model.
            # (Replaces the deprecated livekit-plugins-turn-detector, whose inference
            # worker returned no result on Windows.)
            return inference.TurnDetector()
        except Exception:
            logger.warning("turn detector unavailable -> using VAD silence only", exc_info=True)
    return "vad"


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    s = SETTINGS
    state = ControlState(glossary=Glossary.load(s.glossary_path))
    detector = _turn_detector(s)

    session: AgentSession = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=make_stt(s, state.glossary.stt_prompt()),
        llm=make_llm(s),
        tts=make_tts(s),
        turn_handling={
            "turn_detection": detector,
            "endpointing": {"min_delay": s.min_endpoint_delay, "max_delay": s.max_endpoint_delay},
            "interruption": {
                "enabled": s.overlap_mode == "interrupt",
                "mode": "vad",
                "min_duration": 0.6,  # ignore coughs / short "haan"
                "resume_false_interruption": True,  # noise, not words -> resume Hindi
                "false_interruption_timeout": 1.0,
            },
            "preemptive_generation": {"enabled": True},  # start translating before EOU is final
        },
        use_tts_aligned_transcript=False,  # show the LLM text (digits), not the TTS text
    )

    agent = InterpreterAgent(state, s, ctx.room)

    # ---- latency metrics -> console + UI --------------------------------- #
    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        item = ev.item
        if getattr(item, "role", None) != "assistant":
            return
        msgs = session.history.messages()
        user = next((m for m in reversed(msgs) if m.role == "user" and m.created_at <= item.created_at), None)
        um = dict(user.metrics) if user else {}
        am = dict(item.metrics)
        e2e = None
        if am.get("started_speaking_at") and um.get("stopped_speaking_at"):
            e2e = am["started_speaking_at"] - um["stopped_speaking_at"]
        payload = {
            "type": "metrics",
            "hindi": item.text_content,
            "interrupted": item.interrupted,
            "stt": um.get("transcription_delay"),
            "eou": um.get("end_of_turn_delay"),
            "llm_ttft": am.get("llm_node_ttft"),
            "tts_ttfb": am.get("tts_node_ttfb"),
            "e2e": e2e,
        }
        fmt = lambda v: "-" if v is None else f"{v * 1000:.0f}ms"  # noqa: E731
        logger.info(
            "latency stt=%s eou=%s llm_ttft=%s tts_ttfb=%s TOTAL=%s%s | %s",
            fmt(payload["stt"]), fmt(payload["eou"]), fmt(payload["llm_ttft"]),
            fmt(payload["tts_ttfb"]), fmt(e2e), " (interrupted)" if item.interrupted else "",
            item.text_content,
        )
        asyncio.create_task(agent.emit(payload))

    await session.start(agent=agent, room=ctx.room)

    # ---- RPCs from the web UI -------------------------------------------- #
    lp = ctx.room.local_participant

    @lp.register_rpc_method("set_tone")
    async def _set_tone(data: rtc.RpcInvocationData) -> str:
        if data.payload in ("auto", "formal", "casual"):
            state.tone_mode = data.payload
        return state.tone_mode

    @lp.register_rpc_method("set_input_mode")
    async def _set_input_mode(data: rtc.RpcInvocationData) -> str:
        if data.payload == "ptt":
            state.input_mode = "ptt"
            session.update_options(turn_detection="manual")
            session.input.set_audio_enabled(False)
        else:
            state.input_mode = "auto"
            session.update_options(turn_detection=detector)
            session.input.set_audio_enabled(True)
        return state.input_mode

    @lp.register_rpc_method("ptt_start")
    async def _ptt_start(data: rtc.RpcInvocationData) -> str:
        session.interrupt()  # pressing the button = barge-in
        session.clear_user_turn()
        session.input.set_audio_enabled(True)
        return "ok"

    @lp.register_rpc_method("ptt_end")
    async def _ptt_end(data: rtc.RpcInvocationData) -> str:
        session.input.set_audio_enabled(False)
        session.commit_user_turn(transcript_timeout=5.0)
        return "ok"

    @lp.register_rpc_method("add_terms")
    async def _add_terms(data: rtc.RpcInvocationData) -> str:
        state.glossary.add_entities([t for t in data.payload.split(",") if t.strip()])
        try:  # refresh the STT vocabulary hint too
            agent.update_options(stt=make_stt(s, state.glossary.stt_prompt()))
        except Exception:
            logger.warning("could not refresh STT vocabulary", exc_info=True)
        return json.dumps(state.glossary.entities, ensure_ascii=False)


if __name__ == "__main__":
    cli.run_app(server)
