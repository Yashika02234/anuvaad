# Anuvaad (अनुवाद): Live English → Hindi Voice Interpreter

**Live demo:** [anuvaadx.vercel.app](https://anuvaadx.vercel.app). The access code is shared separately, so the API credits aren't open to everyone.

Speak English and hear it in natural Hindi a moment later. Anuvaad listens to live speech, understands what was meant, translates it into everyday spoken Hindi, and speaks it out loud. It takes about **1.2–2 seconds** from the end of a sentence to the first Hindi audio.

![Anuvaad during a live session](docs/ui.png)

## How it works

Anuvaad works like an assembly line, and every step starts before the previous one has finished:

1. **Listen.** The browser streams the microphone (with echo cancellation) over WebRTC through LiveKit.
2. **Know when a sentence is done.** Voice-activity detection and an end-of-sentence model decide when the speaker has finished, so half sentences aren't translated too early.
3. **Transcribe.** Whisper (large-v3 turbo, on Groq) turns the speech into English text.
4. **Clean up and translate, in one step.** The raw transcript goes straight to one LLM call (Qwen 3.8 27B on Groq, thinking off). A single prompt (`translator/prompts.py`) tells it to skip noise and filler-only turns, fix mis-heard words, drop fillers, pick the tone, and write natural, office-style Hindi exactly the way it should be *spoken*: numbers, times and acronyms already in Hindi words. It does nothing but translate.
5. **Speak.** ElevenLabs Flash v2.5 says the Hindi aloud, starting as soon as the first words arrive.
6. **Show.** The page shows the English (as heard, and as cleaned up by the model), the Hindi, a replay button and the time taken.

An earlier version did the clean-up and the spoken-Hindi rewrite with hand-written rules (regex) around a translation-only prompt. It is parked in `dump/` for reference.

## How each edge case is handled

| Edge case | What Anuvaad does |
|---|---|
| **Strong accents, fast speech** | The translator is told the text came from speech recognition and translates what the speaker *meant*: a word that makes no sense is replaced by the similar-sounding one that does ("google meat" → Google Meet, "next weak" → next week). |
| **Named entities** ("Google Meet", "Asterisk") | Company, product and app names stay in English letters (Google Meet, Zendesk, Razorpay), with Hindi grammar around them ("Google Meet पर"). People and places are written in Hindi (Priya → प्रिया, Mumbai → मुंबई). |
| **Partial sentences** | The end-of-sentence model waits longer after "…move the meeting to". If a half sentence still gets through, it is translated as said, without inventing the rest ("क्या हम मीटिंग को…"), and the next turn completes it ("कल शाम पाँच बजे कर सकते हैं?") because the fragment is passed along as context. |
| **Fillers** ("uh", "you know") | The model drops them, without touching real words ("I like pizza" stays). A turn that is only fillers is not spoken at all. |
| **Noise and silence** | Speech recognition sometimes produces junk on background noise ("Me. Me. Me."). The model skips it, and the page shows the turn as dropped. Hindi speech is skipped too, since the input is English. |
| **Latency buildup** | Every step streams into the next, translation starts before the sentence is fully confirmed, fast models are used, and the prompt stays short. The time to first Hindi audio is shown on every line. |
| **Formal vs casual tone** | Words like "sir, kindly" or "hey dude, gonna" decide between आप and तुम. Formal is the default, the safe choice in Hindi. The tone menu can also force either. |
| **Numbers, dates, abbreviations** | Times are said the Hindi way: 5:30 → साढ़े पाँच, 10:15 → सवा दस, 2:45 → पौने तीन, 1:30 → डेढ़, 2:30 → ढाई. Also 5 PM → शाम पाँच बजे, ₹1,500 → पंद्रह सौ रुपये, $5.30 → पाँच डॉलर तीस सेंट, 20% → बीस प्रतिशत, 3.75 करोड़ → पौने चार करोड़, ETA → पहुँचने का अनुमानित समय, HR → एचआर. The model writes them in words, so the screen shows exactly what the voice says. |
| **Interruptions** | Echo cancellation keeps the Hindi playback out of the microphone. If the speaker talks over the Hindi, playback stops (barge-in), and the line is marked "interrupted". |
| **The model answering instead of translating** | The translator is strictly instructed that it is not an assistant, with examples, so a question like "What time is it?" is translated rather than answered. |

## Using it

Listening never starts on its own. First tap the mic (or **Start session**) and allow the microphone. Then:

- **Hands-free** (default): just talk; a short pause ends each sentence.
- **Hold to talk**: hold the mic button (or the Space bar) while speaking. Good for noisy rooms.
- **Tone**: Auto, Formal (आप) or Casual (तुम).
- **Replay and download**: every Hindi line has a ▶ Play button. The icons beside the mic download the transcript and the full Hindi audio.

Headphones give the cleanest experience.

## Speed

| Step | Typical time |
|---|---|
| Knowing the sentence has ended | 0.4–0.6 s |
| Speech to English text | 0.2–0.4 s |
| First words of the translation | 0.3–0.5 s |
| First Hindi audio | 0.2–0.3 s |
| **End of speech → first Hindi audio** | **≈ 1.2–2 s** |

Measured on the live deployment (previous, regex-based version):

| Spoken English | Hindi | Total |
|---|---|---|
| "Uh, can we move the meeting to 5 PM tomorrow?" | क्या हम मीटिंग को कल शाम 5 बजे कर सकते हैं? | **1.4–1.8 s** |
| "The meeting is at 5:30 pm today." | मीटिंग आज शाम साढ़े 5 बजे है। (spoken: साढ़े पाँच) | **1.2 s** |
| "Please join the Google Meet, the Asterisk server is down." | कृपया Google Meet में जुड़िए, Asterisk सर्वर डाउन है। | **1.9 s** |

## Quality checks

- **Prompt lab** (`scripts/prompt_lab.py`) sends 52 tricky turns (fillers, noise, names, times, money, tone, half sentences and their continuations) to the real model with the live prompt, checks each answer automatically and prints PASS/FAIL. Latest run with Qwen 3.8 27B: 48/50 on the earlier set; see `out/prompt_lab_results.md`.
- **Automated tests** (`pytest`) run the real agent with a fake model: only the Hindi line reaches the voice, skipped turns stay silent and out of the context, and earlier turns are passed along correctly.

## Built with

LiveKit Agents (real-time audio and turn-taking) · Groq Whisper (speech to text) · Groq Qwen 3.8 27B (translation) · ElevenLabs Flash v2.5 (Hindi voice) · Vercel (web page) · LiveKit Cloud (agent hosting). Every service runs on its free tier.

## Known limitations and next steps

- Translation happens per sentence, not word by word. That's deliberate: Hindi puts the verb at the end, so it needs the whole sentence.
- Hinglish (Hindi spoken in English words) works, but speech recognition is tuned for English, so accuracy varies.
- Speech recognition is the slowest step. A streaming speech-to-text service would save about half a second.
- Next ideas: phone-call support, and separate voices for different speakers in a group call.
