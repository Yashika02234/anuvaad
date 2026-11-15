"""Unit tests for the deterministic edge-case handling (no API keys needed)."""

from pathlib import Path

import pytest

from translator.prompts import build_user_message
from translator.text_processing import (
    Glossary,
    StreamingNumberVerbalizer,
    StreamingTTSNormalizer,
    StreamingUnmasker,
    normalize_for_tts,
    detect_tone,
    fix_asr_errors,
    hindi_number,
    hindi_time,
    is_hallucination,
    localization_hints,
    looks_incomplete,
    mask_entities,
    prepare_turn,
    remove_fillers,
    verbalize_numbers,
)

G = Glossary.load(Path(__file__).resolve().parent.parent / "glossary.json")


# ---- fillers ---------------------------------------------------------------
@pytest.mark.parametrize(
    "inp,out",
    [
        ("Uh, can we, you know, move the meeting?", "Can we move the meeting?"),
        ("um so I I think it's fine", "So I think it's fine"),
        ("I mean, the server is down.", "The server is down."),
        ("It works, you know?", "It works?"),
        ("Hmm. Okay let's go", "Okay let's go"),
    ],
)
def test_removes_fillers(inp, out):
    assert remove_fillers(inp).text == out


@pytest.mark.parametrize(
    "keep",
    [
        "I like pizza.",
        "What kind of car is that?",
        "Do you know the way?",
        "I would like, if possible, some tea.",
        "I mean what I say.",
    ],
)
def test_keeps_real_words(keep):
    assert remove_fillers(keep).text == keep


# ---- ASR fixes + entities --------------------------------------------------
def test_asr_corrections_and_casing():
    assert fix_asr_errors("join the google meat on live kit", G) == "join the Google Meet on LiveKit"


def test_mask_and_unmask():
    masked, m = mask_entities("Join Google Meet, Asterisk is down, Google Meet again", G)
    assert masked == "Join [[1]], [[2]] is down, [[1]] again"
    assert m == {"[[1]]": "Google Meet", "[[2]]": "Asterisk"}


def test_entity_inside_word_not_masked():
    masked, m = mask_entities("Zoomed in on the HRMS dashboard", G)
    assert m == {} and masked == "Zoomed in on the HRMS dashboard"


def test_streaming_unmasker_handles_split_tokens():
    u = StreamingUnmasker({"[[1]]": "Google Meet", "[[12]]": "Asterisk"})
    chunks = ["कृपया [", "[1", "]] पर आइए, [[1", "2]] डाउन है [x]"]
    out = "".join(u.feed(c) for c in chunks) + u.flush()
    assert out == "कृपया Google Meet पर आइए, Asterisk डाउन है [x]"


# ---- partial input / hallucinations -----------------------------------------
@pytest.mark.parametrize(
    "text,incomplete",
    [
        ("Can we move the meeting to", True),
        ("Can we move the meeting to.", True),  # Whisper adds a period
        ("I'll send it to the", True),
        ("Can we move the meeting to 5 PM tomorrow?", False),
        ("Thank you", False),
        ("What is that", False),
        ("Please log in", False),
    ],
)
def test_incomplete(text, incomplete):
    assert looks_incomplete(text) is incomplete


@pytest.mark.parametrize("t", ["Thanks for watching!", "you", "", " . "])
def test_hallucinations(t):
    assert is_hallucination(t)


def test_fillers_only_turn_is_skipped():
    assert prepare_turn("uh, um, hmm", G).skip


# ---- numbers, dates, abbreviations ------------------------------------------
@pytest.mark.parametrize(
    "h,m,mer,expected",
    [
        (5, 0, "PM", "शाम 5 बजे"),
        (9, 0, "am", "सुबह 9 बजे"),
        (1, 30, "pm", "दोपहर डेढ़ बजे"),
        (2, 30, "pm", "दोपहर ढाई बजे"),
        (5, 30, "pm", "शाम साढ़े 5 बजे"),
        (10, 15, "am", "सुबह सवा 10 बजे"),
        (6, 45, "pm", "शाम पौने 7 बजे"),
        (12, 0, "pm", "दोपहर 12 बजे"),
        (12, 0, "am", "रात 12 बजे"),
        (11, 10, "pm", "रात 11 बजकर 10 मिनट"),
    ],
)
def test_hindi_time(h, m, mer, expected):
    assert hindi_time(h, m, mer) == expected


def test_localization_hints():
    hints = dict(localization_hints("ETA? Move it to 5 PM tomorrow, March 3rd, budget $500 or 20%", G))
    assert hints["5 PM"] == "शाम 5 बजे"
    assert hints["tomorrow"] == "कल"  # no "(आने वाला)" notes: the LLM would speak them
    assert hints["March 3rd"] == "3 मार्च"
    assert hints["$500"] == "500 डॉलर"
    assert hints["20%"] == "20 प्रतिशत"
    assert hints["ETA"] == "पहुँचने का अनुमानित समय"


def test_day_after_tomorrow_not_tomorrow():
    hints = dict(localization_hints("the day after tomorrow", G))
    assert hints == {"day after tomorrow": "परसों"}


