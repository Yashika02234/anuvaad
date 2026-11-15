"""Runs the real InterpreterAgent inside a text-only AgentSession with a fake LLM, plus
unit tests for the streamed-reply parser. No API keys needed.

Checks: what the LLM is sent (raw ASR text + context), that only the HINDI line reaches
the voice / chat history, that skipped turns say nothing and stay out of the context."""

from __future__ import annotations

import pytest
from livekit.agents import AgentSession, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from translator.agent import ControlState, InterpreterAgent, split_history
from translator.config import Settings
from translator.prompts import ReplyParser


class FakeLLM(llm.LLM):
    """Returns canned chunks (deliberately splitting the header lines) and records prompts."""

    def __init__(self, replies: list[list[str]]):
        super().__init__()
        self.replies = replies
        self.prompts: list[llm.ChatContext] = []

    def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kw):
        self.prompts.append(chat_ctx)
        return _FakeStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options,
                           chunks=self.replies[len(self.prompts) - 1])


class _FakeStream(llm.LLMStream):
    def __init__(self, fake, *, chat_ctx, tools, conn_options, chunks):
        super().__init__(fake, chat_ctx=chat_ctx, tools=tools, conn_options=conn_options)
        self.chunks = chunks

    async def _run(self) -> None:
        for c in self.chunks:
            self._event_ch.send_nowait(
                llm.ChatChunk(id="x", delta=llm.ChoiceDelta(role="assistant", content=c))
            )


def _last_user(ctx: llm.ChatContext) -> str:
    return [m for m in ctx.messages() if m.role == "user"][-1].text_content


def _feed_all(chunks: list[str]) -> tuple[ReplyParser, str]:
    p = ReplyParser()
    out = "".join(p.feed(c) for c in chunks) + p.flush()
    return p, out


# --------------------------------------------------------------------------- #
# agent in a text-only session
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_streams_only_hindi_and_carries_context():
    fake = FakeLLM(
        [
            ["ACTION: tran", "slate\nEN: Please join the Google Meet.\nHIN", "DI: \"कृपया Google Meet पर ", "आइए।\""],
            ["ACTION: skip\nEN: -\nHINDI: -"],
            ["ACTION: translate\nEN: Can we move the meeting to\nHINDI: क्या हम मीटिंग को…"],
            ["ACTION: translate\nEN: 5 PM tomorrow?\nHINDI: कल शाम पाँच बजे कर सकते हैं?"],
        ]
    )
    agent = InterpreterAgent(ControlState(), Settings(), room=None)  # room=None: no UI events

    async with AgentSession(llm=fake) as session:
        await session.start(agent)

        await session.run(user_input="Uh, please join the google meat.")
        sent = _last_user(fake.prompts[0])
        assert "ASR TEXT: Uh, please join the google meat." in sent  # raw text, no regex clean-up
        assert "TONE SETTING: auto" in sent
        assert "CONTEXT" not in sent

        await session.run(user_input="Um, uh.")  # skipped: nothing spoken

        await session.run(user_input="Can we move the meeting to")
        sent3 = _last_user(fake.prompts[2])
        assert "EN: Please join the Google Meet." in sent3  # the LLM's cleaned English as context
        assert "HINDI: कृपया Google Meet पर आइए।" in sent3  # quotes stripped
        assert "Um, uh." not in sent3  # skipped turn is not context

        await session.run(user_input="5 PM tomorrow?")
        sent4 = _last_user(fake.prompts[3])
        assert "EN: Can we move the meeting to\n  HINDI: क्या हम मीटिंग को…" in sent4  # fragment -> continuation

    texts = [m.text_content for m in session.history.messages() if m.role == "assistant"]
    spoken = [t for t in texts if t and t.strip()]
    assert spoken == [
        "कृपया Google Meet पर आइए।",
        "क्या हम मीटिंग को…",
        "कल शाम पाँच बजे कर सकते हैं?",
    ]


