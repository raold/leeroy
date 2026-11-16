"""Asking a candidate the captured turns again, and typing its answers as the guild types.

Each turn goes to the candidate's model alone, one at a time, with one of the captured requests
(`--from`: the generation's first call by default, before any retry hint; or the call whose words
were sent) and its options: the same sampling and stop, a seed fixed per turn so every candidate
draws from the same seed and a resumed run repeats. The candidate's think flag is sent as given
(False for Orion as live; left out, the model's default). A thinking candidate (think not False)
gets no stop (the live stop would fire inside the thinking; the content is cut at its first stop
afterwards), num_predict raised by its think_budget and num_ctx raised to fit. Its messages pass
through the candidate's prompt variant. A thinking reply's thinking is counted, never kept.

Every reply, and the captured replies (the `first` and `live` baselines), then takes the steps of the live
pipeline that need no live state: what the model copied from the chat frame goes
(filters.strip_copied), the repairs (filters.repair), "(nothing)" is silence (filters.is_silence),
then her typing (style.style_lines) as the console's tuning preview types a line: sober, her
typing profile and, with [talk] texture on, her texture; a Discord message keeps its emoji and
its length band. One random generator per turn, the same for every candidate."""
import asyncio
import math
import random
import re
import time
import zlib

from leeroy_mind.discord.channels import is_discord
from leeroy_mind.discord.text import MAX_TEXT as DISCORD_MAX_TEXT
from leeroy_mind.llm.client import LLMError
from leeroy_mind.talk.discord_length import trim_to_sentence
from leeroy_mind.talk.filters import is_silence, repair, strip_copied
from leeroy_mind.talk.states import EFFECTS, Altered
from leeroy_mind.talk.style import style_lines
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import estimate_tokens

from . import variants

KEEP_ALIVE = "5m"   # loaded while its candidate runs; unloaded when the next model begins
BEAT_PREDICT = 240  # a beat's num_predict when the data dir's [talk] think_budget is unknown


def _kind(value: str) -> TurnKind | None:
    try:
        return TurnKind(value)
    except ValueError:
        return None


def turn_seed(seed: str, turn: int) -> int:
    return zlib.crc32(f"{seed}|{turn}".encode()) & 0x7FFFFFFF


class Typist:
    """Repairs and styles a reply as the member types (the module docstring)."""

    def __init__(self, personas, *, his: tuple[str, ...], protect: frozenset[str], texture: bool,
                 seed: str):
        self.personas = personas
        self.his = his
        self.protect = protect
        self.texture = texture
        self.seed = seed

    def type(self, text: str, turn) -> tuple[str, list[str]]:
        """("ok", lines) or ("silent", [])."""
        from leeroy_mind.talk.pipeline import LONG_SLACK, _emoji_only, aliases
        p = self.personas.get(turn.captured.guid)
        if p is None:
            raise LookupError("persona_missing")
        channel = turn.captured.channel
        discord = is_discord(channel)
        name, own = p.character_name, aliases(p)

        def quiet(t: str) -> bool:
            return is_silence(t) and not (discord and _emoji_only(t))
        if not text or quiet(text):
            return "silent", []
        t = strip_copied(text, (name, *own), him=self.his)
        if quiet(t):
            return "silent", []
        t = repair(t, speaker=name, aliases=own, him=self.his, keep_emoji=discord)
        if not t or quiet(t):
            return "silent", []
        shape = turn.captured.shape
        words = int(shape.get("words") or 0)
        max_lines = max(1, int(shape.get("max_lines") or 1))
        kind = _kind(turn.captured.turn_kind)
        most = 1 if words else max_lines
        pieces = ([x for x in t.split("\n") if x.strip()]
                  if kind is TurnKind.BIT and max_lines > 1 and not words else [t])
        rng = random.Random(turn_seed(self.seed, turn.id))
        out = []
        for piece in pieces:
            out += style_lines(piece, p.typing, EFFECTS[Altered.SOBER], rng, protect=self.protect,
                               speaker=name, kind=kind,
                               max_lines=most if len(pieces) == 1 else 1, aliases=own,
                               force_lines=bool(shape.get("two_lines")) and not words,
                               game=not discord,
                               cap=math.ceil(words * LONG_SLACK) if words else None,
                               max_chars=DISCORD_MAX_TEXT if discord else None,
                               texture=self.texture)
        out = out[:max(1, most)]
        lines = [s.line for s in out if s.line.strip()]
        if discord and lines:
            lines = ["\n".join(lines)]
        return ("ok", lines) if lines else ("silent", [])