def test_abbreviation_is_case_sensitive():
    # "eta" inside normal words / lowercase must not trigger
    assert localization_hints("the beta version", G) == []


@pytest.mark.parametrize(
    "n,words",
    [(5, "पाँच"), (45, "पैंतालीस"), (100, "एक सौ"), (2026, "दो हज़ार छब्बीस"),
     (125000, "एक लाख पच्चीस हज़ार"), (30000000, "तीन करोड़")],
)
def test_hindi_number(n, words):
    assert hindi_number(n) == words


def test_verbalize_numbers():
    assert verbalize_numbers("शाम 5 बजे") == "शाम पाँच बजे"
    assert verbalize_numbers("2.5 घंटे") == "दो दशमलव पाँच घंटे"
    assert verbalize_numbers("9876543210") == "नौ आठ सात छह पाँच चार तीन दो एक शून्य"


def test_streaming_verbalizer_never_splits_numbers():
    v = StreamingNumberVerbalizer()
    out = "".join(v.feed(c) for c in ["शाम 1", "2 बजे, ", "2", "5 मिनट"]) + v.flush()
    assert out == "शाम बारह बजे, पच्चीस मिनट"


# ---- tone ---------------------------------------------------------------------
def test_tone():
    assert detect_tone("Sir, could you kindly confirm?") == "formal"
    assert detect_tone("Hey dude, gonna be late") == "casual"
    assert detect_tone("The server is down") == "formal"  # safe default
    assert detect_tone("The server is down", previous=["yo bro", "cool man"]) == "casual"


def test_tone_override():
    assert prepare_turn("Hey dude", G, tone_mode="formal").tone == "formal"


# ---- full turn + prompt -------------------------------------------------------
def test_prepare_turn_example_flow():
    t = prepare_turn("Uh, can we move the meeting on google meat to 5 PM tomorrow?", G)
    assert t.cleaned == "Can we move the meeting on Google Meet to 5 PM tomorrow?"
    assert t.masked == "Can we move the meeting on [[1]] to 5 PM tomorrow?"
    assert not t.skip and not t.incomplete
    msg = build_user_message(t, context=[("Hello", "नमस्ते")])
    assert "PROTECTED: [[1]] = Google Meet" in msg
    assert '"5 PM" -> "शाम 5 बजे"' in msg
    assert msg.endswith("ENGLISH: Can we move the meeting on [[1]] to 5 PM tomorrow?")
    assert "EN: Hello" in msg


# ---- real transcripts from a live Windows session ------------------------------
@pytest.mark.parametrize(
    "raw,reason",
    [
        ("Vocabulary Mekarra, M.", "echo"),
        ("Vocabulary Mere, M.D.", "echo"),
        ("Voculars.", "echo"),
        ("Gnani, Microsoft Teeam, CZ, Gb, WFH, OOO, PTO, TBD, AFAIK, KPI, DM, POC.", "echo"),
        ("तुम्हें मैं फोन पर पसंदू, तुम्हें मैं लाइब नहीं पसंदू, यूं बोलो.", "hindi"),
        ("Thank you.", "noise"),
        ("Me. Me. Me. Me.", "noise"),
    ],
)
def test_live_session_junk_is_skipped(raw, reason):
    t = prepare_turn(raw, G)
    assert t.skip, raw
    assert {"echo": "echoed", "hindi": "Hindi", "noise": "noise"}[reason] in t.skip_reason


@pytest.mark.parametrize(
    "raw",
    ["Can we move the meeting to 5 PM tomorrow?", "Hello.", "Voice or text?",
     "Can you please shut up?", "Google Meet and Zoom are both fine.",
     "I said yes, yes, yes."],
)
def test_live_session_real_speech_kept(raw):
    assert not prepare_turn(raw, G).skip


def test_noise_tail_removed_from_real_speech():
    t = prepare_turn("Come see. Me. Me. Me. Me. Me.  Thank you.", G)
    assert t.cleaned == "Come see." and not t.skip


def test_stt_prompt_is_not_a_list_label():
    p = G.stt_prompt()
    assert not p.lower().startswith("vocabulary") and "WFH" not in p and "Google Meet" in p


