"""Deterministic text processing around the LLM.

Everything here is pure Python with no network calls, so it is fast (well under
1 ms per turn) and fully unit-tested. The LLM still does the actual translation,
but these steps make its job easier and its output safer:

    raw ASR text
      -> fix_asr_errors       ("google meat" -> "Google Meet")
      -> remove_fillers       ("uh", "you know", stutters)
      -> is_hallucination     (Whisper's "Thanks for watching!" on silence)
      -> looks_incomplete     (hold "Can we move the meeting to" for more words)
      -> mask_entities        ("Google Meet" -> "[[1]]" so it can't be translated)
      -> localization_hints   ("5 PM" -> "शाम 5 बजे", "ETA" -> ...)
      -> detect_tone          (formal / casual)
    LLM output
      -> StreamingUnmasker    ("[[1]]" -> "Google Meet", streaming-safe)
      -> StreamingNumberVerbalizer (TTS only: "5 बजे" -> "पाँच बजे")
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Glossary
# --------------------------------------------------------------------------- #


@dataclass
class Glossary:
    entities: list[str] = field(default_factory=list)
    asr_corrections: dict[str, str] = field(default_factory=dict)
    abbreviations: dict[str, str] = field(default_factory=dict)
    # how the Hindi voice should SAY English names ("Google Meet" -> "गूगल मीट"); audio only
    pronunciations: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            entities=list(data.get("entities", [])),
            asr_corrections={k.lower(): v for k, v in data.get("asr_corrections", {}).items()},
            abbreviations=dict(data.get("abbreviations", {})),
            pronunciations=dict(data.get("pronunciations", {})),
        )

    def add_entities(self, names: list[str]) -> None:
        for n in names:
            n = n.strip()
            if n and n.lower() not in {e.lower() for e in self.entities}:
                self.entities.append(n)

    def stt_prompt(self) -> str:
        """Spelling hint for Whisper-style STT.

        Written as a natural sentence with only multi-letter names (no "Vocabulary:" label,
        no abbreviation list): Whisper tends to echo a list-style prompt back verbatim on
        silence or noise ("Vocabulary: Google Meet, WFH, OOO..."). Echoes that still slip
        through are caught by is_prompt_echo()."""
        names = [e for e in self.entities if not e.isupper()][:12]
        return "We talked about " + ", ".join(names) + "."


def _word_re(phrase: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w])" + re.escape(phrase) + r"(?![\w])", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# 1. ASR error correction
# --------------------------------------------------------------------------- #


def fix_asr_errors(text: str, glossary: Glossary) -> str:
    # longest first so "google meat" wins over a shorter overlapping key
    for wrong in sorted(glossary.asr_corrections, key=len, reverse=True):
        text = _word_re(wrong).sub(glossary.asr_corrections[wrong], text)
    # normalise casing of known entities ("google meet" -> "Google Meet")
    for ent in sorted(glossary.entities, key=len, reverse=True):
        text = _word_re(ent).sub(ent, text)
    return text


# --------------------------------------------------------------------------- #
# 2. Filler removal
# --------------------------------------------------------------------------- #

# Sounds that are *always* fillers.
_SOUND_FILLERS = re.compile(
    r"(?<![\w'])(?:u+h+m*|u+m+|e+r+m+|e+r+|a+h+|h+m+|m{2,}|uh-huh)(?![\w'])[,.]?",
    re.IGNORECASE,
)
# Phrases that are fillers only when set off by commas / sentence edges,
# so "I like pizza" and "what kind of car" are left alone.
_PHRASE_FILLERS = ["you know", "i mean", "like", "sort of", "kind of", "you see", "basically"]


_INLINE_FILLERS = {"you know", "i mean"}  # not "like": "I would like, if possible, tea"


def _phrase_filler_re(p: str) -> re.Pattern[str]:
    # ", you know," / "You know, ..." at the start / ", you know" at the end
    pat = r"(?:(?:^|,)\s*" + re.escape(p) + r"\s*(?:,|(?=[.?!])|$))"
    if p in _INLINE_FILLERS:
        # "we should, you know, go" is covered above; this catches "be you know, late"
        pat += r"|(?:(?<=\w)\s+" + re.escape(p) + r"\s*,)"
    return re.compile(pat, re.IGNORECASE)


_PHRASE_RES = [(p, _phrase_filler_re(p)) for p in _PHRASE_FILLERS]
_STUTTER = re.compile(r"\b(\w+)(?:[\s,]+\1\b)+", re.IGNORECASE)


@dataclass
class FillerResult:
    text: str
    removed: list[str]


def remove_fillers(text: str) -> FillerResult:
    removed: list[str] = []

    def _grab(m: re.Match[str]) -> str:
        removed.append(m.group(0).strip(" ,."))
        return " "

    out = _SOUND_FILLERS.sub(_grab, text)
    out = _tidy(out)
    for _, rx in _PHRASE_RES:
        # repeat: "you know, like, ..." needs two passes
        for _ in range(3):
            new = rx.sub(lambda m: (removed.append(m.group(0).strip(" ,.")) or " "), out)
            new = _tidy(new)
            if new == out:
                break
            out = new
    # "I I think" / "the the meeting" -> one copy
    def _stutter(m: re.Match[str]) -> str:
        removed.append(m.group(0))
        return m.group(1)

    out = _STUTTER.sub(_stutter, out)
    return FillerResult(text=_tidy(out), removed=[r for r in removed if r])


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.?!])", r"\1", s)
    s = re.sub(r"([,.?!])(?:\s*,)+", r"\1", s)  # ".," / ", ," -> "." / ","
    s = re.sub(r"^[\s,.;]+", "", s)  # leading punctuation
    s = s.strip()
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s


# --------------------------------------------------------------------------- #
# 3. Whisper hallucinations / empty turns
# --------------------------------------------------------------------------- #

_HALLUCINATIONS = {
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subscribe",
    "you",
    "bye",
    "okay",
    ".",
}


def is_hallucination(text: str) -> bool:
    norm = re.sub(r"[^\w\s]", "", text).strip().lower()
    return norm == "" or norm in _HALLUCINATIONS


def is_repetition_loop(text: str) -> bool:
    """Whisper on noise: "Me. Me. Me. Me." -- one short word repeated 3+ times in a row."""
    return bool(re.search(r"\b(\w{1,6})\b(?:[\s,.!?]+\1\b){2,}", text, re.IGNORECASE))


def is_prompt_echo(text: str, glossary: "Glossary") -> bool:
    """Whisper repeating its own vocabulary prompt instead of real speech, e.g.
    "Vocabulary Mere, M.D." or "Gnani, Microsoft Teams, WFH, OOO, PTO, TBD"."""
    norm = text.strip().lower()
    if norm.startswith(("vocabulary", "voculars", "vocab")) or norm.startswith("we talked about"):
        return True
    terms = {t.lower() for t in glossary.entities} | {a.lower() for a in glossary.abbreviations}
    parts = [p.strip(" .?!").lower() for p in re.split(r"[,;]", text) if p.strip(" .?!")]
    if len(parts) >= 3:
        hits = sum(p in terms for p in parts)
        if hits >= 3 and hits / len(parts) >= 0.5:
            return True
    return False


_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def is_not_english(text: str) -> bool:
    """The speaker used Hindi and Whisper wrote it in Devanagari: nothing to translate."""
    letters = re.findall(r"[^\W\d_]", text)
    return bool(letters) and sum(bool(_DEVANAGARI.match(c)) for c in letters) / len(letters) > 0.3


# --------------------------------------------------------------------------- #
# 4. Incomplete-sentence detection (partial streaming input)
# --------------------------------------------------------------------------- #

_DANGLING = {
    # conjunctions, prepositions, articles, possessives, auxiliaries: a sentence that
    # ends on one of these is almost certainly unfinished. Kept conservative on purpose
    # (no pronouns / "in" / "on" / "that"), because a false "incomplete" adds latency.
    "and", "or", "but", "because", "to", "the", "a", "an", "of", "with", "for", "if",
    "my", "your", "our", "their", "his", "its", "is", "are", "was", "were", "will",
    "can", "could", "would", "should", "about", "from", "into", "than", "at", "unless",
    "although", "whether",
}


def looks_incomplete(text: str) -> bool:
    """True if the utterance obviously stops mid-sentence ("move the meeting to")."""
    t = text.strip()
    if not t:
        return False
    if t.endswith(("?", "!")):
        return False
    # Whisper often adds a period even when the speaker trailed off, so ignore it.
    last = re.sub(r"[^\w']", "", t.rstrip(".").split()[-1]).lower() if t.rstrip(".") else ""
    return last in _DANGLING


# --------------------------------------------------------------------------- #
# 5. Named-entity masking
# --------------------------------------------------------------------------- #


def mask_entities(text: str, glossary: Glossary) -> tuple[str, dict[str, str]]:
    """Replace glossary entities with [[n]] tokens. Returns (masked_text, token->entity)."""
    mapping: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for ent in sorted(glossary.entities, key=len, reverse=True):
        rx = _word_re(ent)

        def _sub(m: re.Match[str], ent: str = ent) -> str:
            if ent not in reverse:
                tok = f"[[{len(mapping) + 1}]]"
                mapping[tok] = ent
                reverse[ent] = tok
            return reverse[ent]

        text = rx.sub(_sub, text)
    return text, mapping


def unmask_entities(text: str, mapping: dict[str, str]) -> str:
    for tok, ent in mapping.items():
        text = text.replace(tok, ent)
    return text


class StreamingUnmasker:
    """Unmasks [[n]] tokens in a token stream where a token may be split across chunks
    ("[[" + "1]]"). Holds back only the minimum needed text."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.buf = ""

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        self.buf = unmask_entities(self.buf, self.mapping)
        # hold back a possible partial token at the end ("[", "[[", "[[1", "[[1]")
        m = re.search(r"\[(?:\[\d*\]?)?$", self.buf)
        if m:
            out, self.buf = self.buf[: m.start()], self.buf[m.start():]
        else:
            out, self.buf = self.buf, ""
        return out

    def flush(self) -> str:
        out, self.buf = unmask_entities(self.buf, self.mapping), ""
        return out


