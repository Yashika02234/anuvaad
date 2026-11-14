# Live English → Hindi Voice Interpreter

**Live demo:** https://live-hindi-interpreter.vercel.app (the access code is shared separately, so the API credits aren't open to everyone)

Real-time voice-to-voice translation: English speech goes in, and fluent Hindi speech comes out in near real time (about 1.5–2 s from the end of a sentence to the first Hindi audio).

Built on **LiveKit Agents**, which handles WebRTC transport, VAD, turn-taking, interruptions and worker scaling. Every AI stage can be swapped with an env var, and all defaults run on free tiers.

![UI](docs/ui.png)

## The assembly line

```
 Browser mic ──WebRTC──► LiveKit ──► Agent worker (Python)
 (echo-cancelled)          SFU        │
                                      ├─ 1. Silero VAD + turn detector     "has the speaker finished?"
                                      ├─ 2. STT: Whisper-large-v3-turbo    (Groq)  | Sarvam (Indian-English)
                                      │        + vocabulary prompt (Google Meet, Asterisk…)
                                      ├─ 3. Gate  (on_user_turn_completed)
                                      │        drop filler-only / hallucinated / noise / Hindi turns
                                      │        hold half sentences ("move the meeting to…")
                                      ├─ 4. Clean + translate  (llm_node, Groq gpt-oss-120b | Gemini)
                                      │        ASR fixes → filler removal → entity masking
                                      │        → localization hints → tone → LLM → unmask (streaming)
                                      ├─ 5. TTS prep (tts_node): numbers → Hindi words for audio only
                                      └─ 6. Hindi TTS: ElevenLabs Flash v2.5 | Sarvam Bulbul
 Browser ◄──WebRTC── Hindi audio + live transcripts + latency events
   ├─ plays Hindi immediately (streaming)
   ├─ records each Hindi line as a clip  → "ready to play" ▶ per line
   └─ records the whole session          → ⬇ download
```

Every stage streams: LLM tokens flow into TTS while the sentence is still being generated, and TTS audio starts playing on the first chunk.

## Quick start

1. Install:

   ```bash
   python -m venv .venv
   source .venv/bin/activate          # Windows: .venv\Scripts\activate
   python -m pip install -r requirements.txt
   python -m livekit.agents download-files
   ```

   The last command downloads the Silero VAD model. On Windows, always run tools as `python -m …`: if Smart App Control is on, it can block the `pip.exe` launcher.

2. Create a file named `.env` in the project folder (it is git-ignored) with:

   ```bash
   LIVEKIT_URL=wss://your-project.livekit.cloud
   LIVEKIT_API_KEY=...
   LIVEKIT_API_SECRET=...
   GROQ_API_KEY=...
   ELEVEN_API_KEY=...
   ```

   - `LIVEKIT_*`: a free project at cloud.livekit.io. The end-of-sentence model also runs through LiveKit Cloud inference.
   - `GROQ_API_KEY`: speech-to-text (Whisper) **and** translation (`openai/gpt-oss-120b`), both on the free tier.
   - `ELEVEN_API_KEY`: the Hindi voice. Alternatively set `TTS_PROVIDER=sarvam` and `SARVAM_API_KEY`.
   - Optional: `LLM_PROVIDER=gemini` with a Google AI Studio `GOOGLE_API_KEY`. Other optional settings (models, voice, turn-taking timings) and their defaults are in `translator/config.py`.

3. Run the agent and the web page in two terminals:

   ```bash
   python -m translator.agent dev
   ```

   ```bash
   python -m uvicorn server:app --port 8000
   ```

4. Open http://localhost:8000, click **Start session**, and say:
   *"Can we move the meeting to 5 PM tomorrow?"*

Use headphones for the best experience. Echo cancellation is on, but headphones remove the Hindi output from the mic completely.

### Test without a microphone

```bash
pytest
```

This runs 67 tests and needs no API keys.

```bash
python scripts/eval_translation.py
```

This runs the edge-case sentences through the real LLM, text only.

```bash
python scripts/eval_translation.py "Your own sentence"
```

## How each edge case is handled

