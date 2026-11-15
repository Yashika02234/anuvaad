# dump/ — the earlier regex pipeline (parked, not used)

These files belong to the first version of the translator, where deterministic Python
(regex) did most of the work around a translation-only LLM call. The live pipeline now
sends the raw speech-to-text turn straight to one LLM prompt
(`translator/prompts.py`), so none of this is imported anywhere.

Kept for reference and in case a piece is worth bringing back.

| File | What it did |
|---|---|
| `translator/text_processing.py` | Regex clean-up before the LLM (ASR fixes, fillers, Whisper noise/echo, incomplete-sentence hold, entity masking, time/money hints, tone) and the spoken-Hindi rewrite after it (numbers, times, acronyms for the TTS) |
| `translator/prompts_old.py` | The old translation-only prompt + few-shot examples |
| `glossary.json` | Protected names, ASR corrections, abbreviation meanings, pronunciations; also fed Whisper its vocabulary prompt |
| `tests/test_text_processing.py` | Unit tests for `text_processing.py` |
| `scripts/eval_translation.py` | Ran edge-case sentences through the old agent |
| `scripts/tts_check.py`, `out/tts_check/` | Old listening check (ElevenLabs audio before/after the spoken-Hindi rewrite) |

To run any of it again, move the file back to its original folder.
