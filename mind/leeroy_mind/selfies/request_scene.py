"""One live enum choice for an owner request, before borrowing the image GPU; with
`[photos] scene_from_words`, one structured reading of his words (choose_words_scene)."""
from dataclasses import replace

from leeroy_mind.llm.client import GpuPaused, LLMError
from leeroy_mind.llm.presets import JSONJobFailed, LIVE_BIT, Preset, chat_json
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.logs import log
from leeroy_mind.nightly.enrich import _facts
from leeroy_mind.nightly.llm import check_schema
from leeroy_mind.personas.card import render_prompt_card
from leeroy_mind.personas.io import load_persona
from leeroy_mind.wow import CLASSES, RACES

from . import files
from .kind import looks_line
from .prompt import over_fit, renderable
from .ration import SEND_MIN_AGE
from .romance_styling import enum_category, level, restyle
from .scene import (Scene, scene_from, scene_json, scene_renderable, scene_schema, scene_system,
                    screen_scene)
from .scene_choices import compose_choices, pairing_codes
from .store import current
from .style import LUST, style_of

SCENE_LIVE = Preset(False, dict(LIVE_BIT.options) | {"num_predict": 512}, timeout=30.0)
# the reading of his words: LIVE's sampling and num_ctx, think false, no stop, room for 7 fields
WORDS_LIVE = Preset(False, dict(LIVE_BIT.options) | {"num_predict": 320}, timeout=30.0)


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


def _subject(app, guid: int, audience: str):
    """(her persona, her roster row, his identity, the names a scene may not hold, the audience)
    for a request's scene, or RequestSceneError with a code."""
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
    ident, member = app.engine.human, app.store.cast().get(guid)
    if member is None:
        raise RequestSceneError("no_member")
    return p, member, ident, request_names(app, guid), audience


def _style(app, guid: int, scene: Scene, *, category, seed, ident, names) -> Scene:
    if not app.selfies.cfg.romance_styling:
        return scene
    return restyle(app.personas.get(guid), scene, enabled=True, category=category,
                   relationship=app.store.get_relationship(guid, ident.guid, lens=True),
                   dial=app.cfg.romance.dial, seed=seed, ident=ident, names=names)


async def choose_scene(app, guid: int, words: str, audience: str, *, model: bool = True) -> Scene:
    """Choose at most twice, then use a screened everyday choice. Never store his words.
    `model=False` takes that everyday choice at once, without the live model.

    The scheduler's job awaits admission itself: a borrow can begin while this job queues.
    chat_json owns the single retry, including schema, pairing and content failures.
    """
    p, member, ident, names, audience = _subject(app, guid, audience)
    schema = scene_schema(p)
    seed = app.clock.now()

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
        scene = _style(app, guid, scene, category=enum_category(data), seed=seed,
                       ident=ident, names=names)
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


def request_names(app, guid: int) -> list[str]:
    """The names no request scene of `guid` may hold: his nickname and character name, and every
    other cast member's name and real name (as _subject reads them)."""
    ident, roster = app.engine.human, app.store.cast()
    names = [ident.nickname, ident.character_name]
    for other in roster.values():
        if other.guid != guid:
            names.append(other.name)
            if (persona := app.personas.base(other.guid)) is not None:
                names.append(persona.identity.real_name)
    return [n for n in names if n]


async def choose_words_scene(app, guid: int, words: str, audience: str, *, request: int,
                             block=None, hour: int | None = None) -> dict:
    """The request's scene read from his words (words_scene): one structured call on the live
    model through its queue and admission (`format` = SCHEMA, think false; chat_json's single
    retry for a reply off the schema), and no other model call. An age word or a child anywhere
    in the reply's free text refuses all of it (log `scene_words_refused reason=age|minor`): the
    enum path's everyday choice. Otherwise each refused field falls back on its own, the place to
    words_scene.fallback_place (also when the prompt would not fit the worker). A reply still off
    the schema, a model error or an unrenderable result takes the enum path's everyday choice.
    Returns the snap's scene dict. GpuPaused propagates (the caller takes the everyday choice).
    `block`: her day plan's block at the photo's time, or None; `hour`: her local hour then.
    Logs `selfie_request_scene` with ids and codes only."""
    from . import words_scene as W
    p, member, ident, names, audience = _subject(app, guid, audience)
    hour = app.clock.local().hour if hour is None else hour
    private_style = (app.selfies.cfg.romance_styling and audience == "him"
        and level(app.personas.get(guid),
                  app.store.get_relationship(guid, ident.guid, lens=True),
                  app.cfg.romance.dial) is not None)

    def validate(data):
        try:
            check_schema(W.SCHEMA, data)
        except ValueError:
            # Unexpected JSON keys can contain his words; never echo those in a retry.
            raise ValueError("scene_words.schema") from None
        return data

    system = W.system(app.data_dir, human=ident, name=member.name,
        card=render_prompt_card(p, race=RACES.get(member.race, ""),
                                class_name=CLASSES.get(member.class_, "")),
        facts=_facts(p), looks=looks_line(p.appearance), block=block, private_style=private_style)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": W.ask(words)}]

    async def job(llm):
        if (admission := getattr(app.engine, "live_admission", None)) is not None:
            await admission()
        return await chat_json(llm, app.cfg.models.live, messages, W.SCHEMA,
                               preset=WORDS_LIVE, validate=validate)

    async def everyday(reason: str) -> dict:
        log("selfie_request_scene", request=request, guid=guid, source="enum", reason=reason)
        return scene_json(await choose_scene(app, guid, words, audience, model=False))

    try:
        data = await app.scheduler.submit(Priority.REPLY_HUMAN, job)
    except GpuPaused:
        raise
    except (JSONJobFailed, LLMError) as error:
        return await everyday("schema" if isinstance(error, JSONJobFailed) else "model")
    if (hit := W.age_hit(data)) is not None:
        log("scene_words_refused", request=request, guid=guid, reason=hit)
        return await everyday(hit)
    screened = W.screen_reply(p, data, ident=ident, names=names)
    codes = list(screened.fallbacks)
    place, where = (screened.place, "words") if screened.place else W.fallback_place(p, words)
    w = W.resolve(p, data, screened, place=place, block=block, hour=hour, seed=request,
                  private_style=private_style)

    def composed(w):
        scene = W.scene_of(p, w, audience=audience)
        category = "as_now" if data["wearing"] == "as_now" else w.wearing
        styled = _style(app, guid, scene_from(scene), category=category, seed=request,
                        ident=ident, names=names)
        scene.update(scene_json(styled))  # keep the words recipe's framing and light codes
        return scene

    scene = composed(w)
    if screened.place is not None and (scene_renderable(p, scene_from(scene)) is None
                                       and over_fit(W.build_words_prompt(p, scene))):
        place, where = W.fallback_place(p, words)
        codes.append("place:prompt_fit")
        w = W.resolve(p, data, screened, place=place, block=block, hour=hour, seed=request,
                      private_style=private_style)
        scene = composed(w)
    if scene_renderable(p, scene_from(scene)) is not None:
        return await everyday("unrenderable")
    if where != "words":
        codes.append(where)
    scene["words"]["fallbacks"] = codes + [c for c in w.fallbacks if c not in codes]
    log("selfie_request_scene", request=request, guid=guid, source="words",
        framing=w.framing, wearing=w.wearing, time=w.time, lighting=w.lighting,
        place=where.removeprefix("place:"), moment="words" if w.moment else "none",
        prop="words" if w.prop else "none", fallbacks=scene["words"]["fallbacks"])
    return scene
