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

Core plan Task 8 widens it (`[talk] intent`, `[life] state`): the pipeline may give the ask the
wants the turn allows (intent.allowed) and the want code drew as a `lean`, and leave `lands` out
(`ask_lands` False) when her state says how his line lands; `render` is then the one block after
the instruction on every eligible turn, the beat's or the code's. With neither, every function
here reads exactly as before.

Pure: no I/O and no logging. The beat's words are never logged or stored (the pipeline logs the
`want` code, whether `use` was set and the time)."""
import json
from dataclasses import dataclass

from leeroy_mind.talk.intent import TAIL_OFF, WANT_WORDS, Intent, want_words
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
READ_CHARS = 200  # "You read it as": one sentence
LANDS_CHARS = 60  # "It lands": a few words
ITEM_CHARS = 120  # each memory or chat line offered, and "Bring in"
MAX_ITEMS = 6
TEMPLATE_TOKENS = 64  # the chat template's own tokens around the messages (a few dozen)
# the longest subject a want carries (intent.topic_for; "raid times, gear or who's coming" is 32)
TOPIC_CHARS = 40

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
LANDS_ASK = '\n"lands": ' + LANDS_DESCRIPTION  # what ask_lands False takes out of the ask
# The want the code drew, named as the lean (`lean`): its own sentence after the want clause
# (prompt text carries no semicolon: filters.py repairs one as a tell)
LEAN = " You lean toward {lean} ({words})."
USE_ITEMS = "the number of one of these worth bringing in, or null:\n{items}"
USE_NONE = "null."


@dataclass(frozen=True)
class Beat:
    read: str
    lands: str
    want: str
    use: str | None  # the memory or chat line to bring in, as the prompt showed it


def wants_for(can_flirt: bool, *, tail: bool = False) -> tuple[str, ...]:
    """The wants on offer: "flirt" only for a member who may flirt with him (romance.flirt_chance;
    the content invariant: never a man, never a woman whose stance has no flirt). `tail`: his
    explicit whisper overlay is in the line's prompt, so the wants it rules out are not on offer
    (intent.TAIL_OFF: never vent, gossip, deflect, draw a line, sort out plans or keep it light;
    PM log 2026-11-16, review-332), whatever [talk] intent says."""
    return tuple(i.value for i in Intent if (can_flirt or i is not Intent.FLIRT)
                 and not (tail and i in TAIL_OFF))


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


def schema(wants: tuple[str, ...], human: str, their: str = "their", *,
           ask_lands: bool = True) -> dict:
    """The beat's JSON schema for Ollama's `format`, each field described as the ask describes it.
    `their`: his possessive form (HumanIdentity.forms). Without `ask_lands` there is no `lands`
    (her state says how his line lands: core plan Task 8)."""
    properties = {
        "read": {"type": "string", "description": READ_DESCRIPTION.format(human=human)},
        "lands": {"type": "string", "description": LANDS_DESCRIPTION},
        "want": {"type": "string", "enum": list(wants),
                 "description": " ".join([WANT_DESCRIPTION, *(
                     m + "." for m in _meanings(human, wants, their))])},
        "use": {"type": ["integer", "null"], "description": USE_DESCRIPTION}}
    if not ask_lands:
        del properties["lands"]
    return {"type": "object", "properties": properties, "required": list(properties)}


def instruction(human: str, wants: tuple[str, ...], items: list[str], their: str = "their", *,
                lean: Intent | None = None, ask_lands: bool = True) -> str:
    """The beat call's own last words, after the turn's prompt; never in the line's prompt.
    `lean`: the want the code drew for the turn, named after the want clause (LEAN), so the
    model leans toward it but still chooses; `ask_lands` False leaves "lands" out."""
    listed = "\n".join(f"- {m}" for m in _meanings(human, wants, their))
    use = (USE_ITEMS.format(items="\n".join(f"{i}. {x}" for i, x in enumerate(items, 1)))
           if items else USE_NONE)
    from leeroy_mind.talk.pieces import text as piece_text  # the lab's variant of the ask
    ask = piece_text("beat_ask", MARK)
    template = INSTRUCTION if ask == MARK else INSTRUCTION.replace(MARK, ask, 1)
    text = template.format(human=human, wants=listed, use=use)
    if lean is not None:
        lean = Intent(lean)
        text = text.replace(WANT_DESCRIPTION, WANT_DESCRIPTION + LEAN.format(
            lean=lean.value, words=want_words(lean, human)), 1)
    if not ask_lands:
        text = text.replace(LANDS_ASK, "", 1)
    return text


def fitting(prompt_tokens: int, human: str, wants: tuple[str, ...], items: list[str], *,
            predict: int, num_ctx: int, their: str = "their", lean: Intent | None = None,
            ask_lands: bool = True) -> list[str] | None:
    """The offered items the beat's request can carry inside num_ctx, its prompt (the turn's,
    `prompt_tokens` estimated, plus the instruction) counted ESTIMATE_SLACK over the estimate:
    the last items go first (the oldest chat lines, then the least relevant memories). None when
    even no item fits (the beat is then skipped)."""
    items = list(items)
    while True:
        asked = prompt_tokens + 1 + estimate_tokens(instruction(
            human, wants, items, their, lean=lean, ask_lands=ask_lands))
        if asked * ESTIMATE_SLACK + TEMPLATE_TOKENS + predict <= num_ctx:
            return items
        if not items:
            return None
        items.pop()


def _clean(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return truncate_words(" ".join(value.split()).strip(" \"'"), limit)


def parse(text: str, wants: tuple[str, ...], items: list[str], *,
          ask_lands: bool = True) -> tuple[Beat | None, str]:
    """The beat in the model's reply, or None and why it is skipped: "empty" (no reply, or no
    read or lands) or "schema" (not the object the schema asks for). `use` 0 is none: Orion
    writes 0 for nothing to bring in (2 of 30 live beats). Without `ask_lands` a reply needs no
    `lands` (one it gives anyway is not used): the Beat's lands is ""."""
    if not text.strip():
        return None, "empty"
    try:
        data = json.loads(text)
    except ValueError:
        return None, "schema"
    needed = {"read", "lands", "want", "use"} if ask_lands else {"read", "want", "use"}
    if not isinstance(data, dict) or not needed <= data.keys():
        return None, "schema"
    read, want, use = _clean(data["read"], READ_CHARS), data["want"], data["use"]
    lands = _clean(data["lands"], LANDS_CHARS) if ask_lands else ""
    if read is None or lands is None or want not in wants:
        return None, "schema"
    if type(use) is int and use == 0:
        use = None
    if use is not None and (type(use) is not int or not 1 <= use <= len(items)):
        return None, "schema"
    if not read or (ask_lands and not lands):
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


def block_room(who: str, *, lands: str | None = None) -> int:
    """The most estimated tokens the beat's block can add to the line's prompt (the newline
    before it included): every field at its cap, the longest want with the longest topic, and
    `lands` as given (the state's: core plan Task 8) or at its cap. The pipeline counts it in the
    prompt's budget before the beat is asked, so the block never takes the prompt past it."""
    longest = max((want_words(i, who, "t" * TOPIC_CHARS) for i in Intent), key=len)
    block = "\n".join([f"You read it as: {'r' * READ_CHARS}",
                       f"It lands: {'l' * LANDS_CHARS if lands is None else lands}",
                       f"You want to: {longest}", f"Bring in: {'u' * ITEM_CHARS}"])
    return estimate_tokens("\n" + block)