# --------------------------------------------------------------------------- #
# 6. Localization hints: times, dates, currency, abbreviations
# --------------------------------------------------------------------------- #

_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?\s?m\.?|p\.?\s?m\.?)(?=\W|$)", re.IGNORECASE
)


def _day_period(hour24: int) -> str:
    if 4 <= hour24 < 12:
        return "सुबह"
    if 12 <= hour24 < 16:
        return "दोपहर"
    if 16 <= hour24 < 20:
        return "शाम"
    return "रात"


def hindi_time(hour: int, minute: int, meridiem: str) -> str:
    """'5', 0, 'pm' -> 'शाम 5 बजे';  '5', 30, 'pm' -> 'शाम साढ़े 5 बजे'."""
    pm = meridiem.lower().startswith("p")
    h12 = hour % 12
    hour24 = h12 + (12 if pm else 0)
    period = _day_period(hour24)
    disp = h12 or 12
    nxt = disp % 12 + 1
    if minute == 0:
        clock = f"{disp} बजे"
    elif minute == 30:
        clock = {1: "डेढ़ बजे", 2: "ढाई बजे"}.get(disp, f"साढ़े {disp} बजे")
    elif minute == 15:
        clock = f"सवा {disp} बजे"
    elif minute == 45:
        clock = f"पौने {nxt} बजे"
    else:
        clock = f"{disp} बजकर {minute} मिनट"
    return f"{period} {clock}"


