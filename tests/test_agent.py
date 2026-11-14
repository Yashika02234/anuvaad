"""Runs the real InterpreterAgent inside a text-only AgentSession with a fake LLM.

Checks the LiveKit wiring without any API keys: what the LLM is sent, that entity
tokens are restored in the streamed output, and that context is carried over."""

from __future__ import annotations

from pathlib import Path

import pytest
from livekit.agents import AgentSession, llm
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

from translator.agent import ControlState, InterpreterAgent, split_history
from translator.config import Settings
from translator.text_processing import Glossary

G_PATH = Path(__file__).resolve().parent.parent / "glossary.json"


class FakeLLM(llm.LLM):
    """Returns canned chunks (deliberately splitting [[1]] across chunks) and records prompts."""

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


@pytest.mark.asyncio
async def test_agent_translates_with_entities_and_context():
    fake = FakeLLM(
        [
            ["\"कृपया [", "[1]] पर ", "आइए।\""],
            ["क्या हम मीटिंग को कल शाम 5 बजे कर सकते हैं?"],
        ]
    )
    state = ControlState(glossary=Glossary.load(G_PATH))
    agent = InterpreterAgent(state, Settings(), room=None)  # room=None: UI events are skipped

    async with AgentSession(llm=fake) as session:
        await session.start(agent)

        r1 = await session.run(user_input="Uh, please join the google meat.")
        r1.expect.contains_message(role="assistant")
        sent = _last_user(fake.prompts[0])
        assert "ENGLISH: Please join the [[1]]." in sent
        assert "PROTECTED: [[1]] = Google Meet" in sent
        assert "uh" not in sent.lower().split("english:")[1]

        r2 = await session.run(user_input="Can we move the meeting to 5 PM tomorrow?")
        sent2 = _last_user(fake.prompts[1])
        assert '"5 PM" -> "शाम 5 बजे"' in sent2
        assert "EN: Please join the Google Meet." in sent2  # previous turn as context
        assert "HI: कृपया Google Meet पर आइए।" in sent2  # unmasked + quotes stripped

    texts = [m.text_content for m in session.history.messages() if m.role == "assistant"]
    assert texts[0] == "कृपया Google Meet पर आइए।"
    assert texts[1] == "क्या हम मीटिंग को कल शाम 5 बजे कर सकते हैं?"


def test_split_history_skips_unanswered_turns():
    ctx = llm.ChatContext.empty()
    ctx.add_message(role="system", content="sys")
    ctx.add_message(role="user", content="uh")  # dropped, never answered
    ctx.add_message(role="user", content="Hello")
    ctx.add_message(role="assistant", content="नमस्ते")
    ctx.add_message(role="user", content="How are you?")
    last, pairs = split_history(ctx.messages())
    assert last == "How are you?"
    assert pairs == [("Hello", "नमस्ते")]
