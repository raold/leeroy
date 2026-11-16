"""The beat before an answer to his whisper or DM (`[talk] think_private`; owner, 2026-11-16: "go
with structured thinking for dms and whispers").

Orion's own thinking (the `think` flag left out; `think: true` is an HTTP 400) cost 5-6 s and
400-550 words a line, mostly a recap of the prompt. Instead the turn makes one short extra call
on its own prompt, constrained by Ollama's `format` to the schema below with `think: false`:
`read` (what he is actually saying, one sentence), `lands` (how it lands on her right now, a few
words), `want` (what she wants from her answer, one of WANTS) and `use` (the number of one
memory or chat line her prompt showed worth bringing in, or null). The line is then asked for as
today, with these as short lines after its instruction (lines). A beat that fails the schema,
times out or comes back empty is skipped and the line goes exactly as today.

Bake-off, 2026-11-16 (47 real turns, blind-judged): with only the names of the wants Orion chose
"ask" on 34 of 47 turns, so each want carries a one-line meaning in the ask and in the schema
("ask" is wanting something from him, not asking a question); and two beats were cut off inside
a long `lands` at num_predict 160, so `[talk] think_budget` is 240 and the ask says how short
`read` and `lands` are.

The owner's live DM test, 2026-11-16 (7 answers, every beat `use` set, 6 of 7 `flirt`): she
raised the same subjects reply after reply and flirted past his questions. So, wherever a beat
runs (any channel, any member), what it may bring in follows four rules: the items are shuffled
per turn with a seeded draw (`shuffled`), so their order carries no bias; an item already brought
in during this conversation (`conversation`: a whisper's pair, a DM, a channel) in her last
LEDGER_TURNS turns there within LEDGER_TTL is not offered again; at most one Bring in per
BRING_IN_EVERY of her turns there (`offerable`); and `use` is null unless the item bears directly
on his last line (USE_DESCRIPTION, USE_ITEMS). The conversation's ledger holds ids only ("m<id>"
for a memory, "c<id>" for a chat line) and is stored by the pipeline in meta
`beat:used:<guid>:<conversation>`. When his line asks her something (asking.asks) the wants
that answer him lead (intent.ordered) and flirt reads as for when he is flirting (`asking`).

Pure: no I/O and no logging. The beat's words are never logged or stored (the pipeline logs the
`want` code, whether `use` was set and the time)."""
import json
import random
from dataclasses import dataclass

from leeroy_mind.talk.intent import TAIL_OFF, WANT_WORDS, Intent, ordered, want_words
from leeroy_mind.talk.prompt import ESTIMATE_SLACK
from leeroy_mind.text import estimate_tokens, truncate_words

# What she wants from her answer: the one want enum (talk/intent.py, the design's section-2
# intents) and the words "You want to:" reads for each, keyed here as the beat has always keyed
# them (by code, {human} for what the guild calls him). MEANING is each one's one-line meaning in
# the ask and the schema.
WANTS = {i.value: WANT_WORDS[i].replace("{who}", "{human}") for i in Intent}
MEANING = {
    "vent": "you need to get something that is bothering you off your chest",
    "share": "you want to tell {human} something of your own: news, a story, your day",
    "ask": "you want something from {human}: {their} time, a favour, attention, a yes "
           "(not just that you ask a question)",
    "tease": "you want to poke fun at {human}, playfully",
    "flirt": "you want to flirt with {human}",
    "reassure_seek": "you want to hear that things are good between you and {human}",
    "gossip": "you want to talk about someone else in the guild or in your life",
    "deflect": "you would rather steer away from what {human} said",
    "small_talk": "nothing much is at stake: you keep it light and easy",
    "react": "you just react to what {human} said: laugh, sympathise, be surprised",
    "boundary": "you want to say no, or draw a line",
    "logistics": "you want to sort out a plan: when, where, who, what to bring",
}
# flirt's meaning when his line asks her something (asking.asks): answering comes first
ASKED_MEANING = {"flirt": "you want to flirt with {human}, for when {human} is flirting with "
                          "you, not for answering a question"}
READ_CHARS = 200  # "You read it as": one sentence
LANDS_CHARS = 60  # "It lands": a few words
ITEM_CHARS = 120  # each memory or chat line offered, and "Bring in"
MAX_ITEMS = 6
TEMPLATE_TOKENS = 64  # the chat template's own tokens around the messages (a few dozen)

MARK = "Before you answer, take a moment for yourself."
READ_DESCRIPTION = "What {human} is actually saying or asking, in one short sentence."
LANDS_DESCRIPTION = "How it lands on you right now, in a few words, not a sentence."
WANT_DESCRIPTION = "What you want from your answer, the one that fits best."
USE_DESCRIPTION = ("The number of one offered memory or line, only if it bears directly on "
                   "{human}'s last line; otherwise null, as for most answers.")
USE_NONE_DESCRIPTION = "null: nothing is offered to bring in this time."
INSTRUCTION = (
    MARK + " Don't write your answer yet: reply with only a JSON object."
    '\n"read": ' + READ_DESCRIPTION +
    '\n"lands": ' + LANDS_DESCRIPTION +
    '\n"want": ' + WANT_DESCRIPTION + " One of:\n{wants}"
    '\n"use": {use}')