def test_split_history_skips_unanswered_turns():
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="system", content="sys")
    ctx.add_message(role="user", content="uh")  # skipped, never answered
    ctx.add_message(role="user", content="Hello")
    ctx.add_message(role="assistant", content="नमस्ते")
    ctx.add_message(role="user", content="How are you?")
    last, pairs = split_history(ctx.messages())
    assert last == "How are you?"
    assert pairs == [("Hello", "नमस्ते")]


# --------------------------------------------------------------------------- #
# ReplyParser
# --------------------------------------------------------------------------- #


def test_parser_splits_header_across_chunks():
    p, out = _feed_all(["ACT", "ION: translate\nE", "N: Hi there.\nHINDI:", " नमस्ते", " दोस्त।"])
    assert (p.action, p.en, out) == ("translate", "Hi there.", "नमस्ते दोस्त।")


def test_parser_streams_hindi_before_the_reply_ends():
    p = ReplyParser()
    assert p.feed("ACTION: translate\nEN: Hello.\nHINDI: नमस्") == "नमस्"  # released at once
    assert p.header_done
    assert p.feed("ते।") == "ते।"


def test_parser_skip_says_nothing():
    p, out = _feed_all(["ACTION: skip\nEN: -\nHINDI: -"])
    assert p.skip and out == ""


def test_parser_strips_quotes_and_ignores_extra_lines():
    p, out = _feed_all(["ACTION: translate\nEN: Yes.\nHINDI: “हाँ।”\nNote: done"])
    assert out == "हाँ।"


def test_parser_dash_alone_is_not_spoken():
    p, out = _feed_all(["ACTION: translate\nEN: -\nHINDI: -\n"])
    assert out == ""


def test_parser_falls_back_to_plain_hindi():
    p, out = _feed_all(["क्या हम मीटिंग कल कर सकते हैं?"])
    assert p.action == "translate" and out == "क्या हम मीटिंग कल कर सकते हैं?"


# --------------------------------------------------------------------------- #
# Whisper confidence filter (translator/stt.py)
# --------------------------------------------------------------------------- #

from types import SimpleNamespace  # noqa: E402

from translator.stt import keep_segment  # noqa: E402


def _seg(no_speech=0.0, logprob=-0.2, compression=1.0):
    return SimpleNamespace(no_speech_prob=no_speech, avg_logprob=logprob, compression_ratio=compression)


@pytest.mark.parametrize(
    "seg, kept",
    [
        (_seg(), True),  # clear speech
        (_seg(no_speech=0.9, logprob=-1.5), False),  # Whisper's own silence rule
        (_seg(no_speech=0.9, logprob=-0.3), True),  # "no speech" but confident words: keep
        (_seg(no_speech=0.1, logprob=-1.5), True),  # unsure words but clearly speech: keep
        (_seg(compression=3.1), False),  # repetition loop "Me. Me. Me. Me."
    ],
)
def test_keep_segment(seg, kept):
    assert keep_segment(seg, no_speech=0.6, min_logprob=-1.0, max_compression=2.4) is kept


# --------------------------------------------------------------------------- #
# Model fallback (rate limit on the first model -> the next one answers)
# --------------------------------------------------------------------------- #

from livekit.agents import APIStatusError  # noqa: E402

from translator.models import ModelChains, wait_from_error  # noqa: E402


class RateLimitedLLM(llm.LLM):
    """Always answers like Groq's free tier when the limit is reached."""

    def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kw):
        return _FailStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class _FailStream(llm.LLMStream):
    async def _run(self) -> None:
        raise APIStatusError("Rate limit reached for model", status_code=429)


@pytest.mark.asyncio
async def test_rate_limited_model_falls_back_to_the_next():
    ok = FakeLLM([["ACTION: translate\nEN: Hello.\nHINDI: नमस्ते।"]])
    chains = ModelChains({"qwen": RateLimitedLLM(), "gpt-oss-120b": ok})
    agent = InterpreterAgent(ControlState(), Settings(), room=None, chains=chains)
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    agent.emit = capture  # record the events the web page would get

    async with AgentSession(llm=ok) as session:
        await session.start(agent)
        await session.run(user_input="Hello.")

    spoken = [m.text_content for m in session.history.messages() if m.role == "assistant"]
    assert spoken == ["नमस्ते।"]  # answered despite the 429
    turn = next(e for e in sent if e["type"] == "turn")
    assert turn["model"] == "gpt-oss-120b" and turn["fallback"] is True
    assert chains.busy == {"qwen"}  # later turns skip it until the recovery check passes
    await chains.aclose()