def hindi_clock(hour: int, minute: int) -> str:
    """Clock time without a day period, digits kept for the screen:
    5:30 -> 'साढ़े 5 बजे', 10:15 -> 'सवा 10 बजे', 2:45 -> 'पौने 3 बजे', 9:10 -> '9 बजकर 10 मिनट'.
    24-hour times (17:40) get the day period since it is known."""
    h12 = hour % 12 or 12
    period = f"{_day_period(hour)} " if hour > 12 else ""
    if minute == 0:
        clock = f"{h12} बजे"
    elif minute == 30:
        clock = {1: "डेढ़ बजे", 2: "ढाई बजे"}.get(h12, f"साढ़े {h12} बजे")
    elif minute == 15:
        clock = f"सवा {h12} बजे"
    elif minute == 45:
        clock = f"पौने {h12 % 12 + 1} बजे"
    else:
        clock = f"{h12} बजकर {minute} मिनट"
    return period + clock


_PLAIN_TIME_RE = re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?![\d:])")

# Speech-to-text often writes times with a dot: "5.30 pm", "at 5.30". Make them "5:30"
# so the time rules (and the translator) see a time, not the decimal number 5.30.
_DOT_TIME_AMPM = re.compile(r"(?<![\d.])(\d{1,2})\.([0-5]\d)(?=\s*(?:a\.?\s?m\.?|p\.?\s?m\.?)(?![a-z]))", re.IGNORECASE)
_DOT_TIME_AFTER_WORD = re.compile(
    r"\b(at|by|till|until|from|around|before|after|about|between|and|or|to)\s+(\d{1,2})\.([0-5]\d)\b"
    r"(?!\s*(?:%|percent|dollars?|rupees?|bucks|lakhs?|crores?|million|billion|km|kg|gb|mb|x\b))",
    re.IGNORECASE,
)


