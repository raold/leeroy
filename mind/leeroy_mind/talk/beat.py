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

Pure: no I/O and no logging. The beat's words are never logged or stored (the pipeline logs the
`want` code, whether `use` was set and the time)."""
import json
from dataclasses import dataclass

from leeroy_mind.talk.prompt import ESTIMATE_SLACK
from leeroy_mind.text import estimate_tokens, truncate_words

# What she wants from her answer (the design's section-2 intents): the words "You want to:" reads
# for each, and its one-line meaning in the ask and the schema. {human} is what the guild calls him.
WANTS = {
    "vent": "get something off your chest",
    "share": "share something of your own",
    "ask": "get something from {human}",
    "tease": "tease {human}",
    "flirt": "flirt with {human}",
    "reassure_seek": "hear that things are good between you",
    "gossip": "talk about someone else",
    "deflect": "steer away from it",
    "small_talk": "keep it light",
    "react": "just react",
    "boundary": "draw a line",
    "logistics": "sort out the plan",
}
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
READ_CHARS = 200  # "You read it as": one sentence
LANDS_CHARS = 60  # "It lands": a few words
ITEM_CHARS = 120  # each memory or chat line offered, and "Bring in"
MAX_ITEMS = 6
TEMPLATE_TOKENS = 64  # the chat template's own tokens around the messages (a few dozen)

MARK = "Before you answer, take a moment for yourself."
READ_DESCRIPTION = "What {human} is actually saying or asking, in one short sentence."
LANDS_DESCRIPTION = "How it lands on you right now, in a few words, not a sentence."
WANT_DESCRIPTION = "What you want from your answer, the one that fits best."
USE_DESCRIPTION = "The number of one offered memory or line worth bringing in, or null."
INSTRUCTION = (
    MARK + " Don't write your answer yet: reply with only a JSON object."
    '\n"read": ' + READ_DESCRIPTION +
    '\n"lands": ' + LANDS_DESCRIPTION +
    '\n"want": ' + WANT_DESCRIPTION + " One of:\n{wants}"
    '\n"use": {use}')
USE_ITEMS = "the number of one of these worth bringing in, or null:\n{items}"
USE_NONE = "null."


@dataclass(frozen=True)
class Beat:
    read: str
    lands: str
    want: str
    use: str | None  # the memory or chat line to bring in, as the prompt showed it


def wants_for(can_flirt: bool) -> tuple[str, ...]:
    """The wants on offer: "flirt" only for a member who may flirt with him (romance.flirt_chance;
    the content invariant: never a man, never a woman whose stance has no flirt)."""
    return tuple(w for w in WANTS if can_flirt or w != "flirt")


def items_of(memories: list[str], chat: list[str]) -> list[str]:
    """What `use` may point at, as plain text (no age, speaker or bullet, so "Bring in" never
    reads as a chat line to copy): `memories` (the memories the prompt showed), then `chat` (the
    others' earlier lines in its chat window, oldest first; never hers, nor the line she answers)
    newest first, at most MAX_ITEMS, each cut to ITEM_CHARS."""
    out = [*memories, *reversed(chat)]
    return [truncate_words(" ".join(x.split()), ITEM_CHARS) for x in out if x.strip()][:MAX_ITEMS]


def unframed(memory: str) -> str:
    """A memory line as the prompt shows it (memory.recall.format_memory: "- <when>: <text>",
    any scope tail kept) without its bullet and age."""
    return memory.split(": ", 1)[1] if memory.startswith("- ") and ": " in memory else memory


def _meanings(human: str, wants: tuple[str, ...], their: str) -> list[str]:
    return [f"{w}: {MEANING[w].format(human=human, their=their)}" for w in wants]


def schema(wants: tuple[str, ...], human: str, their: str = "their") -> dict:
    """The beat's JSON schema for Ollama's `format`, each field described as the ask describes it.
    `their`: his possessive form (HumanIdentity.forms)."""
    return {"type": "object",
            "properties": {
                "read": {"type": "string", "description": READ_DESCRIPTION.format(human=human)},
                "lands": {"type": "string", "description": LANDS_DESCRIPTION},
                "want": {"type": "string", "enum": list(wants),
                         "description": " ".join([WANT_DESCRIPTION, *(
                             m + "." for m in _meanings(human, wants, their))])},
                "use": {"type": ["integer", "null"], "description": USE_DESCRIPTION}},
            "required": ["read", "lands", "want", "use"]}


def instruction(human: str, wants: tuple[str, ...], items: list[str], their: str = "their") -> str:
    """The beat call's own last words, after the turn's prompt; never in the line's prompt."""
    listed = "\n".join(f"- {m}" for m in _meanings(human, wants, their))
    use = (USE_ITEMS.format(items="\n".join(f"{i}. {x}" for i, x in enumerate(items, 1)))
           if items else USE_NONE)
    return INSTRUCTION.format(human=human, wants=listed, use=use)


def fitting(prompt_tokens: int, human: str, wants: tuple[str, ...], items: list[str], *,
            predict: int, num_ctx: int, their: str = "their") -> list[str] | None:
    """The offered items the beat's request can carry inside num_ctx, its prompt (the turn's,
    `prompt_tokens` estimated, plus the instruction) counted ESTIMATE_SLACK over the estimate:
    the last items go first (the oldest chat lines, then the least relevant memories). None when
    even no item fits (the beat is then skipped)."""
    items = list(items)
    while True:
        asked = prompt_tokens + 1 + estimate_tokens(instruction(human, wants, items, their))
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


def lines(beat: Beat, human: str) -> str:
    """What the line's prompt gets after its instruction."""
    out = [f"You read it as: {beat.read}", f"It lands: {beat.lands}",
           f"You want to: {WANTS[beat.want].format(human=human)}"]
    if beat.use is not None:
        out.append(f"Bring in: {beat.use}")
    return "\n".join(out)
