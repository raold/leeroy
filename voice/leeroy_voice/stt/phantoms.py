"""The hallucination guard, the same for every recognizer (draft section 6.4): recognizers invent
short stock phrases on silence and noise ("Thank you."), and the gate passes whatever Silero takes
for speech. is_phantom() is True, and the press is dropped, for a transcript
- with under MIN_SPEECH_MS of speech (the gate's minimum, so a gate set lower changes nothing);
- under min_confidence, whatever its length (M11);
- with no words;
- that is one of PHANTOMS as a whole (its words, whatever their case and punctuation) under
  PHANTOM_MS of speech; with 1 s or more it is kept, since he may have said it."""
import re

from . import MIN_SPEECH_MS, Transcript

PHANTOM_MS = 1000
PHANTOMS = frozenset({"thank you", "thanks for watching", "you", "bye", "okay",
                      "thank you so much", "please subscribe"})
_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")  # letters and digits, apostrophes inside


def phrase(text: str) -> str:
    """The words of `text`, casefolded, one space apart: what is matched against PHANTOMS."""
    return " ".join(_WORD.findall(text.replace("\u2019", "'").casefold()))


def is_phantom(t: Transcript, *, min_confidence: float) -> bool:
    if t.speech_ms < MIN_SPEECH_MS or t.confidence < min_confidence:
        return True
    words = phrase(t.text)
    return not words or (words in PHANTOMS and t.speech_ms < PHANTOM_MS)