USE_ITEMS = ("the number of one of these, only if it bears directly on {human}'s last line, else "
             "null:\n{items}")
USE_NONE = "null."
# What a beat may bring in, per conversation (module docstring)
BRING_IN_EVERY = 3  # at most one Bring in per this many of her turns in a conversation
LEDGER_TURNS = 10  # her last turns a conversation's ledger keeps
LEDGER_TTL = 24 * 3600.0  # an entry older than this is forgotten
LEDGER_KEY = "beat:used:{guid}:{conversation}"


@dataclass(frozen=True)
class Beat:
    read: str
    lands: str
    want: str
    use: str | None  # the memory or chat line to bring in, as the prompt showed it


def wants_for(can_flirt: bool, *, tail: bool = False, asking: bool = False) -> tuple[str, ...]:
    """The wants on offer: "flirt" only for a member who may flirt with him (romance.flirt_chance;
    the content invariant: never a man, never a woman whose stance has no flirt). `tail`: his
    explicit whisper overlay is in the line's prompt, so the wants it rules out are not on offer
    (intent.TAIL_OFF: never vent, gossip, deflect, draw a line, sort out plans or keep it light;
    PM log 2026-11-16, review-332), whatever [talk] intent says. `asking`: his line asks her
    something (asking.asks), so the wants that answer him lead (intent.ordered)."""
    return tuple(i.value for i in ordered(
        (i for i in Intent if (can_flirt or i is not Intent.FLIRT) and not (tail and i in TAIL_OFF)),
        asking))


