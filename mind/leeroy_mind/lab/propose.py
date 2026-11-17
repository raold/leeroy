"""The proposer: tonight's piece (round-robin over talk/pieces.py's registry), its built-in
wording, what it is for, the phrases it must keep, and the numbers the lab measured lately (the
control's rubric and rule-flag rates, and how earlier variants of this piece did), to the
reflection model, which writes up to 2k candidates; the first k that pass `screen` are
kept, the rest dropped with a code. When fewer than k pass, the night asks once more with what
was wrong with each (`problem`, `retry_prompt`).

The first live night (2026-11-17) lost all six candidates to one cause: every one reworded the
piece's kept phrase ("in one short line" became "Keep it brief."), because the prompt named the
phrase once, mid-list, and then said "keep every part of it, reworded". So the prompt now says
why the phrase is locked, shows a valid variant of this piece (EXAMPLES) and ends with a
checklist of the exact placeholders and phrases, and the screen's codes tell a lost phrase
(`kept`) and a placeholder (`placeholders`) from a broken shape.

`authored`: a hand-written variant (lab/authored.py) passes the same screen but for its length:
it may be shorter, and longer by half or AUTHORED_EXTRA characters.

The proposer never sees persona text, his lines or the owner's overlay files: only the repo's own
wording and numbers. Pure: no I/O and no logging."""
import difflib
import math
import re
import unicodedata

from leeroy_mind.learn.lessons import MANIPULATION
from leeroy_mind.nightly.llm import schema
from leeroy_mind.selfies.kind import EXPLICIT_RE
from leeroy_mind.selfies.scene import SEXUAL_RE
from leeroy_mind.talk import pieces

LENGTH_SPREAD = 0.20   # a variant at most 20% longer than the built-in wording
LENGTH_SHORTER = 0.40  # ... and at most 40% shorter (the proposer's rewrites run 20% short)
LENGTH_SLACK = 30      # ... or this many characters either way, whichever is more
AUTHORED_EXTRA = 120   # a hand-written variant: at most half as long again, or this much longer
# What each placeholder stands for, for the proposer (it keeps them only when it knows them).
FIELD_WORDS = {
    "addressee": "the name of the person she answers (a noun: \"Answer {addressee}'s last line\")",
    "they": "that person's subject pronoun, he, she or they (\"what {they} said\")",
    "them": "that person's object pronoun, him, her or them",
    "their": "that person's possessive, his, her or their",
    "extra": "more of this turn's own instructions, filled in at the end; keep it last",
    "words": "a number of words",
    "name": "the owner's name, as the guild calls him",
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
    "replies more real. Each variant is a whole replacement, and the code refuses one that "
    "breaks any of these: every {placeholder} of the instruction is in it exactly as written, "
    "braces included, and no other; every locked phrase is in it letter for letter (other code "
    "finds the phrase by those exact words, so a synonym breaks it); it is one paragraph of "
    "about the same length. Never add names, explicit words, or talk of AI, bots or prompts. "
    "Each variant needs a short idea: what it changes, in at most 12 words.")

# A valid variant of each piece, for the form: the proposer sees how the placeholders and the
# locked phrases stay while the words around them change. Never tried itself (judge's `seen`).
EXAMPLES = {
    "reply_human": ("{addressee} just said something to you. Say back what you'd really say to "
                    "what {they} said, in one short line. A memory or an old joke only when it "
                    "belongs there. {extra}"),
    "continue": ("Chime in on {addressee}'s last line only when you have something of your own: "
                 "a reaction, a jab, a different view or a thing you both remember. Agreeing or "
                 "repeating it isn't worth a line. If you'd let it go, answer (nothing). {extra}"),
    "goodnight": "{addressee} is done for tonight. Say goodnight the way you do, in a few words. {extra}",
    "situation_reply_human": "Say what you'd say back to {addressee}'s last line. {extra}",
    "discord_dm": ("This is your private Discord DM, not the game. Match the moment: when {they} "
                   "wrote a quick line, a quick line back, and more only when there is more you "
                   "want to say (at most about {words} words). One paragraph, no line breaks."),
    "romance_hook": ("Tie it to what's going on right now (the pull, the loot, how late it is), "
                     "never a line you could have saved up."),
    "romance_lead": ("With {name} you're still you, the way you are with a close friend: first "
                     "answer what {they} just said, in your own words, and let your day and your "
                     "life in as much as {them}. Flirt back when {their} line flirts, and go "
                     "further only where {their} lines go."),
    "beat_ask": "Before you answer, stop a second and check in with yourself.",
}