| Edge case | Handling | Where |
|---|---|---|
| **Strong accent / fast speech** | Whisper large-v3 with a **vocabulary prompt** built from the glossary. Glossary `asr_corrections` fix known mis-hearings ("google meat" → Google Meet). The LLM is told the text comes from ASR and to translate what was *meant*. `STT_PROVIDER=sarvam` switches to a model trained on Indian-accented English. | `config.make_stt`, `text_processing.fix_asr_errors`, prompt rule 1 |
| **Named entities** ("Google Meet", "Asterisk") | Replaced by `[[1]]`-style tokens before the LLM sees the text, so they can't be translated. The LLM is told what each token means, so Hindi grammar around it is right ("[[1]] पर"). Tokens are restored by a **streaming-safe unmasker** (a token split across chunks still works). Names that are **not** in the glossary are handled by the prompt: company/product/app names stay in English letters (Zendesk, Salesforce), people and places are written in Devanagari (Priya → प्रिया), with a few-shot example. | `mask_entities`, `StreamingUnmasker`, `prompts.py` rule 8 |
| **Partial / incomplete input** | (a) LiveKit's **turn-detector model** waits longer after "…move the meeting to". (b) A rule-based check holds utterances ending in dangling words (*to, the, because…*) and merges them with the next one. (c) If the speaker really stops, the held text is translated after `INCOMPLETE_HOLD_SECONDS`. The UI merges the fragments into one row. | `looks_incomplete`, `on_user_turn_completed`, `_flush_held_after_timeout` |
| **Fillers** ("uh", "you know") | Deterministic removal: sounds always; phrases only when comma-delimited, so "I like pizza" and "what kind of car" are untouched. Stutters ("I I") are collapsed. Filler-only turns are **not spoken at all**. The LLM has the same rule as a backup. | `remove_fillers`, `is_hallucination` |
| **Whisper hallucinations on silence / noise** ("Thanks for watching!", "Me. Me. Me.", the model repeating its own vocabulary prompt) | Repetition loops are removed and noise-only sentences dropped, while real speech in the same turn is kept. Prompt echoes are dropped. Hindi speech (Devanagari ASR output) is skipped because the input language is English. Romanised Hinglish ("waise to kya") is translated into proper Devanagari. Each dropped turn shows its reason in the UI. | `strip_noise_sentences`, `is_prompt_echo`, `is_not_english`, prompt rule 8 |
| **Latency buildup** | Everything streams (STT → LLM tokens → TTS). Preemptive generation starts translating before end-of-turn is final. Flash-tier LLM and TTS models are used. TTS speed is 1.05×. The LLM gets only the last 2 lines of context, so prompts stay small and constant. Per-stage latency is logged and shown in the UI. | `agent.entrypoint`, `config` |
| **Tone** (formal vs casual Hindi) | Formal/casual markers in the current and recent utterances ("sir, kindly" vs "hey dude, gonna") select आप or तुम. The default is formal, the safe choice in Hindi. A UI toggle can force either. | `detect_tone`, RPC `set_tone` |
| **Numbers, dates, abbreviations** | Deterministic hints are given to the LLM: `5 PM → शाम 5 बजे`, `5:30 → साढ़े 5`, `2:30 → ढाई`, `6:45 → पौने 7`, `March 5th → 5 मार्च`, `$500 → 500 डॉलर`, `20% → 20 प्रतिशत`, `tomorrow → कल` (tense tells tomorrow from yesterday), `ETA → पहुँचने का अनुमानित समय`. For **audio only**, digits become Hindi words (`5 → पाँच`, `1,25,000 → एक लाख पच्चीस हज़ार`), so the voice never says "five" mid-Hindi. The on-screen text keeps digits. | `localization_hints`, `hindi_time`, `StreamingNumberVerbalizer` |
| **Interruptions** (speech overlapping TTS) | WebRTC echo cancellation. `OVERLAP_MODE=interrupt` (default): the speaker talking for 0.6 s or more stops the Hindi playback (barge-in). Noise that isn't speech resumes playback. `OVERLAP_MODE=queue`: interpreter style, where translations are never cut off. Interrupted lines are flagged in the UI. | `turn_handling` in `agent.entrypoint` |
| **LLM answering instead of translating** | Strict system prompt ("you are NOT an assistant") plus few-shot examples. The LLM never sees the raw chat history, only a fresh translation prompt each turn. | `prompts.py`, `llm_node` |

## Input modes

Listening never starts on its own: the speaker first taps the mic (or **Start session**) and allows the microphone. After that:

- **Hands-free** (default): no button to hold. The speaker just talks and pauses, and VAD and the turn detector decide when a sentence is done.
- **Hold to talk**: hold the mic button or the Space bar. Releasing commits the turn immediately (`commit_user_turn`), which suits noisy rooms.

## Project layout

```
translator/
  agent.py            LiveKit agent: gating, llm_node, tts_node, RPCs, metrics
  text_processing.py  all deterministic edge-case logic (pure Python, unit-tested)
  prompts.py          system prompt, few-shot examples, per-turn message builder
  config.py           env settings + STT/LLM/TTS provider factories
server.py             token endpoint + serves the UI
web/public/           index.html, app.js, style.css (no build step)
web/api/token.py      token endpoint (Vercel serverless function; reused by server.py locally)
Dockerfile            the agent image (LiveKit Cloud: lk agent create)
glossary.json         names to keep, ASR corrections, abbreviations
tests/                67 tests (text processing + agent with a fake LLM)
scripts/eval_translation.py   real-LLM evaluation of edge cases (text only)
```

## Latency budget (targets)

| Stage | Typical |
|---|---|
| End-of-speech detection (VAD + turn detector) | 0.4–0.6 s |
| STT (Groq Whisper turbo) | 0.2–0.4 s |
| LLM first token (Groq gpt-oss-120b) | 0.3–0.5 s |
| TTS first audio (ElevenLabs Flash) | 0.2–0.3 s |
| **End of speech → first Hindi audio** | **≈ 1.4–2 s** |

The agent logs a line for every utterance, and the UI shows the same numbers.

**Measured** (end-to-end voice test on a home Windows laptop, speech fed in as a microphone):

| Spoken English | Hindi output | ASR | LLM | TTS | Total |
|---|---|---|---|---|---|
| "Uh, can we move the meeting to 5 PM tomorrow?" | क्या हम मीटिंग को कल शाम 5 बजे कर सकते हैं? | 1066 ms | 338 ms | 246 ms | **1.70 s** |
| "Please join the Google Meet, the Asterisk server is down." | कृपया Google Meet में जुड़िए, Asterisk सर्वर डाउन है। | 1178 ms | 444 ms | 248 ms | **1.93 s** |

ASR includes the end-of-speech wait. Live sessions with a real microphone measured 1.4–1.9 s.

## Known limitations / next steps

- Translation is per sentence, not word by word. That is deliberate: Hindi is verb-final (SOV), so it needs the whole clause.
- Romanised Hinglish is handled by the LLM (prompt rule 8 + a few-shot example), but code-mixed speech is still recognised by an English ASR, so accuracy varies.
- The ASR step is the largest share of latency, because Whisper works per utterance. A streaming ASR (e.g. Deepgram) would cut about 0.5 s.
- Possible next steps: a SIP phone-call demo via LiveKit SIP, and per-speaker voices for multi-party calls.