def keyed_items(memories: list[tuple[str, str]],
                chat: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """What `use` may point at, as (key, plain text) pairs (no age, speaker or bullet, so "Bring
    in" never reads as a chat line to copy): `memories` (the memories the prompt showed, keyed
    "m<id>"), then `chat` (the others' earlier lines in its chat window, oldest first, keyed
    "c<id>"; never hers, nor the line she answers) newest first, at most MAX_ITEMS, each cut to
    ITEM_CHARS."""
    out = [*memories, *reversed(chat)]
    return [(k, truncate_words(" ".join(x.split()), ITEM_CHARS)) for k, x in out
            if x.strip()][:MAX_ITEMS]


def items_of(memories: list[str], chat: list[str]) -> list[str]:
    """keyed_items' texts, for text read back without ids (the bake-off's captured requests)."""
    return [x for _, x in keyed_items([("", m) for m in memories], [("", c) for c in chat])]


def conversation(channel: str, to_guid: int | None) -> str:
    """The conversation a beat's turn belongs to, for its ledger: a whisper's pair (her side is
    the key's guid), else the channel ("dm:<guid>", "guild", "gdm:<id>", "voice:<room>")."""
    return f"whisper:{to_guid or 0}" if channel == "whisper" else channel


def ledger_key(guid: int, channel: str, to_guid: int | None) -> str:
    return LEDGER_KEY.format(guid=guid, conversation=conversation(channel, to_guid))


def ledger(raw: str | None, now: float) -> list[dict]:
    """A conversation's ledger as stored (JSON, oldest first: {"at": ts, "use": key or null} for
    each of her turns there), without entries older than LEDGER_TTL, the last LEDGER_TURNS. An
    unreadable one reads as empty."""
    try:
        rows = json.loads(raw) if raw else []
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    kept = [{"at": float(r["at"]), "use": r.get("use") if isinstance(r.get("use"), str) else None}
            for r in rows if isinstance(r, dict) and isinstance(r.get("at"), (int, float))
            and not isinstance(r.get("at"), bool) and 0 <= now - float(r["at"]) <= LEDGER_TTL]
    return kept[-LEDGER_TURNS:]


def offerable(items: list[tuple[str, str]], entries: list[dict]) -> list[tuple[str, str]]:
    """The items a beat may offer this turn: none while one of her last BRING_IN_EVERY - 1 turns
    in this conversation brought something in, else every item none of `entries` brought in."""
    if any(e["use"] for e in entries[-(BRING_IN_EVERY - 1):]):
        return []
    used = {e["use"] for e in entries if e["use"]}
    return [(k, x) for k, x in items if not k or k not in used]


def shuffled(items: list, rng: random.Random) -> list:
    """`items` in the turn's own order (a seeded draw: life.seed.seeded), so the first is no
    likelier to be brought in than the last."""
    out = list(items)
    rng.shuffle(out)
    return out


def recorded(entries: list[dict], now: float, use: str | None) -> str:
    """The ledger after one more turn of hers (`use`: the key she brought in, or None), as
    stored: JSON, ids and times only, the last LEDGER_TURNS."""
    return json.dumps([*entries, {"at": now, "use": use}][-LEDGER_TURNS:],
                      separators=(",", ":"))


def unframed(memory: str) -> str:
    """A memory line as the prompt shows it (memory.recall.format_memory: "- <when>: <text>",
    any scope tail kept) without its bullet and age."""
    return memory.split(": ", 1)[1] if memory.startswith("- ") and ": " in memory else memory


def _meanings(human: str, wants: tuple[str, ...], their: str, asking: bool = False) -> list[str]:
    meaning = {**MEANING, **ASKED_MEANING} if asking else MEANING
    return [f"{w}: {meaning[w].format(human=human, their=their)}" for w in wants]


def schema(wants: tuple[str, ...], human: str, their: str = "their", *, offered: bool = True,
           asking: bool = False) -> dict:
    """The beat's JSON schema for Ollama's `format`, each field described as the ask describes it.
    `their`: his possessive form (HumanIdentity.forms). `offered` False: nothing is offered to
    bring in, so `use` can only be null (a number there would cost the whole beat). `asking`:
    his line asks her something (ASKED_MEANING)."""
    use = ({"type": ["integer", "null"], "description": USE_DESCRIPTION.format(human=human)}
           if offered else {"type": "null", "description": USE_NONE_DESCRIPTION})
    return {"type": "object",
            "properties": {
                "read": {"type": "string", "description": READ_DESCRIPTION.format(human=human)},
                "lands": {"type": "string", "description": LANDS_DESCRIPTION},
                "want": {"type": "string", "enum": list(wants),
                         "description": " ".join([WANT_DESCRIPTION, *(
                             m + "." for m in _meanings(human, wants, their, asking))])},
                "use": use},
            "required": ["read", "lands", "want", "use"]}


def instruction(human: str, wants: tuple[str, ...], items: list[str], their: str = "their", *,
                asking: bool = False) -> str:
    """The beat call's own last words, after the turn's prompt; never in the line's prompt."""
    listed = "\n".join(f"- {m}" for m in _meanings(human, wants, their, asking))
    use = (USE_ITEMS.format(human=human,
                            items="\n".join(f"{i}. {x}" for i, x in enumerate(items, 1)))
           if items else USE_NONE)
    return INSTRUCTION.format(human=human, wants=listed, use=use)


def fitting(prompt_tokens: int, human: str, wants: tuple[str, ...], items: list[str], *,
            predict: int, num_ctx: int, their: str = "their",
            asking: bool = False) -> list[str] | None:
    """The offered items the beat's request can carry inside num_ctx, its prompt (the turn's,
    `prompt_tokens` estimated, plus the instruction) counted ESTIMATE_SLACK over the estimate:
    the last items go first (the oldest chat lines, then the least relevant memories). None when
    even no item fits (the beat is then skipped)."""
    items = list(items)
    while True:
        asked = prompt_tokens + 1 + estimate_tokens(instruction(human, wants, items, their,
                                                                asking=asking))
        if asked * ESTIMATE_SLACK + TEMPLATE_TOKENS + predict <= num_ctx:
            return items
        if not items:
            return None
        items.pop()


def _clean(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return truncate_words(" ".join(value.split()).strip(" \"'"), limit)


def parse(text: str, wants: tuple[str, ...], items: list[str]) -> tuple[Beat | None, str]:
    """The beat in the model's reply, or None and why it is skipped: "empty" (no reply, or no
    read or lands) or "schema" (not the object the schema asks for). `use` 0 is none: Orion
    writes 0 for nothing to bring in (2 of 30 live beats)."""
    if not text.strip():
        return None, "empty"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "schema"
    if not isinstance(data, dict) or not {"read", "lands", "want", "use"} <= data.keys():
        return None, "schema"
    read, lands, want, use = (_clean(data["read"], READ_CHARS), _clean(data["lands"], LANDS_CHARS),
                              data["want"], data["use"])
    if read is None or lands is None or want not in wants:
        return None, "schema"
    if type(use) is int and use == 0:
        use = None
    if use is not None and (type(use) is not int or not 1 <= use <= len(items)):
        return None, "schema"
    if not read or not lands:
        return None, "empty"
    return Beat(read, lands, want, items[use - 1] if use is not None else None), ""


def render(*, read: str = "", lands: str = "", want: Intent | None = None, who: str = "",
           topic: str = "", use: str | None = None) -> str:
    """The one block after a line's instruction, on any channel: a line for each field given,
    in this order: "You read it as", "It lands", "You want to" (intent.want_words: the want's
    words for `who`, then its `topic` in brackets) and "Bring in"; "" when none is given. The
    beat fills it on his private turns (lines); elsewhere the code-drawn want and the state's
    "It lands" will fill it (core plan Task 8)."""
    out = []
    if read:
        out.append(f"You read it as: {read}")
    if lands:
        out.append(f"It lands: {lands}")
    if want is not None:
        out.append(f"You want to: {want_words(want, who, topic)}")
    if use:
        out.append(f"Bring in: {use}")
    return "\n".join(out)


def lines(beat: Beat, human: str) -> str:
    """What the line's prompt gets after its instruction (render, from the beat)."""
    return render(read=beat.read, lands=beat.lands, want=Intent(beat.want), who=human,
                  use=beat.use)
