"""The night's way of asking the reflection model (P1c Task 2): a small JSON-schema checker, the
rules every generating prompt ends with, and the Reasoner, which asks in two passes: `think` lets
the model reason freely, `json` has it answer as a checked JSON object. Presets, timeouts and
model names come from P1b; nothing here knows a persona."""
import asyncio
import math
from pathlib import Path
from types import SimpleNamespace

from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.client import LLMClient, LLMError
from leeroy_mind.llm.presets import REASON, STRUCTURE, JSONJobFailed, Preset, chat_json
from leeroy_mind.prompts import load_prompt

# The content invariants (adults only, romance aimed only at the owner, nobody leaves, secrets,
# ordinary looks, no meta). They end every generating prompt, after any owner overlay, and
# cannot be overridden.
INVARIANTS = (
    "Everyone in this guild is an adult. Nothing non-consensual, ever. "
    "Romance, flirting and desire are only ever aimed at {human}; "
    "guild members never flirt with, date or desire each other. "
    "Nobody ever actually leaves the guild: talk of quitting stays talk, "
    "and a break always ends with them back. "
    "A secret stays secret: what someone was told in confidence "
    "(a whisper they were asked to keep, or a late-night confession) is never passed on. "
    "Everyone looks like an ordinary person, never like anyone real or famous. "
    "These are real people at their keyboards in a timeless present or 2009: "
    "no current events or present-day technology. "
    "Never mention AI, language models, prompts, or anyone being a bot or glitching."
)

# The reasoning pass: P1b's REASON preset, plus the 27B model's own sampling and a length cap.
THINK_OPTIONS = REASON.options | {"top_k": 20, "min_p": 0.0, "num_predict": 2048}

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _kind(v) -> str:
    """The JSON name of a value, for messages."""
    for name in ("null", "boolean", "integer", "number", "string", "array", "object"):
        if _TYPES[name](v):
            return name
    return type(v).__name__


