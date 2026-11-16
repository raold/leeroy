import json
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Callable, Literal, TypeVar

from leeroy_mind.llm.client import LLMClient

T = TypeVar("T")


class JSONJobFailed(Exception):
    pass


@dataclass(frozen=True)
class Preset:
    think: bool | None  # sent as `think`; None omits the key (the model's own default)
    options: dict
    timeout: float | None = None  # seconds; None means the client default

    def __post_init__(self):
        frozen = {k: tuple(v) if isinstance(v, list) else v for k, v in self.options.items()}
        object.__setattr__(self, "options", MappingProxyType(frozen))


# Every request to one model uses the same num_ctx (4096 live, 8192 reflection):
# a different value makes Ollama reload the model. Never add repeat penalties.
# LIVE stops at the first blank line: what a model writes after one is narration, another
# speaker's line or a second answer, which the tells would retry or drop anyway.
LIVE = Preset(False, {"temperature": 0.9, "min_p": 0.05, "top_p": 1.0,
                      "num_predict": 80, "num_ctx": 4096, "stop": ["\n\n"]})
# A bit of several rows (talk.bits): LIVE without the blank line stop, so a model that puts a
# blank line between the rows still gives them all (prompts lens, minor 9). num_predict and
# the turn's max_lines still bound it.
LIVE_BIT = Preset(LIVE.think, {k: v for k, v in LIVE.options.items() if k != "stop"},
                  timeout=LIVE.timeout)
# Thinking before an answer to him ([talk] think_private; talk/pipeline.py _think_budget).
# Ollama has no budget of its own for thinking: it counts against num_predict, so a thinking
# request asks for the line's own allowance plus the budget (thinking). num_ctx never changes
# (another value reloads the model): the prompt makes the room instead (prompt.room_for).
# No stop: Ollama applies stop sequences to the raw text, the thinking included, and LIVE's
# blank-line stop fired inside Orion's thinking (measured 2026-11-16: empty content,
# done_reason stop), so the caller cuts the answer at the stop itself (cut_at_stop).
# `think` is tri-state per model: None omits the key, the model's own default, which is how
# Orion thinks (it refuses `think: true` with HTTP 400, measured 2026-11-16); True asks a model
# that thinks only when asked; False never thinks, and is what a retry without thinking sends.
THINK_BUDGET = 1400  # Orion thought 600-1000 tokens on real DM/whisper prompts (2026-11-16)
THINK_FLAG = None  # how the live model is asked to think: by omitting `think`


def thinking(preset: Preset, budget: int = THINK_BUDGET, *, flag: bool | None = THINK_FLAG) -> Preset:
    """`preset` for a request that thinks first: `flag` as its `think`, `budget` tokens more
    num_predict than the line's own allowance, and no stop."""
    if flag is False or budget <= 0:
        raise ValueError("a thinking request needs a thinking flag and a budget")
    options = {k: v for k, v in preset.options.items() if k != "stop"}
    options["num_predict"] = options.get("num_predict", 80) + budget
    return replace(preset, think=flag, options=options)


def cut_at_stop(text: str, stops) -> str:
    """`text` up to the first of `stops`, as the server's stop would have ended it."""
    ends = [i for i in (text.find(s) for s in stops if s) if i >= 0]
    return text[:min(ends)].rstrip() if ends else text


LIVE_THINK = thinking(LIVE)
EXTRACT = Preset(False, {"temperature": 0.2, "num_ctx": 4096})
LOOK = Preset(False, {"num_predict": 140, "num_ctx": 4096, "temperature": 0.2})
# The 27B reflection model with thinking on easily outlasts the 60 s client default.
REASON = Preset(True, {"temperature": 1.0, "top_p": 0.95, "num_ctx": 8192}, timeout=600.0)
STRUCTURE = Preset(False, {"temperature": 0.2, "num_ctx": 8192}, timeout=600.0)


def _with_suffix(messages: list[dict], suffix: str) -> list[dict]:
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m.get("role") == "user":
            m["content"] = m["content"] + suffix
            return out
    out.append({"role": "user", "content": suffix.strip()})
    return out


async def chat_json(llm: LLMClient, model: str, messages: list[dict], schema: dict,
                    preset: Preset, validate: Callable[[dict], T]) -> T:
    base = _with_suffix(
        messages, f"\n\nReply with only a JSON object matching this schema:\n{json.dumps(schema)}")
    msgs = base
    last = ""
    for _ in range(2):
        # LLMError is not caught here: the caller marks the job failed.
        r = await llm.chat(model, msgs, options=dict(preset.options),
                           think=preset.think, format=schema, timeout=preset.timeout)
        try:
            data = json.loads(r.text)
            if not isinstance(data, dict):
                raise ValueError("the reply is not a JSON object")
            return validate(data)
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            msgs = _with_suffix(
                base, f"\n\nYour previous reply was invalid (error: {last}). "
                      "Reply again with only the corrected JSON object.")
    raise JSONJobFailed(last)


def model_for(job: Literal["live", "extract", "embed", "persona"], cfg) -> str:
    if job in ("live", "extract"):
        return cfg.models.live
    if job == "embed":
        return cfg.models.embedding
    if job == "persona":
        return cfg.models.reflection
    raise ValueError(f"unknown job {job!r}")