def normalize_clock_punctuation(text: str) -> str:
    """'5.30 pm' -> '5:30 pm'; 'at 5.30' -> 'at 5:30'. Leaves '$5.30' and '5.30 percent' alone."""
    text = _DOT_TIME_AMPM.sub(r"\1:\2", text)
    return _DOT_TIME_AFTER_WORD.sub(r"\1 \2:\3", text)


_FIXED_HINTS = {
    r"\bday after tomorrow\b": "परसों",
    # plain "कल" for both: the verb tense disambiguates. Never put notes in a hint,
    # the LLM copies them into the spoken output.
    r"\btomorrow\b": "कल",
    r"\byesterday\b": "कल",
    r"\btoday\b": "आज",
    r"\btonight\b": "आज रात",
    r"\bnoon\b": "दोपहर 12 बजे",
    r"\bmidnight\b": "रात 12 बजे",
    r"\bmonday\b": "सोमवार",
    r"\btuesday\b": "मंगलवार",
    r"\bwednesday\b": "बुधवार",
    r"\bthursday\b": "गुरुवार",
    r"\bfriday\b": "शुक्रवार",
    r"\bsaturday\b": "शनिवार",
    r"\bsunday\b": "रविवार",
    r"\bnext week\b": "अगले हफ़्ते",
}

_MONTHS = {
    "january": "जनवरी", "february": "फ़रवरी", "march": "मार्च", "april": "अप्रैल",
    "may": "मई", "june": "जून", "july": "जुलाई", "august": "अगस्त",
    "september": "सितंबर", "october": "अक्टूबर", "november": "नवंबर", "december": "दिसंबर",
}
_DATE_RE = re.compile(
    r"\b(?:(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + "|".join(_MONTHS) + r")"
    r"|(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?)\b",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    r"(?:(\$|₹|rs\.?\s?|inr\s?)\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(dollars|rupees|bucks))",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s?(%|percent\b)", re.IGNORECASE)


def localization_hints(text: str, glossary: Glossary) -> list[tuple[str, str]]:
    """Return (english_span, hindi_rendering) pairs the LLM should use verbatim."""
    hints: list[tuple[str, str]] = []

    covered: list[tuple[int, int]] = []
    for m in _TIME_RE.finditer(text):
        h, mnt = int(m.group(1)), int(m.group(2) or 0)
        if 1 <= h <= 12 and 0 <= mnt < 60:
            hints.append((m.group(0).strip().rstrip("."), hindi_time(h, mnt, m.group(3))))
            covered.append(m.span())

    # clock times without AM/PM ("at 5:30") -> "साढ़े 5 बजे" (no day period: we don't know it)
    for m in _PLAIN_TIME_RE.finditer(text):
        if any(a <= m.start() < b for a, b in covered):
            continue
        h, mnt = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mnt < 60:
            hints.append((m.group(0), hindi_clock(h, mnt)))

    for m in _DATE_RE.finditer(text):
        day = m.group(1) or m.group(4)
        month = (m.group(2) or m.group(3)).lower()
        hints.append((m.group(0), f"{int(day)} {_MONTHS[month]}"))

    for m in _MONEY_RE.finditer(text):
        sym, amt = (m.group(1) or "").strip().lower(), m.group(2) or m.group(3)
        word = (m.group(4) or "").lower()
        unit = "डॉलर" if sym == "$" or word in ("dollars", "bucks") else "रुपये"
        hints.append((m.group(0).strip(), f"{amt} {unit}"))

    for m in _PERCENT_RE.finditer(text):
        hints.append((m.group(0), f"{m.group(1)} प्रतिशत"))

    for pat, hi in _FIXED_HINTS.items():
        for m in re.finditer(pat, text, re.IGNORECASE):
            if pat == r"\btomorrow\b" and re.search(r"day after tomorrow", text, re.I):
                continue
            hints.append((m.group(0), hi))

    for abbr, hi in glossary.abbreviations.items():
        if re.search(r"(?<![\w])" + re.escape(abbr) + r"(?![\w])", text):  # case-sensitive
            hints.append((abbr, hi))

    # de-duplicate, keep order
    seen: set[tuple[str, str]] = set()
    return [h for h in hints if not (h in seen or seen.add(h))]


