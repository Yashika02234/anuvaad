"""Run the edge-case sentences through the REAL agent + LLM (text only, no audio).

Uses the same InterpreterAgent as the live pipeline, so it tests the exact prompt,
masking and post-processing. Needs only the LLM key (GOOGLE_API_KEY or GROQ_API_KEY).

    python scripts/eval_translation.py            # built-in edge cases
    python scripts/eval_translation.py "Your own sentence" "another one"
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from livekit.agents import AgentSession  # noqa: E402

from translator.agent import ControlState, InterpreterAgent  # noqa: E402
from translator.config import SETTINGS, make_llm  # noqa: E402
from translator.text_processing import Glossary, prepare_turn  # noqa: E402

CASES = [
    ("example flow", "Can we move the meeting to 5 PM tomorrow?"),
    ("fillers", "Uh, so, you know, I I think we should, um, push the release."),
    ("named entities", "Please join the google meat, the asterix server is down."),
    ("abbreviations", "What's your ETA? I need the report by EOD, ASAP."),
    ("time + date", "The demo is on March 5th at 10:30 am, not 2:30 pm."),
    ("money + percent", "The plan costs $500 a month, that's 20% off."),
    ("casual tone", "Hey dude, gonna be late, grab me a coffee?"),
    ("formal tone", "Sir, could you kindly share the invoice with the HR team?"),
    ("question to the bot", "Can you tell me what time it is?"),  # must translate, not answer
    ("ASR-ish errors", "we can scheduled the call for next weak on monday"),
    ("fragment", "and then the client said that"),
]


async def main(sentences: list[tuple[str, str]]) -> None:
    glossary = Glossary.load(SETTINGS.glossary_path)
    agent = InterpreterAgent(ControlState(glossary=glossary), SETTINGS, room=None)
    print(f"LLM provider: {SETTINGS.llm_provider}\n")
    async with AgentSession(llm=make_llm(SETTINGS)) as session:
        await session.start(agent)
        for label, text in sentences:
            turn = prepare_turn(text, glossary)
            t0 = time.perf_counter()
            await session.run(user_input=text)
            dt = time.perf_counter() - t0
            hi = [m for m in session.history.messages() if m.role == "assistant"][-1].text_content
            print(f"[{label}]")
            print(f"  EN raw    : {text}")
            print(f"  EN cleaned: {turn.cleaned}   (tone={turn.tone})")
            if turn.hints:
                print(f"  hints     : {'; '.join(f'{a} → {b}' for a, b in turn.hints)}")
            print(f"  HI        : {hi}")
            print(f"  LLM time  : {dt:.2f}s\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    asyncio.run(main([("custom", a) for a in args] if args else CASES))