def request(candidate, turn, seed: str, source: str = "first") -> tuple[list[dict], dict]:
    captured = turn.captured.request(source)
    messages = variants.apply(candidate.prompt_variant, captured.messages)
    options = dict(captured.options)
    if candidate.thinks:
        options.pop("stop", None)
        options["num_predict"] = int(options.get("num_predict", 80)) + candidate.think_budget
    ctx = candidate.context(int(options.get("num_ctx", 4096)))
    if ctx != options.get("num_ctx"):
        options["num_ctx"] = ctx
    options.setdefault("seed", turn_seed(seed, turn.id))
    return messages, options


def cut_at_stop(text: str, stops) -> str:
    """What the live stop would have kept: the content before its first stop string."""
    if isinstance(stops, str):
        stops = [stops]
    cuts = [text.find(s) for s in stops or () if isinstance(s, str) and s and s in text]
    return text[:min(cuts)].strip() if cuts else text


def trimmed(text: str, *, words, done_reason) -> str:
    """A Discord message cut off by num_predict back to its last full sentence, as _ask does."""
    return trim_to_sentence(text) if words and done_reason == "length" else text


_HTTP = re.compile(r"HTTP (\d{3})")


def error_code(e: BaseException) -> str:
    """A short reason for the report: http_400, timeout or the class's name; never its text."""
    if isinstance(e, asyncio.TimeoutError):
        return "timeout"
    text = str(e)
    if m := _HTTP.search(text):
        return f"http_{m[1]}"
    if "timed out" in text:
        return "timeout"
    return type(e).__name__


async def ask(llm, candidate, turn, seed: str, source: str = "first", who=None) -> dict:
    """One turn: {"status", "code", "text", "thinking_tokens", "eval_count", "seconds", "beat"}.
    A `beat` candidate asks its beat first (bakeoff/beat.py; `who(turn)` gives his name, his
    possessive, whether she may flirt with him and her character name); "beat" is then its want
    code or "skipped:<why>", never its words, and its time counts in "seconds"."""
    messages, options = request(candidate, turn, seed, source)
    started = time.monotonic()
    beat_code = None
    if candidate.beat:
        from . import beat
        info = who(turn) if who is not None else {}
        lines, beat_code = await beat.ask(
            llm, candidate.model, messages, human=info.get("human", ""),
            their=info.get("their", "their"), can_flirt=bool(info.get("can_flirt")),
            speaker=info.get("speaker", ""), timeout=candidate.timeout, keep_alive=KEEP_ALIVE,
            seed=turn_seed(f"{seed}|beat|{candidate.name}", turn.id),
            predict=info.get("think_budget", BEAT_PREDICT))
        if lines is None:
            beat_code = f"skipped:{beat_code}"
        else:
            messages = beat.with_lines(messages, lines)
    result = await _line(llm, candidate, turn, messages, options, source, started)
    result["beat"] = beat_code
    return result


async def _line(llm, candidate, turn, messages, options, source, started) -> dict:
    try:
        r = await llm.chat(candidate.model, messages, options=options, think=candidate.think,
                           keep_alive=KEEP_ALIVE, timeout=candidate.timeout)
    except (LLMError, asyncio.TimeoutError) as e:
        return {"status": "error", "code": error_code(e), "text": None, "thinking_tokens": 0,
                "eval_count": 0, "seconds": round(time.monotonic() - started, 3)}
    text = r.text
    if candidate.thinks:
        text = cut_at_stop(text, turn.captured.request(source).options.get("stop"))
    text = trimmed(text, words=turn.captured.shape.get("words"),
                   done_reason=getattr(r.diagnostics, "done_reason", None))
    return {"status": "ok", "code": None, "text": text,
            "thinking_tokens": estimate_tokens(r.thinking) if r.thinking else 0,
            "eval_count": r.eval_count, "seconds": round(time.monotonic() - started, 3)}