# --------------------------------------------------------------------------- #
# 7. Tone detection
# --------------------------------------------------------------------------- #

_FORMAL = re.compile(
    r"\b(sir|ma'?am|madam|please|kindly|could you|would you|may i|regards|"
    r"appreciate|grateful|sincerely|request|mr|mrs|ms|dr)\b",
    re.IGNORECASE,
)
_CASUAL = re.compile(
    r"\b(hey|yo|dude|bro|buddy|man|gonna|wanna|gotta|ya|yeah|yep|nope|cool|lol|"
    r"awesome|chill|guys|sup|kinda)\b",
    re.IGNORECASE,
)


def detect_tone(text: str, previous: list[str] | None = None, default: str = "formal") -> str:
    """'formal' or 'casual'. Current utterance counts double; recent context breaks ties.
    Default is formal (आप), the safe choice in Hindi."""
    score = 2 * (len(_CASUAL.findall(text)) - len(_FORMAL.findall(text)))
    for p in (previous or [])[-3:]:
        score += len(_CASUAL.findall(p)) - len(_FORMAL.findall(p))
    if score > 0:
        return "casual"
    if score < 0:
        return "formal"
    return default


# --------------------------------------------------------------------------- #
# 8. Hindi number words for TTS
# --------------------------------------------------------------------------- #

_HI_0_99 = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह "
    "अठारह उन्नीस बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस "
    "इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस "
    "तैंतालीस चवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन "
    "चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ "
    "अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर "
    "उनासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे "
    "इक्यानवे बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()
assert len(_HI_0_99) == 100


def hindi_number(n: int) -> str:
    """Integer -> Hindi words using the Indian system (हज़ार, लाख, करोड़)."""
    if n < 0:
        return "माइनस " + hindi_number(-n)
    if n < 100:
        return _HI_0_99[n]
    parts: list[str] = []
    for div, name in ((10_000_000, "करोड़"), (100_000, "लाख"), (1000, "हज़ार"), (100, "सौ")):
        if n >= div:
            q, n = divmod(n, div)
            parts.append(f"{hindi_number(q)} {name}")
    if n:
        parts.append(_HI_0_99[n])
    return " ".join(parts)


_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")


def verbalize_numbers(text: str) -> str:
    """'शाम 5 बजे, 2.5 घंटे, 9876543210' -> Hindi words, so the TTS never says 'five'."""

    def _sub(m: re.Match[str]) -> str:
        tok = m.group(0)
        digits = tok.replace(",", "")
        if "." in digits:
            whole, frac = digits.split(".", 1)
            return f"{hindi_number(int(whole))} दशमलव " + " ".join(_HI_0_99[int(d)] for d in frac)
        if len(digits) >= 10 or (len(digits) > 1 and digits.startswith("0")):
            return " ".join(_HI_0_99[int(d)] for d in digits)  # phone numbers, codes
        return hindi_number(int(digits))

    return _NUM_TOKEN.sub(_sub, text)


