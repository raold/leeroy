"""The prompt pieces the conversation lab may vary (lab/README.md), and how a variant reaches a
prompt: at render time, by name, the way an overlay does.

Each piece is one built-in wording the repo owns: an instruction template, the Discord DM wording,
the romance lead's hook, the beat's ask. A variant never edits that text. While a turn composes
(TalkEngine._compose), `using` puts the texts in force for it in a context variable; the code that
renders a piece asks `text(name, builtin)` and gets the variant or the built-in wording, and the
piece is marked as rendered either way, so the learning credit can tell a turn that read it from
one that never did. Outside `using` (every turn of the live mind with no lab arm, the night, the
console's preview) `text` returns the built-in wording and marks nothing: the prompts and the
template trace stay byte-identical.

Two sources fill `using`:
- a sandbox mind with `[lab] overlays = true` reads `prompts/lab_<piece>.txt` from its own copied
  data dir (lab/child.py writes them there for a variant's arm, never in the live data dir);
- the live mind's learn bandit, for a lab arm promoted into it (lab/live.py).

A text that drops a placeholder or a required phrase, or adds one, is never used (`fits`)."""
import string
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

OVERLAY_PREFIX = "lab_"
MAX_CHARS = 1200


@dataclass(frozen=True)
class Piece:
    name: str
    what: str                     # what the piece does, for the proposer (no persona text)
    keep: tuple[str, ...] = ()    # phrases other code relies on, kept verbatim in a variant


def _instruction(kind: str) -> str:
    from leeroy_mind.talk.prompt import INSTRUCTIONS
    from leeroy_mind.talk.turns import TurnKind
    return INSTRUCTIONS[TurnKind(kind)]


def _situation(kind: str) -> str:
    from leeroy_mind.talk.prompt import SITUATION_INSTRUCTIONS
    from leeroy_mind.talk.turns import TurnKind
    return SITUATION_INSTRUCTIONS[TurnKind(kind)]


def _discord_dm() -> str:
    from leeroy_mind.talk.prompt import DISCORD_DM
    return DISCORD_DM


def _romance_hook() -> str:
    from leeroy_mind.talk.romance import SPECIFIC
    return SPECIFIC


def _romance_lead() -> str:
    from leeroy_mind.talk.romance import REALISM_LEAD
    return REALISM_LEAD


def _beat_ask() -> str:
    from leeroy_mind.talk.beat import MARK
    return MARK


# The registry: name -> (piece, its built-in wording). The keep phrases are the ones render code
# finds by their words (render_instruction's DISCORD_DROPS and MORE_LINES, the (nothing) answer).
PIECES: dict[str, tuple[Piece, object]] = {
    "reply_human": (Piece("reply_human", "the instruction a member reads last when she answers "
                          "the owner's line in game chat, a whisper or a DM",
                          ("in one short line",)), lambda: _instruction("reply_human")),
    "continue": (Piece("continue", "the instruction for chiming in after someone else's line in "
                       "a group chat, or letting it go", ("(nothing)",)),
                 lambda: _instruction("continue")),
    "goodnight": (Piece("goodnight", "the instruction for saying goodnight when the owner logs "
                        "off", (", in a few words",)), lambda: _instruction("goodnight")),
    "situation_reply_human": (Piece("situation_reply_human", "the short answer instruction of "
                                    "the situation prompt (no length of its own)"),
                              lambda: _situation("reply_human")),
    "discord_dm": (Piece("discord_dm", "how long and how fully a member writes in her private "
                         "Discord DM with the owner"), _discord_dm),
    "romance_hook": (Piece("romance_hook", "the last sentence of a woman's romance lead toward "
                           "the owner: tie it to what is happening, never a canned line"),
                     _romance_hook),
    "romance_lead": (Piece("romance_lead", "the closing lead of a woman's romance text toward the "
                           "owner with [romance] realism: answer him first, stay herself, follow "
                           "his lead"), _romance_lead),
    "beat_ask": (Piece("beat_ask", "the first sentence of the short think-first step before she "
                       "answers his whisper or DM"), _beat_ask),
}
NAMES = tuple(PIECES)


def piece(name: str) -> Piece:
    return PIECES[name][0]


def builtin(name: str) -> str:
    return PIECES[name][1]()


def overlay_name(name: str) -> str:
    return OVERLAY_PREFIX + name


def fields(text: str) -> set[str] | None:
    """The {placeholders} of `text`, or None when it does not parse as a format string or uses
    anything but plain names."""
    out = set()
    try:
        for _, field, spec, conv in string.Formatter().parse(text):
            if field is None:
                continue
            if not field.isidentifier() or spec or conv:
                return None
            out.add(field)
    except ValueError:
        return None
    return out


def fits(name: str, text: str) -> bool:
    """`text` may stand in for the piece: its placeholders are exactly the built-in wording's,
    every keep phrase is there verbatim, it is one paragraph without a `|` and at most MAX_CHARS."""
    if name not in PIECES or not isinstance(text, str) or not text.strip():
        return False
    text = text.strip()
    if len(text) > MAX_CHARS or "\n" in text or "|" in text:
        return False
    original = builtin(name)
    if fields(text) != fields(original):
        return False
    return all(k in text for k in piece(name).keep)


_active: ContextVar[dict | None] = ContextVar("lab_pieces", default=None)


@contextmanager
def using(texts: dict[str, str], rendered: set[str]):
    """While the block runs (one turn's composition), `text` gives `texts`' wording for its
    pieces and adds every piece it is asked for to `rendered`. Texts that do not fit are left
    out."""
    kept = {k: v.strip() for k, v in texts.items() if fits(k, v)}
    token = _active.set({"texts": kept, "rendered": rendered})
    try:
        yield kept
    finally:
        _active.reset(token)


def text(name: str, default: str) -> str:
    state = _active.get()
    if state is None or name not in PIECES:
        return default
    state["rendered"].add(name)
    return state["texts"].get(name, default)


def instruction(kind: str, situation: bool, default: str) -> str:
    """An instruction template through `text`, when its kind is a registered piece."""
    name = f"situation_{kind}" if situation else kind
    return text(name, default) if name in PIECES else default


def read_overlays(data_dir) -> dict[str, str]:
    """A sandbox's `prompts/lab_<piece>.txt` overlays, by piece; only those that fit."""
    from leeroy_mind.prompts import read_overlay
    out = {}
    for name in NAMES:
        found = read_overlay(overlay_name(name), data_dir)
        if found is not None and fits(name, found.text):
            out[name] = found.text
    return out