def pick_piece(history: list[str], *, skip: set[str] = frozenset(),
               prefer: set[str] = frozenset()) -> str | None:
    """The next piece round-robin: the one after the last run's piece in the registry's order,
    passing over `skip` (a piece whose overlay name the live data dir has, or one this mind's
    settings never render: pieces.unrendered). While any piece of `prefer` (those with a
    hand-written variant waiting, lab/authored.py) is left, the round-robin is among them."""
    names = [n for n in pieces.NAMES if n not in skip]
    if any(n in prefer for n in names):
        names = [n for n in names if n in prefer]
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


def _quoted(items) -> str:
    return ", ".join(f'"{x}"' for x in items)


def length_band(name: str, *, authored: bool = False) -> tuple[int, int]:
    """The characters a variant of `name` may have (inclusive)."""
    n = len(pieces.builtin(name))
    if authored:
        return 1, min(pieces.MAX_CHARS, n + max(n // 2, AUTHORED_EXTRA))
    return (max(1, math.ceil(n - max(LENGTH_SHORTER * n, LENGTH_SLACK))),
            int(n + max(LENGTH_SPREAD * n, LENGTH_SLACK)))


def prompt(name: str, *, k: int, feedback: dict) -> str:
    p = pieces.piece(name)
    original = pieces.builtin(name)
    found = sorted(pieces.fields(original) or ())
    fields = ("\n".join(f"- {{{f}}}: {FIELD_WORDS.get(f, 'filled in by the code')}" for f in found)
              if found else "none")
    words = len(original.split())
    most = int(words * length_band(name)[1] / len(original))
    lines = [f"What it is: {p.what}.", f"The instruction now:\n{original}",
             "Placeholders: every variant uses each of these exactly as written, in the same "
             f"role, and no others:\n{fields}"]
    if p.keep:
        lines.append(f"Locked phrases: {_quoted(p.keep)}. Copy each into every variant letter "
                     "for letter, and reword only what is around it. The code looks for these "
                     "exact words to change the length for other chats, so a variant that says "
                     "the same in other words is refused.")
    lines += [f"Length: about {words} words, as long as the instruction now (at most {most}). "
              "Rewrites tend to come out short: keep everything it asks for.",
              "Change one thing at a time: what she pays attention to, the order, or how one "
              "sentence puts it. A variant that only swaps a word for its synonym teaches "
              "nothing."]
    example = EXAMPLES.get(name)
    if example:
        lines.append("A valid variant, to show the form (the placeholders and locked phrases "
                     f"kept, the rest reworded); write your own, not this one:\n{example}")
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
    lines.append(f"Write {k * OVERASK} different variants. " + checklist(name))
    return "\n\n".join(lines)


def checklist(name: str) -> str:
    """The last thing the proposer reads: what every variant must hold, by its exact words."""
    found = sorted(pieces.fields(pieces.builtin(name)) or ())
    parts = []
    if found:
        parts.append("it has " + ", ".join(f"{{{f}}}" for f in found) + " and no other {placeholder}")
    keep = pieces.piece(name).keep
    if keep:
        parts.append("it has " + _quoted(keep) + " exactly")
    parts.append("it is one paragraph")
    return "Before you answer, check each one: " + "; ".join(parts) + "."


WORDS = {"shape": "it must be one paragraph of plain text, with no line break and no |",
         "names": "it adds a name or an @",
         "explicit": "it adds an explicit or manipulative word",
         "meta": "it talks of AI, bots or prompts",
         "same": "it is the instruction now, the example or a variant already tried",
         "extra": None}


def problem(name: str, text, code: str) -> str | None:
    """Why the screen refused `text` with `code`, in words for the proposer's second try; None
    for a variant refused only because enough had passed."""
    if code == "placeholders":
        want = pieces.fields(pieces.builtin(name)) or set()
        got = pieces.fields(text) if isinstance(text, str) else None
        if got is None:
            return "its braces do not parse: use { and } only around the placeholders"
        parts = []
        if want - got:
            parts.append("it lost " + ", ".join(f"{{{f}}}" for f in sorted(want - got)))
        if got - want:
            parts.append("it adds " + ", ".join(f"{{{f}}}" for f in sorted(got - want)))
        return " and ".join(parts) or "its placeholders differ"
    if code == "kept":
        lost = [k for k in pieces.piece(name).keep if not isinstance(text, str) or k not in text]
        return f"it lost the locked phrase {_quoted(lost)}: copy it letter for letter"
    if code == "length":
        low, high = length_band(name)
        return f"it is {len(' '.join(str(text).split()))} characters: keep it within {low}-{high}"
    return WORDS.get(code, "it was refused")


def retry_prompt(name: str, first: str, reply: dict, codes: list, *, k: int) -> str:
    """The second (and last) ask: the first prompt, what was wrong with each refused variant,
    and how many more are wanted."""
    items = reply.get("variants") if isinstance(reply, dict) else None
    rows = []
    for item, code in zip(items if isinstance(items, list) else (), codes):
        if code is None:
            continue
        text = repaired(name, item.get("text") if isinstance(item, dict) else None)
        why = problem(name, text, code)
        if why:
            rows.append(f"- one of them: {why}")
    rows = list(dict.fromkeys(rows))
    return (first + "\n\nYour earlier variants were refused by the code:\n" + "\n".join(rows)
            + f"\n\nWrite {k * OVERASK} new variants without those faults. " + checklist(name))


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


def screen(name: str, text, *, names_rx=None, seen: set[str] = frozenset(),
           authored: bool = False) -> str | None:
    """None when `text` may be tried as a variant of piece `name`, else the refusal code:
    placeholders (its {placeholders} are not exactly the built-in wording's), kept (a kept
    phrase is not there verbatim), shape (not one paragraph of text, a `|`, over MAX_CHARS),
    length (outside length_band), names (a known name the built-in wording does not have),
    explicit (an explicit or manipulative word it adds), meta (talk of AI, bots or prompts),
    same (the built-in wording or a variant already seen). `authored`: a hand-written variant,
    with its own length band."""
    if not isinstance(text, str) or not text.strip():
        return "shape"
    text = " ".join(unicodedata.normalize("NFKC", text).split())
    original = pieces.builtin(name)
    if pieces.fields(text) != pieces.fields(original):
        return "placeholders"
    if not all(k in text for k in pieces.piece(name).keep):
        return "kept"
    if not pieces.fits(name, text):
        return "shape"
    low, high = length_band(name, authored=authored)
    if not low <= len(text) <= high:
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
    other, `earlier` variants of this piece and the prompt's example) and the refusal codes of
    the rest."""
    kept, codes = judge(name, reply, k=k, names_rx=names_rx, earlier=earlier)
    return kept, [c for c in codes if c is not None]


def judge(name: str, reply: dict, *, k: int, names_rx=None,
          earlier: list[str] = ()) -> tuple[list[dict], list]:
    """`accept`, with one code per variant of the reply in its order (None: kept)."""
    items = reply.get("variants") if isinstance(reply, dict) else None
    seen = {_norm(t) for t in earlier}
    if name in EXAMPLES:
        seen.add(_norm(EXAMPLES[name]))
    kept, codes = [], []
    for item in items if isinstance(items, list) else ():
        text = repaired(name, item.get("text") if isinstance(item, dict) else None)
        code = screen(name, text, names_rx=names_rx, seen=seen)
        if code is None and len(kept) < k:
            text = " ".join(unicodedata.normalize("NFKC", text).split())
            seen.add(_norm(text))
            kept.append({"text": text, "diff": diff(pieces.builtin(name), text)})
            codes.append(None)
        else:
            codes.append(code or "extra")
    return kept, codes


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