class StreamingNumberVerbalizer:
    """verbalize_numbers for a text stream: never splits a number across chunks."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        m = re.search(r"[\d.,]+$", self.buf)
        if m:
            out, self.buf = self.buf[: m.start()], self.buf[m.start():]
        else:
            out, self.buf = self.buf, ""
        return verbalize_numbers(out)

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return verbalize_numbers(out)


# --------------------------------------------------------------------------- #
# 9. Speech normalisation for the Hindi voice (audio only; the screen keeps digits)
# --------------------------------------------------------------------------- #
# TTS voices mispronounce what a Hindi listener expects: "5:30" should be "साढ़े पाँच",
# "₹500" -> "पाँच सौ रुपये", "HR" -> "एचआर", "Google Meet" -> "गूगल मीट".

_LETTER_HI = {
    "A": "ए", "B": "बी", "C": "सी", "D": "डी", "E": "ई", "F": "एफ़", "G": "जी", "H": "एच",
    "I": "आई", "J": "जे", "K": "के", "L": "एल", "M": "एम", "N": "एन", "O": "ओ", "P": "पी",
    "Q": "क्यू", "R": "आर", "S": "एस", "T": "टी", "U": "यू", "V": "वी", "W": "डब्ल्यू",
    "X": "एक्स", "Y": "वाई", "Z": "ज़ेड",
}
_PERIOD_WORDS = ("सुबह", "दोपहर", "शाम", "रात")
_NUM = r"\d[\d,]*(?:\.\d+)?"


def spell_acronym(s: str) -> str:
    """'HR' -> 'एचआर', 'PDF' -> 'पीडीएफ़' (how Hindi speakers say English letters)."""
    return "".join(_LETTER_HI.get(c, c) for c in s.upper())


def clock_words(hour: int, minute: int) -> tuple[str, bool]:
    """(spoken clock, needs 'बजे' after it). 5:30 -> ('साढ़े पाँच', True),
    1:30 -> ('डेढ़', True), 5:45 -> ('पौने छह', True), 5:10 -> ('पाँच बजकर दस मिनट', False)."""
    h12 = hour % 12 or 12
    if minute == 0:
        return hindi_number(h12), True
    if minute == 30:
        return {1: "डेढ़", 2: "ढाई"}.get(h12, f"साढ़े {hindi_number(h12)}"), True
    if minute == 15:
        return f"सवा {hindi_number(h12)}", True
    if minute == 45:
        return f"पौने {hindi_number(h12 % 12 + 1)}", True
    return f"{hindi_number(h12)} बजकर {hindi_number(minute)} मिनट", False


def _has_period_before(text: str, pos: int) -> bool:
    return any(w in text[max(0, pos - 8):pos] for w in _PERIOD_WORDS)


_TTS_TIME = re.compile(
    r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])(\s*[ap]\.?\s?m\.?(?![a-z]))?(\s*बजे)?", re.IGNORECASE
)
_TTS_DOT_TIME = re.compile(
    r"(?<![\d.])(\d{1,2})\.([0-5]\d)(?=\s*(?:बजे|[ap]\.?\s?m\.?(?![a-z])))", re.IGNORECASE
)
_TTS_BAJKAR = re.compile(r"(?<![\d.:])(\d{1,2})\s*बजकर\s*(15|30|45)\s*मिनट")
_TTS_AMPM = re.compile(r"(?<![\d:.])(\d{1,2})\s*([ap])\.?\s?m\.?(?![a-z])(\s*बजे)?", re.IGNORECASE)
_TTS_MONEY = re.compile(r"(₹|rs\.?|inr|\$)\s?(" + _NUM + r")", re.IGNORECASE)
_TTS_PERCENT = re.compile(r"(" + _NUM + r")\s?%")
_UNITS = {
    "km": "किलोमीटर", "kg": "किलो", "gb": "जीबी", "mb": "एमबी", "tb": "टीबी", "kb": "केबी",
    "ms": "मिलीसेकंड", "min": "मिनट", "mins": "मिनट", "hr": "घंटे", "hrs": "घंटे",
    "cm": "सेंटीमीटर", "mm": "मिलीमीटर", "ml": "मिलीलीटर", "mbps": "एमबीपीएस",
}
_TTS_UNIT = re.compile(r"(" + _NUM + r")\s?(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b", re.IGNORECASE)
# Devanagari short forms the LLM sometimes writes ("15 किमी"): the voice reads them literally
_HI_UNITS = {"किमी": "किलोमीटर", "कि.मी.": "किलोमीटर", "किग्रा": "किलोग्राम", "कि.ग्रा.": "किलोग्राम",
             "मि.": "मिनट", "से.मी.": "सेंटीमीटर", "मी.": "मीटर"}
_TTS_HI_UNIT = re.compile(
    r"(" + _NUM + r")\s?(" + "|".join(re.escape(u) for u in sorted(_HI_UNITS, key=len, reverse=True)) + r")(?=\s|[।,.!?]|$)"
)
_TTS_HALF = re.compile(r"(?<![\d.])(\d+)\.5(?!\d)")
_TTS_ACRONYM = re.compile(r"(?<![A-Za-z])([A-Z]{2,6})(?![A-Za-z])")
_TTS_AMP_ACRONYM = re.compile(r"(?<![A-Za-z])([A-Z]{1,3})&([A-Z]{1,3})(?![A-Za-z])")  # R&D, Q&A
_QTY_WORDS = r"लाख|करोड़|हज़ार|हजार|घंटे|घंटा|साल|किलो|लीटर|रुपये|डॉलर|किलोमीटर|मीटर"
_TTS_QUARTER = re.compile(r"(?<![\d.])(\d+)\.(25|75)(?!\d)(\s*(?:" + _QTY_WORDS + r"))")
_TTS_SYMBOLS = [(re.compile(r"\s&\s"), " और "), (re.compile(r"\s\+\s"), " प्लस "), (re.compile(r"@"), " ऐट ")]


def normalize_for_tts(text: str, pronunciations: dict[str, str] | None = None, context: str = "") -> str:
    """Rewrite Hindi text so the voice reads it the way a Hindi speaker would.
    `context` = text spoken just before (streaming), so "शाम" isn't added twice."""
    def _has_period_before_ctx(t: str, pos: int) -> bool:
        return _has_period_before(context + t, len(context) + pos)

    # 1. English names -> Devanagari pronunciation (longest first)
    for name in sorted(pronunciations or {}, key=len, reverse=True):
        text = re.sub(r"(?<![\w])" + re.escape(name) + r"(?![\w])", pronunciations[name], text, flags=re.IGNORECASE)

    # 2a. dotted times the translator copied from speech-to-text: "5.30 बजे" / "5.30 PM" -> "5:30 …"
    text = _TTS_DOT_TIME.sub(r"\1:\2", text)
    # 2b. "5 बजकर 30 मिनट" -> "5:30 बजे" so it is said the natural way ("साढ़े पाँच बजे")
    text = _TTS_BAJKAR.sub(lambda m: f"{m.group(1)}:{int(m.group(2)):02d} बजे", text)

    # 2. clock times, with or without AM/PM: "5:30 PM" -> "शाम साढ़े पाँच बजे"
    def _time(m: re.Match[str]) -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return m.group(0)
        words, needs_baje = clock_words(h, mi)
        ampm = (m.group(3) or "").strip().lower()
        prefix = ""
        if ampm and not _has_period_before_ctx(text, m.start()):
            h24 = h % 12 + (12 if ampm.startswith("p") else 0)
            prefix = _day_period(h24) + " "
        elif not ampm and h > 12 and not _has_period_before_ctx(text, m.start()):
            prefix = _day_period(h) + " "  # 24-hour "17:30"
        if needs_baje:
            return prefix + words + " बजे"
        # "17:40 बजे निकलेगी" -> "पाँच बजकर चालीस मिनट पर निकलेगी" (बजे acted as "at")
        return prefix + words + (" पर" if m.group(4) else "")

    text = _TTS_TIME.sub(_time, text)

    # 3. leftover "5 PM" -> "शाम पाँच बजे"
    def _ampm(m: re.Match[str]) -> str:
        h = int(m.group(1))
        if not 1 <= h <= 12:
            return m.group(0)
        h24 = h % 12 + (12 if m.group(2).lower() == "p" else 0)
        prefix = "" if _has_period_before_ctx(text, m.start()) else _day_period(h24) + " "
        return f"{prefix}{hindi_number(h)} बजे"

    text = _TTS_AMPM.sub(_ampm, text)

    # 4. money, percent, units
    text = _TTS_MONEY.sub(lambda m: f"{m.group(2)} {'डॉलर' if m.group(1) == '$' else 'रुपये'}", text)
    text = _TTS_PERCENT.sub(lambda m: f"{m.group(1)} प्रतिशत", text)
    text = _TTS_UNIT.sub(lambda m: f"{m.group(1)} {_UNITS[m.group(2).lower()]}", text)
    text = _TTS_HI_UNIT.sub(lambda m: f"{m.group(1)} {_HI_UNITS[m.group(2)]}", text)

    # 5. halves the Hindi way: 1.5 -> डेढ़, 2.5 -> ढाई, 3.5 -> साढ़े तीन
    def _half(m: re.Match[str]) -> str:
        n = int(m.group(1))
        return {0: "आधा", 1: "डेढ़", 2: "ढाई"}.get(n, f"साढ़े {hindi_number(n)}")

    text = _TTS_HALF.sub(_half, text)
    # quarters before a quantity word: 3.75 करोड़ -> पौने चार करोड़, 1.25 लाख -> सवा एक लाख
    def _quarter(m: re.Match[str]) -> str:
        n, frac = int(m.group(1)), m.group(2)
        return (f"सवा {hindi_number(n)}" if frac == "25" else f"पौने {hindi_number(n + 1)}") + m.group(3)

    text = _TTS_QUARTER.sub(_quarter, text)
    # "5:30 PM पर" -> "साढ़े पाँच बजे पर" is unnatural: Hindi says "साढ़े पाँच बजे"
    text = re.sub(r"बजे\s+पर\b", "बजे", text)

    # 6. symbols, English acronyms spelled the Hindi way, then all remaining digits
    for rx, word in _TTS_SYMBOLS:
        text = rx.sub(word, text)
    text = _TTS_AMP_ACRONYM.sub(lambda m: f"{spell_acronym(m.group(1))} एंड {spell_acronym(m.group(2))}", text)
    text = _TTS_ACRONYM.sub(lambda m: spell_acronym(m.group(1)), text)
    return verbalize_numbers(text)


