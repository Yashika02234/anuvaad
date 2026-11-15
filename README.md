# Anuvaad (अनुवाद): Live English → Hindi Voice Interpreter

Speak English, hear natural spoken Hindi about **1.2–2 seconds** later. **Live demo:** [anuvaadx.vercel.app](https://anuvaadx.vercel.app) (access code shared separately).

| Web | Mobile |
|---|---|
| ![Web conversation](docs/screenshots/web-conversation.png) | <img src="docs/screenshots/mobile-conversation.png" alt="Mobile conversation" width="260"> |

<details><summary>Start screens</summary>

| Web | Mobile |
|---|---|
| ![Web start screen](docs/screenshots/web-start.png) | <img src="docs/screenshots/mobile-start.png" alt="Mobile start screen" width="260"> |

</details>

*Conversation screenshots use sample lines.*

## Architecture

```mermaid
---
title: Anuvaad – System Architecture v2 (scope - one live English → Hindi session)
---
flowchart LR
    subgraph ACT["👥 Actor"]
        USER(("👤 Speaker<br/>speaks English,<br/>hears Hindi"))
    end

    subgraph FE["💻 Frontend · Vercel CDN"]
        WEB["Web App<br/>React + livekit-client<br/>mic, playback, screen,<br/>keeps transcript + clips"]
    end

    subgraph BE["🔒 Backend · Vercel (trust boundary 1: access code)"]
        TOK["Token Service<br/>Python serverless fn<br/>checks access code,<br/>signs room pass (JWT)"]
    end

    subgraph INFRA["🔒 Real-time Infra · LiveKit Cloud (trust boundary 2: JWT)"]
        ROOM["Call Room<br/>WebRTC media server<br/>carries audio, text, events"]
        AGENT["Interpreter Agent<br/>Python, LiveKit Agents<br/>hear → transcribe → translate → speak"]
        MEM[("Conversation Memory<br/>agent RAM, per session<br/>last turns for context")]
    end

    subgraph EXT["🔒 Third-party AI · API keys held only by the agent (trust boundary 3)"]
        GROQ["Groq<br/>Whisper (speech → text)<br/>gpt-oss-120b · Qwen · gpt-oss-20b<br/>(translate)"]
        GEM["Google Gemini<br/>Flash (last-resort<br/>translator)"]
        TTS["ElevenLabs<br/>Flash v2.5<br/>Hindi text → voice"]
    end

    USER -- "① taps mic, speaks English" --> WEB
    WEB -- "② access code" --> TOK
    TOK -- "③ signed room pass (JWT)" --> WEB
    WEB -- "④ mic audio (WebRTC)" --> ROOM
    ROOM -- "⑤ audio + 'join' job" --> AGENT
    AGENT -- "⑥ speech clip → English text" --> GROQ
    AGENT -- "⑦ read last turns / save new turn" --> MEM
    AGENT -- "⑧ prompt → ACTION / EN / HINDI" --> GROQ
    AGENT -- "⑧b same prompt, only if all Groq<br/>translators are busy" --> GEM
    AGENT -- "⑨ Hindi text → Hindi voice" --> TTS
    AGENT -- "⑩ Hindi audio + text" --> ROOM
    ROOM -- "⑪ Hindi audio + text" --> WEB
    WEB -- "⑫ 🔊 Hindi + on-screen text" --> USER

    AGENT -. "events: turn · dropped ·<br/>model_status · metrics" .-> ROOM
    ROOM -. "events" .-> WEB
    WEB -. "settings: tone · model ·<br/>talk mode (RPC)" .-> ROOM
    AGENT -. "background: tiny<br/>recovery probe" .-> GROQ

    classDef actor fill:#ffffff,stroke:#374151,color:#111827
    classDef fe fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef be fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef data fill:#ffedd5,stroke:#ea580c,color:#7c2d12
    classDef ext fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    classDef infra fill:#f3f4f6,stroke:#6b7280,color:#111827
    class USER actor
    class WEB fe
    class TOK,AGENT be
    class MEM data
    class GROQ,GEM,TTS ext
    class ROOM infra
    style ACT fill:none,stroke:#9ca3af,stroke-dasharray:6 4
    style FE fill:none,stroke:#2563eb,stroke-dasharray:6 4
    style BE fill:none,stroke:#16a34a,stroke-dasharray:6 4
    style INFRA fill:none,stroke:#6b7280,stroke-dasharray:6 4
    style EXT fill:none,stroke:#7c3aed,stroke-dasharray:6 4
```

- **Web App** (Vercel) captures the mic and plays the Hindi. The **Token Service** only lets people with the access code in.
- **LiveKit Cloud** carries the audio and runs the **Interpreter Agent**, which calls Groq, Gemini and ElevenLabs. API keys never reach the browser.

## How it Works

```mermaid
sequenceDiagram
    autonumber
    actor U as Speaker
    participant W as Web App
    participant A as Interpreter Agent
    participant S as Groq Whisper
    participant L as Translator (first free model)
    participant T as ElevenLabs

    U->>W: Speaks "Can we move the meeting to 5.30 pm tomorrow?"
    W->>A: Mic audio (via LiveKit room)
    A->>A: Pause detected → sentence clip
    A->>S: Speech clip
    S-->>A: English text + confidence
    alt only noise
        A->>A: Drop it, nothing is translated
    else real speech
        A->>A: Turn detector: sentence finished
        A->>L: Rules + last 2 turns + tone + sentence
        alt model rate-limited or too slow
            L-->>A: 429 / timeout
            A->>L: Same prompt to next model in line
        end
        L-->>A: ACTION / EN / HINDI (streamed)
        A->>T: Hindi text only
        T-->>A: Hindi voice (streamed)
        A->>W: Hindi voice + text + latency
        W-->>U: 🔊 Hindi plays, Hindi shown on screen
    end
```

1. A voice detector cuts each sentence when you pause. Groq Whisper turns it into English, and pure noise is dropped.
2. A turn detector waits a moment if the sentence sounds unfinished.
3. One LLM call does everything: it cleans the English, picks the tone (आप/तुम), and writes Hindi with numbers and times spelled the way they are said.
4. Only the Hindi line is sent to ElevenLabs, which starts speaking while the rest is still arriving.

### Edge cases

| Case | Handling |
|---|---|
| Accents, mis-heard words | Translates what was *meant* ("google meat" → Google Meet) |
| Names | Brands stay in English letters; people and places in Devanagari |
| Half sentences | Waits longer; a fragment is completed on the next turn |
| Fillers, noise | Dropped; filler-only or noise turns are not spoken |
| Tone | आप / तुम picked from the words, or set from the menu |
| Numbers, times | Said the Hindi way: 5:30 → साढ़े पाँच, 1:30 → डेढ़, $5.30 → पाँच डॉलर तीस सेंट |
| Questions to the bot | Translated, never answered |
| Interruptions | Talking over the Hindi stops it |
| Rate limits | Automatic model fallback, plus a "slow down" hint on the page |

## Data Pipeline

```mermaid
---
title: Anuvaad – Speech-to-Speech Pipeline
---
flowchart LR
    subgraph IN["Input"]
        A1["🎙️ English speech<br/>browser mic, echo/noise cleaned"]
    end

    subgraph PRE["Preprocessing"]
        B1["Voice detection<br/>cut clip after 0.45 s silence"]
        B2["Speech → text<br/>Groq Whisper + noise filter"]
        B3["End-of-turn check<br/>finished? wait 0.8 s · unsure ≤ 2.5 s"]
    end

    subgraph PROC["Processing"]
        C1["Prompt build<br/>rules + last 2 turns + tone"]
        C2["Translate<br/>gpt-oss-120b → Qwen → gpt-oss-20b → Gemini"]
        C3["Reply check<br/>keep Hindi line, skip noise turns"]
    end

    subgraph OUT["Output"]
        D1["🔊 Hindi voice<br/>ElevenLabs stream"]
        D2["🖥️ Screen<br/>English + Hindi + latency"]
    end

    A1 -- "audio stream" --> B1
    B1 -- "speech clip" --> B2
    B2 -- "English text" --> B3
    B3 -- "complete sentence" --> C1
    C1 -- "prompt" --> C2
    C2 -- "ACTION / EN / HINDI" --> C3
    C3 -- "Hindi text" --> D1
    C3 -- "English + Hindi" --> D2

    classDef in fill:#dbeafe,stroke:#2563eb,color:#1e3a8a
    classDef pre fill:#f3f4f6,stroke:#6b7280,color:#111827
    classDef proc fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef out fill:#ede9fe,stroke:#7c3aed,color:#4c1d95
    class A1 in
    class B1,B2,B3 pre
    class C1,C2,C3 proc
    class D1,D2 out
```

### Model fallback (the Translate stage)

```mermaid
sequenceDiagram
    autonumber
    participant A as Model Chain (agent)
    participant M1 as gpt-oss-120b (Groq)
    participant M2 as Qwen (Groq)
    participant M3 as gpt-oss-20b (Groq)
    participant G as Gemini (Google)
    participant W as Web App

    Note over A: Sentence ready · line-up (Auto):<br/>gpt-oss-120b → Qwen → gpt-oss-20b → Gemini

    A->>M1: Prompt (5 s limit, no retries)
    alt 429 rate limit / timeout / error before Hindi
        M1-->>A: 429 "try again in 20 s"
        A->>A: Mark gpt-oss-120b busy
        A--)W: model_status: busy → "gpt-oss 120B is busy"
        A->>M2: Same prompt
        M2-->>A: ACTION / EN / HINDI ✅
    else reply ended without a Hindi line (cut off)
        M1-->>A: "ACTION… EN…" (no HINDI)
        A->>M2: Same prompt (gpt-oss-120b stays available)
        M2-->>A: ACTION / EN / HINDI ✅
    else failed after Hindi had started
        M1-->>A: Partial Hindi, then error
        A->>A: Stop · no switch (would repeat the sentence)
    end

    opt Qwen also busy
        A->>M3: Same prompt
        M3-->>A: ACTION / EN / HINDI ✅
    end

    opt all three Groq models busy
        A->>G: Same prompt (last resort)
        alt Gemini answers
            G-->>A: ACTION / EN / HINDI ✅
        else Gemini fails too
            G-->>A: error
            A--)W: no Hindi → page shows "⚠ not translated"
        end
    end

    Note over A,W: Next sentence: busy models are skipped straight away

    Note over A,M1: Background recovery (runs in parallel)
    A->>A: Wait the time the API gave (20 s)
    A->>M1: Tiny probe "Reply OK" (8 tokens)
    M1-->>A: "OK" + header: tokens left this minute
    alt answered and ≥ 3,500 tokens left
        A->>A: Mark gpt-oss-120b available
        A--)W: model_status: available → "gpt-oss 120B is available again"
    else not enough yet
        A->>A: Wait for the reset, probe again
    end
```

- Each free model has its own rate limit, so taking turns keeps translation going when one is busy.
- A busy model is skipped until a tiny probe shows it is free again, so the full prompt is never re-sent just to check.
- In the page, **Auto** follows this order and shows the model answering right now. Picking a model puts it first.

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React + Vite, `livekit-client`, hosted on Vercel |
| Token API | Python serverless function on Vercel (`web/api/token.py`) |
| Real-time + agent | LiveKit Cloud, LiveKit Agents (Python 3.11), Silero VAD, LiveKit turn detector |
| Speech to text | Groq Whisper large-v3-turbo |
| Translation (LLM) | Groq gpt-oss-120b · Qwen 3.8 27B · gpt-oss-20b, Google Gemini Flash (fallback) |
| Hindi voice | ElevenLabs Flash v2.5 |
| Tests | pytest (fake LLM, no API keys needed) |

## Setup / Run

**1. Keys.** Create `.env` in the project root:

```env
LIVEKIT_URL=wss://<your-project>.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
GROQ_API_KEY=...
ELEVEN_API_KEY=...
GOOGLE_API_KEY=...        # optional: Gemini as last-resort translator
ACCESS_CODE=...           # optional: page then needs ?code=<ACCESS_CODE>
```

Optional tuning: `LLM_FALLBACK_ORDER` (default `gpt-oss-120b,qwen,gpt-oss-20b,gemini`), `GEMINI_MODEL`, `MIN_ENDPOINT_DELAY`, `MAX_ENDPOINT_DELAY`.

**2. Agent** (Python 3.11):

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m translator.agent download-files           # VAD + turn-detector models
python -m translator.agent dev                      # joins rooms on your LiveKit project
```

**3. Web page** (two terminals):

```bash
python -m uvicorn server:app --port 8000            # token API on :8000
cd web && npm install && npm run dev                # page on :5173, proxies /api to :8000
```

Open `http://localhost:5173/?code=<ACCESS_CODE>`, tap the mic and speak English.

**4. Tests:** `pytest`

**5. Deploy:**

```bash
lk agent deploy                                     # agent → LiveKit Cloud (keys via `lk agent update-secrets`)
cd web && vercel deploy --prod                      # page + token API → Vercel (set LIVEKIT_*, ACCESS_CODE there)
```
