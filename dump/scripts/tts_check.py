"""Listen to how the Hindi voice pronounces tricky sentences (times, money, names...).

For each English sentence: real translation (same agent + LLM as live) -> the text the
voice actually receives -> ElevenLabs audio (same voice/model as the app). Lines the
speech normaliser changed also get the OLD audio, so you can hear the difference.
Writes out/tts_check/index.html with players for every line.

    python scripts/tts_check.py                 # built-in cases
    python scripts/tts_check.py "My own sentence at 6:45 pm"
    python scripts/tts_check.py --dry-run       # text only, no ElevenLabs credits used
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import sys
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from livekit.agents import AgentSession  # noqa: E402
from livekit.plugins.elevenlabs.tts import DEFAULT_VOICE_ID  # noqa: E402

from translator.agent import ControlState, InterpreterAgent  # noqa: E402
from translator.config import SETTINGS, make_llm  # noqa: E402
from translator.text_processing import Glossary, normalize_for_tts, verbalize_numbers  # noqa: E402

CASES = [
    ("time, PM", "The meeting is at 5:30 pm today."),
    ("time, quarter past", "Let's meet at 10:15 am tomorrow."),
    ("time, quarter to", "The call ends at 2:45 pm."),
    ("time, half past 1 / 2", "Can we do 1:30 instead of 2:30?"),
    ("time, 24-hour", "The train leaves at 17:40."),
    ("time, odd minutes", "The bus comes at 9:10 am."),
    ("date + time", "The demo is on March 5th at 10:30 am."),
    ("money + percent", "The plan costs $500 a month, that's 20% off."),
    ("rupees", "Please pay ₹1,500 by Friday."),
    ("crore, quarters", "The budget is 3.75 crore rupees."),
    ("phone + OTP", "My number is 9876543210 and the OTP is 0420."),
    ("names", "Please join the Google Meet, the Asterisk server is down."),
    ("acronyms", "Send the PDF to HR and copy the CEO."),
    ("abbreviations", "What's your ETA? I need the report by EOD."),
    ("units", "The file is 250 MB and we have 1.5 GB left."),
    ("distance + minutes", "I'm 15 km away, I'll reach in 30 minutes."),
    ("decimal hours", "The workshop lasts 1.5 hours with 12 people."),
    ("year", "We started in 2019 and grew 3x by 2025."),
    ("R&D", "Hey dude, the R&D team is ready."),
    ("formal", "Sir, could you kindly share the invoice?"),
]

OUT = ROOT / "out" / "tts_check"


def synth(text: str, path: Path, key: str) -> None:
    voice = SETTINGS.eleven_voice_id or DEFAULT_VOICE_ID
    body = {
        "text": text, "model_id": SETTINGS.eleven_model, "language_code": "hi",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": SETTINGS.tts_speed},
    }
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        data=json.dumps(body).encode(), headers={"xi-api-key": key, "Content-Type": "application/json"},
    )
    path.write_bytes(urllib.request.urlopen(req, timeout=60).read())


async def translate_all(cases: list[tuple[str, str]], glossary: Glossary) -> list[str]:
    agent = InterpreterAgent(ControlState(glossary=glossary), SETTINGS, room=None)
    out = []
    async with AgentSession(llm=make_llm(SETTINGS)) as session:
        await session.start(agent)
        for n, (_, en) in enumerate(cases):
            if n:
                await asyncio.sleep(8)  # Groq free tier: ~8k tokens/min, ~1k tokens per translation
            await session.run(user_input=en)
            out.append([m for m in session.history.messages() if m.role == "assistant"][-1].text_content)
    return out


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    cases = [("custom", a) for a in args] if args else CASES
    glossary = Glossary.load(SETTINGS.glossary_path)
    key = os.getenv("ELEVEN_API_KEY", "").strip()  # .env is loaded by translator.config
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"Translating {len(cases)} sentences with {SETTINGS.llm_provider}...")
    hindi = asyncio.run(translate_all(cases, glossary))

    rows, chars = [], 0
    for i, ((label, en), hi) in enumerate(zip(cases, hindi), 1):
        new = normalize_for_tts(hi, glossary.pronunciations)
        old = verbalize_numbers(hi)  # what the voice received before this fix
        changed = old != new
        chars += len(new) + (len(old) if changed else 0)
        rows.append((i, label, en, hi, new, old, changed))
        print(f"\n[{i}] {label}\n  EN : {en}\n  HI : {hi}\n  TTS: {new}" + (f"\n  OLD: {old}" if changed else ""))

    print(f"\nAbout {chars:,} ElevenLabs characters for this run.")
    if not dry:
        if not key:
            sys.exit("ELEVEN_API_KEY missing in .env")
        for i, label, en, hi, new, old, changed in rows:
            synth(new, OUT / f"{i:02d}_new.mp3", key)
            if changed:
                synth(old, OUT / f"{i:02d}_old.mp3", key)
            print(f"  audio {i}/{len(rows)} ok")

    esc = html.escape
    trs = "".join(
        f"<tr><td class=n>{i}</td><td><b>{esc(label)}</b><div class=en>{esc(en)}</div></td>"
        f"<td class=hi>{esc(hi)}</td><td class=hi>{esc(new)}"
        + ("" if dry else f"<audio controls src='{i:02d}_new.mp3'></audio>")
        + "</td><td class=hi>"
        + (f"{esc(old)}" + ("" if dry else f"<audio controls src='{i:02d}_old.mp3'></audio>") if changed else "<span class=same>same as new</span>")
        + "</td></tr>"
        for i, label, en, hi, new, old, changed in rows
    )
    (OUT / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Hindi voice check</title>"
        "<style>body{font:14px system-ui;margin:24px;background:#0b0c10;color:#e8e9ee}"
        "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #2a2d38;padding:10px;vertical-align:top;text-align:left}"
        ".hi{font:17px 'Nirmala UI','Noto Sans Devanagari',sans-serif;max-width:330px}.en{color:#9aa0b0;margin-top:4px}"
        ".n{color:#6b7080}audio{display:block;margin-top:8px;width:260px;height:32px}.same{color:#6b7080;font:13px system-ui}"
        "th{color:#9aa0b0;font-weight:500}</style>"
        "<h2>Hindi voice check</h2><p>Screen text keeps digits; the voice gets the <b>spoken</b> form. "
        "Compare <b>NEW</b> (with the speech normaliser) and <b>OLD</b> (before).</p>"
        f"<table><tr><th>#</th><th>English</th><th>Hindi on screen</th><th>NEW: what the voice says</th><th>OLD: before the fix</th></tr>{trs}</table>",
        encoding="utf-8",
    )
    print(f"\nReport: {OUT / 'index.html'}")
    if not dry:
        webbrowser.open((OUT / "index.html").as_uri())


if __name__ == "__main__":
    main()