class StreamingTTSNormalizer:
    """normalize_for_tts for a token stream.

    Releases text only up to a word boundary, and also holds back a trailing run of
    English / number words, so "5:30" + " PM", "₹" + "500" and "Google" + " Meet" are
    normalised together. Costs at most a couple of words of delay."""

    _HOLD_WORD = re.compile(r"[A-Za-z\d₹$%:]")

    def __init__(self, pronunciations: dict[str, str] | None = None) -> None:
        self.buf = ""
        self.spoken = ""  # tail of what was already released (for "शाम" context)
        self.pron = pronunciations or {}

    def _release(self, out: str) -> str:
        res = normalize_for_tts(out, self.pron, context=self.spoken)
        self.spoken = (self.spoken + out)[-16:]
        return res

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        cut = max(self.buf.rfind(" "), self.buf.rfind("\n"))  # last complete word ends here
        if cut <= 0:
            return ""
        # walk back over trailing English/number words: they may continue in the next chunk
        words = list(re.finditer(r"\S+", self.buf[:cut]))
        while words and (self._HOLD_WORD.search(words[-1].group(0)) or words[-1].group(0) in ("बजकर", "बजे", "मिनट")):
            cut = words[-1].start()
            words.pop()
        if cut <= 0:
            return ""
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return self._release(out)

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return self._release(out) if out else ""