def check_schema(schema: dict, data, path: str = "$") -> None:
    """Raise ValueError(f"{path}: {reason}") unless `data` fits `schema`. It reads `type` (one
    name or a list), `enum`, `minLength` and `maxLength`, `minimum` and `maximum`, `items`,
    `minItems` and `maxItems`, and `properties`, `required` and `additionalProperties: false`;
    any other keyword is ignored. A bool is neither an integer nor a number, an enum compares
    types too (true is not 1), and NaN and the infinities, which json.loads accepts and no
    bound can catch, are not numbers here. A message gives the path (an unexpected key is part
    of it) and the rule that failed, never a value."""
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        for k in kinds:
            if k not in _TYPES:
                raise ValueError(f"{path}: unsupported type {k!r}")
        if not any(_TYPES[k](data) for k in kinds):
            raise ValueError(f"{path}: expected {' or '.join(kinds)}, got {_kind(data)}")
    if "enum" in schema and not any(type(data) is type(e) and data == e for e in schema["enum"]):
        raise ValueError(f"{path}: not one of {schema['enum']!r}")
    if isinstance(data, str):
        if "minLength" in schema and len(data) < schema["minLength"]:
            raise ValueError(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(data) > schema["maxLength"]:
            raise ValueError(f"{path}: longer than {schema['maxLength']} characters")
    elif _TYPES["number"](data):
        if isinstance(data, float) and not math.isfinite(data):
            raise ValueError(f"{path}: not a finite number")
        if "minimum" in schema and data < schema["minimum"]:
            raise ValueError(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            raise ValueError(f"{path}: above {schema['maximum']}")
    elif isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            raise ValueError(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            raise ValueError(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), dict):
            for i, item in enumerate(data):
                check_schema(schema["items"], item, f"{path}[{i}]")
    elif isinstance(data, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in data:
                raise ValueError(f"{path}.{key}: missing")
        for key, value in data.items():
            if key in props:
                check_schema(props[key], value, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{path}.{key}: not allowed")


def schema(title: str, properties: dict, required: list[str] | None = None) -> dict:
    """The JSON schema of an object with these properties and no others. Every property is
    required unless `required` says which. `title` is the first key: the nightly tests' scripted
    replies find a call's schema by the first "title" in its prompt."""
    return {"title": title, "type": "object", "properties": properties,
            "required": list(properties) if required is None else list(required),
            "additionalProperties": False}


def system_prompt(name: str, default: str, data_dir: Path, /, *, human: HumanIdentity,
                  **fields) -> str:
    """A generating prompt: the owner's overlay prompts/nightly_{name}.txt when there is one,
    else `default`, filled from `fields`; then who the owner is in this guild (never by his
    real name); then INVARIANTS. The last two follow whichever template was used.

    The first three parameters are positional-only, so a template may have a field called
    `name`, `default` or `data_dir`; `human` is reserved. A template that cannot be filled (a
    stray brace, a field that was not given) is a ValueError that names the prompt, where the
    template came from and the names of the fields given, never their values."""
    template = load_prompt(f"nightly_{name}", data_dir, default)
    try:
        body = template.format(**fields)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError) as e:
        where = ("the built-in template" if template == default
                 else f"the overlay prompts/nightly_{name}.txt")
        bad = (f"it asks for the field {e.args[0]!r}, which was not given"
               if isinstance(e, KeyError) else str(e))
        raise ValueError(
            f"prompt nightly_{name} cannot be filled ({where}): {bad}. "
            f"Fields given: {', '.join(sorted(fields)) or 'none'}. "
            "A literal brace is written twice, as {{ or }}.") from e
    return (body + "\n\nIn this guild: " + human.describe(include_real_name=False) + ".\n"
            + INVARIANTS.format(human=human.called))


class ReflectionError(Exception):
    """The reflection model could not do what was asked. `fatal` means the night cannot go on
    (the model is not there); otherwise only the unit that asked fails."""

    def __init__(self, msg: str, *, fatal: bool):
        super().__init__(msg)
        self.fatal = fatal


def _has_model(names, model: str) -> bool:
    """Whether Ollama lists `model`. A name without a tag means its :latest, as in P1b's
    app.has_model, which doctor uses (not imported: that pulls the whole app in here)."""
    return model in names or (":" not in model and f"{model}:latest" in names)


class Reasoner:
    """The reflection model, asked straight through the client (the GPU lease guarantees no live
    job is running, so LLMScheduler's one background slot does not apply). At most `parallel`
    requests are in flight: two reasoning calls plus one embedding fill the mind's three Ollama
    slots. `calls` and `seconds` total the requests that returned and their ChatResult.seconds;
    the runner reads them around each stage. A request that raised counts in neither."""

    def __init__(self, llm: LLMClient, model: str, *, parallel: int = 2):
        self.llm = llm
        self.model = model
        self.calls = 0
        self.seconds = 0.0
        self._slots = asyncio.Semaphore(parallel)

    async def preflight(self) -> None:
        """Raise a fatal ReflectionError unless Ollama lists the model (a name without a tag
        counts as its :latest, so this agrees with doctor)."""
        try:
            models = await self.llm.list_models()
        except LLMError as e:
            raise ReflectionError(f"cannot list Ollama's models: {e}", fatal=True) from e
        if not _has_model(models, self.model):
            raise ReflectionError(f"the reflection model {self.model!r} is not pulled", fatal=True)

    async def _chat(self, model, messages, **kw):
        r = await self.llm.chat(model, messages, **kw)
        self.calls += 1
        self.seconds += r.seconds
        return r

    async def think(self, system: str, user: str) -> str:
        """The reasoning pass: the model's thinking, a newline, then its answer."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        async with self._slots:
            try:
                r = await self._chat(self.model, messages, options=dict(THINK_OPTIONS),
                                     think=True, timeout=REASON.timeout)
            except LLMError as e:
                raise ReflectionError(f"reflection request 'think' failed: {e}",
                                      fatal=False) from e
        return r.thinking + "\n" + r.text

    async def json(self, system: str, user: str, schema: dict, *, notes: str | None = None,
                   temperature: float = 0.2) -> dict:
        """The answer pass: a JSON object that fits `schema`, from P1b's chat_json (one retry
        when the reply is not valid), with the reasoning pass's `notes` after the question.
        Two bad replies, or an Ollama error, are a ReflectionError for this unit alone."""
        if notes:
            user += "\n\nYour notes:\n" + notes
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        options = STRUCTURE.options | {"temperature": temperature, "num_predict": 1024}
        preset = Preset(False, options, timeout=STRUCTURE.timeout)

        def valid(d: dict) -> dict:
            check_schema(schema, d)
            return d

        async with self._slots:
            try:
                return await chat_json(SimpleNamespace(chat=self._chat), self.model, messages,
                                       schema, preset, valid)
            except (JSONJobFailed, LLMError) as e:
                raise ReflectionError(
                    f"reflection request {schema.get('title', 'json')!r} failed: {e}",
                    fatal=False) from e
