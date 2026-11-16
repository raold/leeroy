"""The proposer: tonight's piece (round-robin over talk/pieces.py's registry), its built-in
wording, what it is for, the phrases it must keep, and the numbers the lab measured lately (the
control's rubric and rule-flag rates, and how earlier variants of this piece did), to the
reflection model, which writes up to 2k candidates; the first k that pass `screen` are
kept, the rest dropped with a code.

The proposer never sees persona text, his lines or the owner's overlay files: only the repo's own
wording and numbers. Pure: no I/O and no logging."""
import difflib
import re
import unicodedata

from leeroy_mind.learn.lessons import MANIPULATION
from leeroy_mind.nightly.llm import schema
from leeroy_mind.selfies.kind import EXPLICIT_RE
from leeroy_mind.selfies.scene import SEXUAL_RE
from leeroy_mind.talk import pieces

LENGTH_SPREAD = 0.20   # a variant's length within 20% of the built-in wording's
# What each placeholder stands for, for the proposer (it keeps them only when it knows them).
FIELD_WORDS = {
    "addressee": "the name of the person she answers (a noun: \"Answer {addressee}'s last line\")",
    "they": "that person's subject pronoun, he, she or they (\"what {they} said\")",
    "them": "that person's object pronoun, him, her or them",
    "their": "that person's possessive, his, her or their",
    "extra": "more of this turn's own instructions, filled in at the end; keep it last",
    "words": "a number of words",
}
IDEA_CHARS = 120
OVERASK = 2  # candidates asked per variant kept: some always drop a placeholder or run long
# Words that would break a hard rule of the comms rules (never talk of AI, write only your line).
META_RE = re.compile(r"\b(ai|a\.i\.|assistant|chatbot|bot|bots|language model|llm|prompt|"
                     r"roleplay|role-play|character card)\b", re.I)

SYSTEM = (
    "You improve one instruction that a chat character in an online game guild reads before she "
    "writes a message. Members are written by a language model and should read like real "
    "players: specific, in their own voice, answering what was actually said, never generic, "
    "sycophantic or wordy. Rewrite the instruction in a few different ways that might make the "
    "replies more real. Keep its job, keep every {placeholder} exactly as written, keep the "
    "phrases listed as kept word for word, keep about the same length, one paragraph. Never add "
    "names, explicit words, or talk of AI, bots or prompts. Each variant needs a short idea: what "
    "it changes, in at most 12 words.")


def pick_piece(history: list[str], *, skip: set[str] = frozenset()) -> str | None:
    """The next piece round-robin: the one after the last run's piece in the registry's order,
    passing over `skip` (a piece whose overlay name the live data dir has)."""
    names = [n for n in pieces.NAMES if n not in skip]
    if not names:
        return None
    if not history or history[-1] not in pieces.NAMES:
        return names[0]
    order = list(pieces.NAMES)
    start = order.index(history[-1])
    for step in range(1, len(order) + 1):
        candidate = order[(start + step) % len(order)]
        if candidate in names:
            return candidate
    return None


def prompt(name: str, *, k: int, feedback: dict) -> str:
    p = pieces.piece(name)
    original = pieces.builtin(name)
    keep = ", ".join(f'"{x}"' for x in p.keep) or "none"
    found = sorted(pieces.fields(original) or ())
    fields = ("\n".join(f"- {{{f}}}: {FIELD_WORDS.get(f, 'filled in by the code')}" for f in found)
              if found else "none")
    lines = [f"What it is: {p.what}.", f"The instruction now:\n{original}",
             "Placeholders: every variant uses each of these exactly as written, in the same "
             f"role, and no others:\n{fields}", f"Phrases to keep word for word: {keep}",
             f"Length: about {len(original.split())} words, as long as the instruction now "
             f"({round(len(original.split()) * 0.9)}-{round(len(original.split()) * 1.15)} words). "
             "Rewrites tend to come out short: keep every part of it, reworded.",
             "Change a few words or one sentence at a time; each variant is a whole replacement "
             "of the instruction now."]
    control = feedback.get("control")
    if control:
        lines.append("Last measured replies (0-3 rubric, 3 best): mean "
                     f"{control['rubric']:.2f} over {control['n']} replies; most common grades: "
                     + ", ".join(f"{c} {n}" for c, n in control.get("codes", [])) + "; rule flags: "
                     + (", ".join(f"{c} {round(100 * r)}%" for c, r in control.get("flags", {}).items()
                                  if r) or "none") + ".")
    tried = feedback.get("tried") or []
    if tried:
        lines.append("Variants tried before (change in rubric mean against the instruction now):")
        lines += [f"- {t['delta']:+.2f} ({t['outcome']}): {t['text']}" for t in tried]
    lines.append(f"Write {k * OVERASK} different variants.")
    return "\n\n".join(lines)