# --------------------------------------------------------------------------- #
# Everything together
# --------------------------------------------------------------------------- #


@dataclass
class PreparedTurn:
    raw: str
    cleaned: str  # English after ASR fixes + filler removal (shown in the UI)
    masked: str  # what the LLM actually sees
    entities: dict[str, str]
    hints: list[tuple[str, str]]
    tone: str
    removed_fillers: list[str]
    skip: bool  # nothing worth translating (only fillers / hallucination)
    incomplete: bool
    skip_reason: str = ""
    asr_fixes: list[str] = field(default_factory=list)  # "google meat → Google Meet"


_LOOP_RE = re.compile(r"\b(\w{1,6})\b(?:[\s,.!?]+\1\b){2,}[.!?]?", re.IGNORECASE)


def strip_noise_sentences(text: str) -> str:
    """Remove Whisper noise artifacts from a longer utterance: repetition loops
    ("Me. Me. Me. Me.") and stock phrases ("Thank you.") tacked onto real speech."""
    text = _LOOP_RE.sub(" ", text)
    sentences = re.split(r"(?<=[.?!])\s+", text.strip())
    if len(sentences) > 1:
        sentences = [s for s in sentences if not is_hallucination(s)]
    return _tidy(" ".join(sentences))


def prepare_turn(
    raw: str,
    glossary: Glossary,
    tone_mode: str = "auto",
    previous: list[str] | None = None,
) -> PreparedTurn:
    reason = ""
    if is_not_english(raw):
        reason = "Hindi speech: input language is English"
    elif is_prompt_echo(raw, glossary):
        reason = "ASR echoed its vocabulary hint (noise / silence)"
    denoised = strip_noise_sentences(raw)
    asr_fixes = [
        f"{wrong} → {right}"
        for wrong, right in glossary.asr_corrections.items()
        if _word_re(wrong).search(denoised)
    ]
    fixed = normalize_clock_punctuation(fix_asr_errors(denoised, glossary))
    fr = remove_fillers(fixed)
    cleaned = fr.text
    if not reason and is_hallucination(cleaned):
        reason = "only fillers" if fr.removed else "noise / no speech"
    skip = bool(reason)
    masked, entities = mask_entities(cleaned, glossary)
    tone = tone_mode if tone_mode in ("formal", "casual") else detect_tone(cleaned, previous)
    return PreparedTurn(
        raw=raw,
        cleaned=cleaned,
        masked=masked,
        entities=entities,
        hints=localization_hints(cleaned, glossary),
        tone=tone,
        removed_fillers=fr.removed,
        skip=skip,
        incomplete=looks_incomplete(cleaned),
        skip_reason=reason,
        asr_fixes=asr_fixes,
    )
