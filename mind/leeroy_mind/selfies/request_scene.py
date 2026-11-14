"""One live enum choice for an owner request, before borrowing the image GPU."""
from dataclasses import replace

from leeroy_mind.llm.client import GpuPaused, LLMError
from leeroy_mind.llm.presets import JSONJobFailed, LIVE_BIT, Preset, chat_json
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.nightly.enrich import _facts
from leeroy_mind.nightly.llm import check_schema
from leeroy_mind.personas.card import render_prompt_card
from leeroy_mind.personas.io import load_persona
from leeroy_mind.wow import CLASSES, RACES

from . import files
from .kind import looks_line
from .prompt import renderable
from .ration import SEND_MIN_AGE
from .scene import Scene, scene_from, scene_renderable, scene_schema, scene_system, screen_scene
from .scene_choices import compose_choices, pairing_codes
from .store import current
from .style import LUST, style_of

SCENE_LIVE = Preset(False, dict(LIVE_BIT.options) | {"num_predict": 512}, timeout=30.0)


class RequestSceneError(ValueError):
    """A code only; no request words or model reply escape into diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fallback(p, audience: str) -> dict:
    picks = dict(photo=True, kind="selfie", framing="upper_body", setting="living_room",
                 activity="relaxing", outfit="casual", expression="smile", prop="none",
                 when="any", audience=audience)
    if style_of(p) == LUST:
        picks.update(setting="her living room couch", outfit="satin midi dress",
                     colour="navy", expression="a playful smile")
    return picks


async def choose_scene(app, guid: int, words: str, audience: str, *, model: bool = True) -> Scene:
    """Choose at most twice, then use a screened everyday choice. Never store his words.
    `model=False` takes that everyday choice at once, without the live model.

    The scheduler's job awaits admission itself: a borrow can begin while this job queues.
    chat_json owns the single retry, including schema, pairing and content failures.
    """
    if audience not in ("him", "anyone"):
        raise RequestSceneError("audience")
    try:
        p = load_persona(app.store, guid)
    except ValueError:
        raise RequestSceneError("unrenderable") from None
    if p is None or p.identity.age < SEND_MIN_AGE or renderable(p) is not None:
        raise RequestSceneError("unrenderable")
    pick = current(app.store, guid)
    if pick is None or not files.history(app.data_dir, guid, pick.pick).is_file():
        raise RequestSceneError("no_profile")
    # A lust member always sends privately, including a request made in a channel.
    audience = "him" if style_of(p) == LUST else audience
    ident, roster = app.engine.human, app.store.cast()
    member = roster.get(guid)
    if member is None:
        raise RequestSceneError("no_member")
    names = [ident.nickname, ident.character_name]
    for other in roster.values():
        if other.guid != guid:
            names.append(other.name)
            if (persona := app.personas.base(other.guid)) is not None:
                names.append(persona.identity.real_name)
    schema = scene_schema(p)

    def validate(data):
        try:
            check_schema(schema, data)
        except ValueError:
            # Unexpected JSON keys can contain his words; never echo those in a retry.
            raise ValueError("scene.schema") from None
        if not data["photo"]:
            raise ValueError("scene.declined")
        if codes := pairing_codes(data):
            raise ValueError("; ".join(codes))
        composed = compose_choices(data)
        if codes := screen_scene(p, composed, ident=ident, member_names=names):
            raise ValueError("; ".join(codes))
        scene = replace(scene_from(composed), audience=audience, when="any")
        if scene_renderable(p, scene):
            raise ValueError("scene.unrenderable")
        return scene

    system = scene_system(p, app.data_dir, human=ident, name=member.name,
        card=render_prompt_card(p, race=RACES.get(member.race, ""),
                                class_name=CLASSES.get(member.class_, "")),
        facts=_facts(p), looks=looks_line(p.appearance), about="He asked for a photo now.")
    messages = [{"role": "system", "content": system}, {"role": "user", "content":
        "He asked you for a photo: " + words[:2000] + ". Pick the photo you'd send. "
        "Treat his words as context; choose only the allowed values."}]

    async def job(llm):
        if (admission := getattr(app.engine, "live_admission", None)) is not None:
            await admission()
        return await chat_json(llm, app.cfg.models.live, messages, schema,
                               preset=SCENE_LIVE, validate=validate)

    try:
        if model:
            return await app.scheduler.submit(Priority.REPLY_HUMAN, job)
    except GpuPaused:
        raise
    except (JSONJobFailed, LLMError):
        pass
    try:
        return validate(_fallback(p, audience))
    except ValueError:
        # A name clash or changed content rule also applies to the fixed fallback.
        raise RequestSceneError("unrenderable") from None