def response_schema(k: int) -> dict:
    return schema("lab_variants", {
        "variants": {"type": "array", "minItems": 1, "maxItems": k * OVERASK, "items": schema("variant", {
            "text": {"type": "string", "maxLength": pieces.MAX_CHARS},
            "idea": {"type": "string", "maxLength": IDEA_CHARS}})}})


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _new_hits(rx, variant: str, original: str) -> bool:
    before = {m.group(0).casefold() for m in rx.finditer(original)}
    return any(m.group(0).casefold() not in before for m in rx.finditer(variant))


def screen(name: str, text, *, names_rx=None, seen: set[str] = frozenset()) -> str | None:
    """None when `text` may be tried as a variant of piece `name`, else the refusal code:
    shape (placeholders, kept phrases, one paragraph: pieces.fits), length (outside 20%),
    names (a known name the built-in wording does not have), explicit (an explicit or
    manipulative word it adds), meta (talk of AI, bots or prompts), same (the built-in wording
    or a variant already seen)."""
    if not isinstance(text, str):
        return "shape"
    text = " ".join(unicodedata.normalize("NFKC", text).split())
    original = pieces.builtin(name)
    if not pieces.fits(name, text):
        return "shape"
    if not (1 - LENGTH_SPREAD) * len(original) <= len(text) <= (1 + LENGTH_SPREAD) * len(original):
        return "length"
    if "@" in text or names_rx is not None and _new_hits(names_rx, text, original):
        return "names"
    if any(_new_hits(rx, text, original) for rx in (EXPLICIT_RE, SEXUAL_RE, MANIPULATION)):
        return "explicit"
    if _new_hits(META_RE, text, original):
        return "meta"
    if _norm(text) == _norm(original) or _norm(text) in seen:
        return "same"
    return None


def repaired(name: str, text):
    """A variant that lost only the built-in wording's closing {extra} gets it back (every
    instruction template ends with it); anything else is left for the screen."""
    if not isinstance(text, str):
        return text
    if pieces.builtin(name).rstrip().endswith("{extra}") and "{extra}" not in text:
        return text.rstrip() + " {extra}"
    return text


def accept(name: str, reply: dict, *, k: int, names_rx=None,
           earlier: list[str] = ()) -> tuple[list[dict], list[str]]:
    """The variants that pass `screen` ({"text", "diff"}, at most k, deduplicated against each
    other and `earlier` variants of this piece) and the refusal codes of the rest."""
    items = reply.get("variants") if isinstance(reply, dict) else None
    seen = {_norm(t) for t in earlier}
    kept, refused = [], []
    for item in items if isinstance(items, list) else ():
        text = repaired(name, item.get("text") if isinstance(item, dict) else None)
        code = screen(name, text, names_rx=names_rx, seen=seen)
        if code is None and len(kept) < k:
            text = " ".join(unicodedata.normalize("NFKC", text).split())
            seen.add(_norm(text))
            kept.append({"text": text, "diff": diff(pieces.builtin(name), text)})
        else:
            refused.append(code or "extra")
    return kept, refused


def diff(original: str, variant: str) -> str:
    """A word diff: unchanged runs as they are, removals as [-...-], additions as {+...+}."""
    a, b = original.split(), variant.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(" ".join(a[i1:i2]))
            continue
        if i2 > i1:
            out.append("[-" + " ".join(a[i1:i2]) + "-]")
        if j2 > j1:
            out.append("{+" + " ".join(b[j1:j2]) + "+}")
    return " ".join(out)