def test_fallback_order_keeps_the_rest_in_place():
    chains = ModelChains({n: FakeLLM([]) for n in ["qwen", "gpt-oss-120b", "gpt-oss-20b", "gemini"]})
    assert chains.order_for("qwen") == ["qwen", "gpt-oss-120b", "gpt-oss-20b", "gemini"]
    assert chains.order_for("gpt-oss-120b") == ["gpt-oss-120b", "qwen", "gpt-oss-20b", "gemini"]
    assert chains.order_for("gemini") == ["gemini", "qwen", "gpt-oss-120b", "gpt-oss-20b"]


@pytest.mark.parametrize("msg, wait", [
    ("Rate limit reached ... Please try again in 19.937s. Need more tokens?", 19.937),
    ("... tokens per day (TPD) ... Please try again in 7m48.288s.", 468.288),
    ("RESOURCE_EXHAUSTED ... 'retryDelay': '30s'", 30.0),
    ("some other error", None),
])
def test_wait_time_is_read_from_the_error(msg, wait):
    got = wait_from_error(APIStatusError(msg, status_code=429))
    assert (got is None and wait is None) or abs(got - wait) < 1e-6


class _FailAfterStream(llm.LLMStream):
    """Streams some chunks, then fails like a dropped connection."""

    def __init__(self, fake, *, chunks, **kw):
        super().__init__(fake, **kw)
        self.chunks = chunks

    async def _run(self) -> None:
        for c in self.chunks:
            self._event_ch.send_nowait(llm.ChatChunk(id="x", delta=llm.ChoiceDelta(role="assistant", content=c)))
        raise APIStatusError("connection dropped", status_code=500)


class FailAfterLLM(llm.LLM):
    def __init__(self, chunks):
        super().__init__()
        self.chunks = chunks

    def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **kw):
        return _FailAfterStream(self, chunks=self.chunks, chat_ctx=chat_ctx, tools=tools or [],
                                conn_options=conn_options)


async def _collect(chains, choice="auto"):
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="user", content="x")
    out, names = "", []
    async for name, chunk in chains.stream(choice, ctx):
        names.append(name)
        out += chunk.delta.content if chunk.delta and chunk.delta.content else ""
    return names, out


@pytest.mark.asyncio
async def test_cut_off_reply_without_hindi_moves_to_the_next_model():
    # like gpt-oss hitting the token cap inside the EN line
    cut = FakeLLM([["ACTION: translate\nEN: I am 15% sure that"]])
    ok = FakeLLM([["ACTION: translate\nEN: Yes.\nHINDI: हाँ।"]])
    chains = ModelChains({"gpt-oss-120b": cut, "gemini": ok})
    names, out = await _collect(chains)
    assert set(names) == {"gemini"}  # nothing from the cut-off reply was passed on
    assert "HINDI: हाँ।" in out
    assert chains.busy == set()  # cut off is not a rate limit: the model stays available


@pytest.mark.asyncio
async def test_failure_before_hindi_falls_back_but_after_hindi_does_not():
    ok = FakeLLM([["ACTION: translate\nEN: Yes.\nHINDI: हाँ।"]])
    early = FailAfterLLM(["ACTION: translate\nEN: Yes.\n"])  # dies before the Hindi line
    chains = ModelChains({"qwen": early, "gemini": ok})
    names, out = await _collect(chains)
    assert set(names) == {"gemini"} and "हाँ" in out

    late = FailAfterLLM(["ACTION: translate\nEN: Yes.\nHINDI: हाँ, "])  # dies mid-Hindi
    chains2 = ModelChains({"qwen": late, "gemini": FakeLLM([["unused"]])})
    with pytest.raises(APIStatusError):  # no switch: the voice already started speaking
        await _collect(chains2)
    await chains.aclose(); await chains2.aclose()
