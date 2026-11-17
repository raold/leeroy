"""The `beat` candidate setting (`beat = true`): the candidate asks its own beat first, through
talk/beat.py as the live pipeline does ([talk] think_private): one call on the turn's own request
with the beat's instruction after it, constrained to beat.schema with think false (BEAT's
sampling and num_ctx, num_predict the data dir's [talk] think_budget, a seed per turn and
candidate), then the line is asked with the beat's lines after the
instruction. A beat that fails the schema, does not fit or errs is skipped and the line goes
without it, as live.

The captured requests never hold the live beat (bakeoff/capture.py takes its lines back out), so
every candidate starts from the request without one. What a beat may bring in is read back from
that request, since the composed prompt's items are not stored: the memories under "You
remember:" and the earlier chat lines under "Chat:" that are not hers (the last one, the line she
answers, never), plain, as beat.items_of takes them. The beat's words are used only for its
line's request: never stored, never logged; the result keeps its want code or why it was
skipped."""
import asyncio
import random
import re

from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.presets import BEAT
from leeroy_mind.talk import beat as beats
from leeroy_mind.text import estimate_tokens

_CHAT_ROW = re.compile(r"^(?:\[[^\]]*\] )?(.+?)(?: \([^()]*\))?: (.*)$")


def with_lines(messages: list[dict], text: str) -> list[dict]:
    """`text` after the last message, as the pipeline's _with_hint puts the beat's lines."""
    out = [dict(m) for m in messages]
    out[-1]["content"] = f"{out[-1]['content']}\n{text}"
    return out


def _section(user: str, head: str) -> list[str]:
    for part in user.split("\n\n"):
        if part.startswith(head + "\n"):
            return [x for x in part.split("\n")[1:] if x.strip()]
    return []


def items(messages: list[dict], speaker: str) -> list[str]:
    """What the beat may offer to bring in, read back from the request (module docstring)."""
    user = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
    memories = [beats.unframed(x) for x in _section(user, "You remember:")]
    chat = []
    for row in _section(user, "Chat:")[:-1]:
        m = _CHAT_ROW.match(row)
        if m is not None and m[1].strip().casefold() != speaker.casefold():
            chat.append(m[2])
    return beats.items_of(memories, chat)


async def ask(llm, model: str, messages: list[dict], *, human: str, their: str, can_flirt: bool,
              speaker: str, timeout: float, keep_alive: str, seed: int,
              predict: int = BEAT.options["num_predict"]) -> tuple[str | None, str]:
    """(the beat's lines, its want code) or (None, why it was skipped). `seed`: per turn and
    candidate, so a re-run draws the same beat; `predict`: the live [talk] think_budget."""
    wants = beats.wants_for(can_flirt)
    options = dict(BEAT.options) | {"num_predict": int(predict), "seed": int(seed)}
    offered = beats.fitting(sum(estimate_tokens(m.get("content") or "") for m in messages), human,
                            wants, items(messages, speaker), predict=options["num_predict"],
                            num_ctx=options["num_ctx"], their=their)
    if offered is None:
        return None, "room"
    # in the turn's own order, as live (beat.shuffled), drawn from the candidate's seed
    offered = beats.shuffled(offered, random.Random(f"beat_items:{seed}"))
    asked = with_lines(messages, beats.instruction(human, wants, offered, their))
    try:
        r = await llm.chat(model, asked, options=options, think=False,
                           format=beats.schema(wants, human, their, offered=bool(offered)),
                           keep_alive=keep_alive, timeout=timeout)
    except (LLMError, asyncio.TimeoutError) as e:
        from .generate import error_code
        return None, error_code(e)
    found, reason = beats.parse(r.text, wants, offered)
    if found is None:
        return None, reason
    return beats.lines(found, human), found.want
