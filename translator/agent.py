"""LiveKit voice agent: English speech in, Hindi speech out.

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
    cli,
    llm,
)
from livekit.agents import inference
from livekit.agents.types import NOT_GIVEN
from livekit.plugins import silero

from .config import SETTINGS, Settings, build_model_chains, make_stt, make_tts
from .models import ModelChains
from .prompts import SYSTEM_PROMPT, ReplyParser, build_user_prompt

logger = logging.getLogger("translator")

TOPIC_EVENTS = "translator.events"  # data-channel topic the web UI listens to
CONTEXT_PAIRS = 2  # how many previous (EN, HINDI) pairs the LLM sees


@dataclass
class ControlState:
    """Things the web UI can change live over RPC."""

    tone_mode: str = "auto"  # auto | formal | casual
    input_mode: str = "auto"  # auto (always listening, VAD) | ptt (push-to-talk)
    model: str = "auto"  # auto (fallback order) | a model name pinned first (see ModelChains)
    extra: dict[str, Any] = field(default_factory=dict)




def split_history(messages: list[llm.ChatMessage]) -> tuple[str, list[tuple[str, str]]]:
    """Return (latest user text, previous (user, assistant) pairs); skipped turns are left out."""
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


def build_translation_ctx(user_text: str, tone: str, context: list[tuple[str, str]]) -> llm.ChatContext:
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="system", content=SYSTEM_PROMPT)
    ctx.add_message(role="user", content=build_user_prompt(user_text, tone, context))
    return ctx




class InterpreterAgent(Agent):
    def __init__(
        self, state: ControlState, settings: Settings, room: rtc.Room | None,
        chains: ModelChains | None = None,
    ):
        super().__init__(instructions=SYSTEM_PROMPT)
        self.state = state
        self.chains = chains  # None = use the session's own LLM (tests)
        self.settings = settings
        self.room = room
        self._cleaned: dict[str, str] = {}  # raw ASR text -> the LLM's cleaned English
        self._announced: str | None = None  # last raw text the UI was told about

    async def emit(self, payload: dict[str, Any]) -> None:
        if self.room is None:
            return
        try:
            await self.room.local_participant.publish_data(
                json.dumps(payload, ensure_ascii=False).encode(),
                reliable=True,
                topic=TOPIC_EVENTS,
            )
        except Exception:  # UI events must never break the audio pipeline
            logger.debug("publish_data failed", exc_info=True)

    async def _announce(
        self, turn_key: str, raw: str, reply: ReplyParser, model: str | None = None,
        fallback: bool = False,
    ) -> None:
        """Tell the web page what happened to this turn (once per turn)."""
        if self._announced == turn_key:
            return
        self._announced = turn_key
        if reply.skip:
            await self.emit({"type": "dropped", "raw": raw, "reason": "nothing to translate"})
            return
        event: dict[str, Any] = {"type": "turn", "raw": raw, "cleaned": reply.en or raw}
        if self.state.tone_mode in ("formal", "casual"):
            event["tone"] = self.state.tone_mode
        if model:
            event["model"] = model  # which model translated it
            event["fallback"] = fallback  # True: the first-choice model was busy
        await self.emit(event)

    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings
    ) -> AsyncIterable[str]:
        messages = chat_ctx.messages()
        user_text, pairs = split_history(messages)
        if not user_text.strip():
            return
        # same turn number + same text = same turn (a repeated sentence is a new turn)
        turn_key = f"{sum(m.role == 'user' for m in messages)}:{user_text}"
        context = [(self._cleaned.get(u, u), h) for u, h in pairs[-CONTEXT_PAIRS:]]
        ctx = build_translation_ctx(user_text, self.state.tone_mode, context)

        reply = ReplyParser()
        info: dict[str, Any] = {}
        async for chunk in self._generate(ctx, model_settings, info):
            if isinstance(chunk, str):
                delta = chunk
            elif isinstance(chunk, llm.ChatChunk):
                delta = chunk.delta.content if chunk.delta else None
            else:  # FlushSentinel etc.
                yield chunk
                continue
            if not delta:
                continue
            out = reply.feed(delta)
            if reply.header_done:
                await self._announce(turn_key, user_text, reply, **info)
            if out:
                yield out
        tail = reply.flush()
        await self._announce(turn_key, user_text, reply, **info)
        if reply.en and reply.en != "-":
            self._cleaned[user_text] = reply.en
        if tail:
            yield tail
        if not reply.skip and not reply.hindi.strip():
            logger.warning("LLM reply had no Hindi line for %r", user_text)

    async def _generate(self, ctx: llm.ChatContext, model_settings: ModelSettings, info: dict):
        """Stream the reply from the model chain; `info` gets the model that answered."""
        if self.chains is None:
            async for chunk in Agent.default.llm_node(self, ctx, [], model_settings):
                yield chunk
            return
        choice = self.state.model
        async for name, chunk in self.chains.stream(choice, ctx):
            if not info:
                info.update(model=name, fallback=name != self.chains.primary(choice))
                if info["fallback"]:
                    logger.info("translated by fallback model %s (%s busy)", name,
                                self.chains.primary(choice))
            yield chunk




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
            # LiveKit's end-of-turn model: waits longer when the sentence sounds unfinished
            return inference.TurnDetector()
        except Exception:
            logger.warning("turn detector unavailable -> using VAD silence only", exc_info=True)
    return "vad"


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    s = SETTINGS
    state = ControlState()
    chains = build_model_chains(s)
    ctx.add_shutdown_callback(chains.aclose)
    detector = _turn_detector(s)

    session: AgentSession = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=make_stt(s),
        llm=chains.models[chains.order[0]],  # translation itself goes through chains.stream
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
            "preemptive_generation": {"enabled": False},  # one LLM call per finished turn
        },
        use_tts_aligned_transcript=False,  # show the LLM text, not the TTS text
    )

    agent = InterpreterAgent(state, s, ctx.room, chains)

    def _model_status(name: str, available: bool) -> None:
        logger.info("model %s %s", name, "available again" if available else "unavailable")
        asyncio.create_task(agent.emit({"type": "model_status", "model": name, "available": available}))

    chains.on_change(_model_status)

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

    lp = ctx.room.local_participant

    @lp.register_rpc_method("set_tone")
    async def _set_tone(data: rtc.RpcInvocationData) -> str:
        if data.payload in ("auto", "formal", "casual"):
            state.tone_mode = data.payload
        return state.tone_mode

    @lp.register_rpc_method("set_model")
    async def _set_model(data: rtc.RpcInvocationData) -> str:
        if data.payload in chains.names:
            state.model = data.payload
        return state.model

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


if __name__ == "__main__":
    cli.run_app(server)