# ---- speech normalisation for the Hindi voice (audio only) -------------------
@pytest.mark.parametrize(
    "hi,spoken",
    [
        ("मीटिंग 5:30 PM पर है।", "मीटिंग शाम साढ़े पाँच बजे है।"),
        ("मीटिंग शाम 5:30 बजे है।", "मीटिंग शाम साढ़े पाँच बजे है।"),  # no second "शाम"
        ("कॉल 10:15 AM पर है, 2:45 PM तक।", "कॉल सुबह सवा दस बजे है, दोपहर पौने तीन बजे तक।"),
        ("हम 1:30 बजे और 2:30 बजे मिलेंगे।", "हम डेढ़ बजे और ढाई बजे मिलेंगे।"),
        ("ट्रेन 17:40 बजे प्रस्थान करती है।", "ट्रेन शाम पाँच बजकर चालीस मिनट पर प्रस्थान करती है।"),
        ("डेमो 6 PM पर है।", "डेमो शाम छह बजे है।"),
        ("कीमत ₹1,500 है और $20 छूट।", "कीमत एक हज़ार पाँच सौ रुपये है और बीस डॉलर छूट।"),
        ("20% छूट, यानी 2.5 लाख रुपये।", "बीस प्रतिशत छूट, यानी ढाई लाख रुपये।"),
        ("बजट 3.75 करोड़ है।", "बजट पौने चार करोड़ है।"),
        ("HR टीम को PDF भेजिए।", "एचआर टीम को पीडीएफ़ भेजिए।"),
        ("R&D टीम तैयार है।", "आर एंड डी टीम तैयार है।"),
        ("फ़ाइल 250 MB की है, 1.5 GB बाकी।", "फ़ाइल दो सौ पचास एमबी की है, डेढ़ जीबी बाकी।"),
        ("मैं 15 किमी दूर हूँ।", "मैं पंद्रह किलोमीटर दूर हूँ।"),
        ("OTP 0420 है।", "ओटीपी शून्य चार दो शून्य है।"),
    ],
)
def test_normalize_for_tts(hi, spoken):
    assert normalize_for_tts(hi, G.pronunciations) == spoken


def test_tts_keeps_english_names_but_applies_glossary_pronunciations():
    # ElevenLabs reads Latin-script names well (a Devanagari "एस्टरिस्क" came out as "S-Risk"),
    # so names stay as-is unless the glossary gives a pronunciation (Gnani -> ज्ञानी).
    out = normalize_for_tts("कृपया Google Meet पर जुड़ें, Asterisk सर्वर डाउन है। Gnani टीम।", G.pronunciations)
    assert out == "कृपया Google Meet पर जुड़ें, Asterisk सर्वर डाउन है। ज्ञानी टीम।"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 8])
def test_streaming_tts_normalizer_matches_one_shot(size):
    s = "कृपया Google Meet पर 5:30 PM जुड़ें, कीमत ₹1,500 है और HR को PDF भेजें।"
    st = StreamingTTSNormalizer(G.pronunciations)
    streamed = "".join(st.feed(s[i:i + size]) for i in range(0, len(s), size)) + st.flush()
    assert streamed == normalize_for_tts(s, G.pronunciations)


def test_streaming_does_not_repeat_day_period_across_chunks():
    st = StreamingTTSNormalizer()
    out = st.feed("मीटिंग शाम ") + st.feed("5:30 PM ") + st.feed("पर है।") + st.flush()
    assert out.count("शाम") == 1


# ---- dotted times from speech-to-text ("5.30 pm") ----------------------------
@pytest.mark.parametrize(
    "en,expected",
    [
        ("The meeting is at 5.30 pm today.", "The meeting is at 5:30 pm today."),
        ("Let's meet at 5.30.", "Let's meet at 5:30."),
        ("from 9.15 to 10.45", "from 9:15 to 10:45"),
        ("It costs $5.30 now.", "It costs $5.30 now."),          # money stays a decimal
        ("Growth was 5.30 percent.", "Growth was 5.30 percent."),  # so does a percentage
    ],
)
def test_normalize_clock_punctuation(en, expected):
    from translator.text_processing import normalize_clock_punctuation
    assert normalize_clock_punctuation(en) == expected


def test_dotted_time_gets_a_hint():
    turn = prepare_turn("The meeting is at 5.30 pm today.", G)
    assert ("5:30 pm", "शाम साढ़े 5 बजे") in turn.hints


def test_plain_time_without_ampm_gets_a_hint():
    hints = dict(localization_hints("Let's meet at 5:30 tomorrow.", G))
    assert hints["5:30"] == "साढ़े 5 बजे"


@pytest.mark.parametrize(
    "hi,spoken",
    [
        ("मीटिंग 5.30 बजे है।", "मीटिंग साढ़े पाँच बजे है।"),          # what the live agent produced
        ("मीटिंग 5.30 PM पर है।", "मीटिंग शाम साढ़े पाँच बजे है।"),
        ("मीटिंग 5 बजकर 30 मिनट पर है।", "मीटिंग साढ़े पाँच बजे है।"),
        ("कॉल 2 बजकर 45 मिनट पर खत्म होगी।", "कॉल पौने तीन बजे खत्म होगी।"),
        ("कीमत 5.30 डॉलर है।", "कीमत पाँच दशमलव तीन शून्य डॉलर है।"),   # a price is not a time
    ],
)
def test_tts_dotted_and_bajkar_times(hi, spoken):
    assert normalize_for_tts(hi, G.pronunciations) == spoken


@pytest.mark.parametrize("size", [1, 2, 3, 4, 7])
def test_streaming_keeps_time_phrases_together(size):
    s = "मीटिंग 5 बजकर 30 मिनट पर है और कॉल 5.30 बजे है।"
    st = StreamingTTSNormalizer(G.pronunciations)
    streamed = "".join(st.feed(s[i:i + size]) for i in range(0, len(s), size)) + st.flush()
    assert streamed == normalize_for_tts(s, G.pronunciations)
    assert "साढ़े पाँच बजे" in streamed and "दशमलव" not in streamed
