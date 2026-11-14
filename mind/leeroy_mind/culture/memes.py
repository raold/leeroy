"""Original memes (D5 Part C, `[culture] memes`): image memes the guild makes about its own
running jokes, planned at night and posted in #memes by a member.

- **Planning** (stage "meme_plan", reflection model, after "language"): at most one a night,
  at most `[culture] memes_per_week` in any seven days, spaced through the week. One active,
  public joke with a rebuildable origin (culture.seeds) and no meme in the last 30 days is
  chosen; the model writes only the caption (`top`, `bottom`, at most 60 characters each) and
  picks the picture: one of MEME_SCENES (places and objects, never people), or the joke's butt's
  own photo when one was already posted in a public channel. The caption must pass the photo
  screens (EXPLICIT_RE, WEAR_RE, AGE_RE), the real-name screen, and name no member but the
  joke's own butt and originator; a refused caption stores nothing.
- **Rendering** (stage "meme_render", image model, after the photo stages): a plain worker
  (no identity) renders the scene from MEME_SCENES' fixed words only, so no name can ever reach
  an image prompt, with the NSFW gate and the selfie GPU rules (never under a live lease; his
  login ends the worker's whole process tree). The worker then draws the caption with Pillow
  (selfie/leeroy_selfie/caption.py) and scores the captioned image again. The file is
  mind-data/discord/memes/<id>.jpg. A flagged or screened meme is `refused`; no meme file is
  ever deleted (a flagged render is never written by the worker at all).
- **Posting**: a ready meme is a `meme_image` seed (culture.seeds) for #memes; the conversation's
  opener carries it as a D3 attachment of kind `meme` (MemeOffer), and it is `posted` in the
  message's own transaction. A meme not posted within a week is no longer offered.

Logs carry ids, counts and codes, never a caption, a scene or a name."""
from dataclasses import dataclass
import logging
import math
from pathlib import Path
import random
import time
from typing import Callable

from leeroy_mind.discord.store import AttachmentIn
from leeroy_mind.logs import log as log_event

log = logging.getLogger(__name__)

PLAN_STAGE = "meme_plan"
RENDER_STAGE = "meme_render"
MEMES_DIR = "discord/memes"
WEEK_S = 7 * 86400.0
READY_S = 7 * 86400.0  # a ready meme is offered for a week after it was planned
JOKE_GAP_S = 30 * 86400.0  # a joke gets at most one meme in this long
OWN_PHOTO = "own_photo"
MAX_CAPTION = 60
CONTENDED = 2  # worker failures in a row that end the stage for the night

# Places and objects with no people in them: the only words an image prompt can hold.
MEME_SCENES: dict[str, str] = {
    "raid_desk": "an empty gaming desk at night with energy drink cans and a glowing monitor",
    "burnt_pizza": "a burnt pizza on a kitchen counter",
    "cat_keyboard": "a cat asleep on a gaming keyboard",
    "snack_pile": "a pile of empty snack wrappers beside a gaming mouse",
    "rainy_window": "a rainy window at night with a desk lamp glowing",
    "dish_mountain": "a mountain of dirty dishes in a kitchen sink",
    "flat_tire": "a parked car with a flat tire in the rain",
    "traffic_cone": "a lone traffic cone in an empty parking lot",
    "wilted_plant": "a wilted houseplant on a sunny windowsill",
    "smoking_toaster": "a toaster with a thin wisp of smoke in a small kitchen",
    "goldfish": "a goldfish in a round bowl on a wooden table",
    "dog_bowl": "a dog staring at an empty food bowl on a kitchen floor",
    "spilled_cereal": "a spilled bowl of cereal on a kitchen floor",
    "cracked_laptop": "a laptop with a cracked screen on an unmade bed",
    "tipped_chair": "an office chair tipped over on a wooden floor",
    "melted_candle": "a melted candle on a dinner table",
    "lonely_sock": "a single sock on top of a laundry pile",
    "trash_can": "an overflowing trash can in a quiet alley",
    "router_lights": "a home internet router with blinking lights on a shelf",
    "coffee_spill": "a knocked over coffee mug on a desk covered in papers",
}
SCENE_STYLE = (("candid everyday photo, natural light, sharp focus", 2),)
SCENE_NEGATIVE: tuple[tuple[str, int], ...] = (
    ("person, people, man, woman, child, face, hands, body, crowd", 0),
    ("nsfw, nude", 0),
    ("text, letters, words, writing, watermark, logo", 1),
    ("cartoon, illustration, 3d render", 2),
    ("blurry, low quality, deformed", 3),
)

CAPTION_SYSTEM = (
    "You make one original image meme for a gaming guild's private Discord, about a running "
    "joke the guild really has. Write the classic two-line caption: a top line and a bottom line, "
    "each at most 60 characters, funny and kind, never cruel. Use only the recorded fact; invent "
    "no events and name nobody who is not in it. No real-world names, no slurs, nothing sexual. "
    "Pick the picture under the caption from the listed choices.")
CAPTION_ASK = ("The running joke: {fact}\n\nPictures to choose from:\n{choices}\n\n"
               "Write the caption and choose the picture.")


def scene_spec(key: str) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...]]:
    """A scene's render phrases and negatives, from MEME_SCENES' fixed words alone: nothing a
    member, the owner or the model wrote can reach an image prompt."""
    return ((MEME_SCENES[key], 0), *SCENE_STYLE), SCENE_NEGATIVE


def caption_codes(text, *, ident, names: dict[int, str], actors: frozenset[int]) -> list[str]:
    """Why a caption line is refused (empty: it passes). The photo screens, the real-name screen,
    a member named who is not in the joke, and the shape the worker accepts."""
    from leeroy_mind.personas.screens import has_word, names_real_name
    from leeroy_mind.selfies.kind import EXPLICIT_RE, WEAR_RE
    from leeroy_mind.selfies.style import AGE_RE
    from leeroy_mind.text import has_meta
    if (not isinstance(text, str) or len(text) > MAX_CAPTION or text != text.strip()
            or any(not c.isprintable() for c in text)):
        return ["shape"]
    codes = []
    if has_meta(text) or "|" in text:
        codes.append("meta")
    for code, pattern in (("explicit", EXPLICIT_RE), ("wear", WEAR_RE), ("age", AGE_RE)):
        if pattern.search(text):
            codes.append(code)
    if ident is not None and names_real_name(text, ident):
        codes.append("real_name")
    if any(guid not in actors and name and has_word(text, name) for guid, name in names.items()):
        codes.append("name")
    return codes


def made_since(conn, since: float) -> int:
    return conn.execute("SELECT count(*) FROM discord_meme WHERE created>=?", (since,)).fetchone()[0]


def plan_due(conn, now: float, per_week: int) -> str | None:
    """Why no meme is planned now (None: one may be): `off` at 0 a week, `cap` with the week's
    memes made, `spacing` within the even share of a week since the last one."""
    if per_week <= 0:
        return "off"
    if made_since(conn, now - WEEK_S) >= per_week:
        return "cap"
    last = conn.execute("SELECT max(created) FROM discord_meme").fetchone()[0]
    if last is not None and now - last < 0.8 * WEEK_S / per_week:
        return "spacing"
    return None


def own_snap(conn, data_dir: Path, guid: int, *, nsfw_max: float, personas) -> int | None:
    """The butt's own situational photo already posted in a public channel (a plain one, scored
    under the NSFW limit, its file still there, its member 25 or older), newest first."""
    import json
    from leeroy_mind.selfies import files
    from leeroy_mind.selfies.ration import SEND_MIN_AGE
    p = personas.get(guid)
    if p is None or p.identity.age < SEND_MIN_AGE:
        return None
    for sid, scene, nsfw in conn.execute(
            "SELECT id,scene,nsfw FROM selfie_snap WHERE kind='situation' AND guid=? AND audience='anyone' "
            "AND status='sent' AND channel='discord' ORDER BY sent DESC,id DESC LIMIT 8", (guid,)):
        try:
            data = json.loads(scene)
        except (TypeError, ValueError):
            continue
        if (isinstance(data, dict) and data.get("audience") == "anyone" and data.get("style", "plain") == "plain"
                and type(nsfw) in (int, float) and math.isfinite(nsfw) and nsfw < nsfw_max
                and files.snap(data_dir, guid, sid).is_file()):
            return sid
    return None


def meme_path(data_dir: Path, meme: int, *, base: bool = False) -> Path:
    return Path(data_dir) / MEMES_DIR / (f"{meme}.base.jpg" if base else f"{meme}.jpg")


def _jokes(ctx, library):
    from leeroy_mind.culture.seeds import _culture_seeds
    personas = {g: p for g in ctx.members(include_away=True) if (p := ctx.personas.get(g)) is not None}
    now = ctx.clock.now()
    recent = {r[0] for r in ctx.store.conn.execute(
        "SELECT joke FROM discord_meme WHERE joke IS NOT NULL AND created>=?", (now - JOKE_GAP_S,))}
    return personas, [s for s in _culture_seeds(ctx.store, now=now, personas=personas, human=ctx.human,
                                                 include_recent=True, library=library, ident=ctx.ident)
                      if int(s.key[5:]) not in recent]


def make_plan_stage(app, selfies_cfg) -> Callable:
    """The "meme_plan" stage: one caption a night at most, written by the reflection model."""
    from leeroy_mind.nightly.llm import ReflectionError, schema
    from leeroy_mind.personas.screens import redact_real_name

    async def stage_meme_plan(ctx) -> None:
        unit = "plan"
        if ctx.done(PLAN_STAGE, unit):
            return
        now = ctx.clock.now()
        why = plan_due(ctx.store.conn, now, app.culture_config.memes_per_week)
        library = Path(ctx.settings.data_dir) / "library.db"
        personas, jokes = ({}, []) if why else _jokes(ctx, library)
        if why is None and not jokes:
            why = "no_joke"
        if why is not None:
            ctx.skip(PLAN_STAGE, unit)
            log_event("culture_meme_plan", night=ctx.night, planned=0, code=why)
            return
        joke = random.Random(f"memes:{ctx.night}").choice(jokes)
        butt, originator = joke.about
        snap = (own_snap(ctx.store.conn, ctx.settings.data_dir, butt, nsfw_max=selfies_cfg.nsfw_max,
                         personas=personas) if butt and butt != ctx.human else None)
        choices = {**MEME_SCENES, **({OWN_PHOTO: "the photo the person this joke is about posted"} if snap else {})}

        async def one(_: str) -> None:
            fact = redact_real_name(joke.fact, ctx.ident, ctx.ident.called)
            try:
                reply = await ctx.reasoner.json(
                    CAPTION_SYSTEM, CAPTION_ASK.format(fact=fact, choices="\n".join(
                        f"- {key}: {text}" for key, text in choices.items())),
                    schema("culture_meme", {"top": {"type": "string", "maxLength": MAX_CAPTION},
                                            "bottom": {"type": "string", "maxLength": MAX_CAPTION},
                                            "picture": {"type": "string", "enum": list(choices)}}),
                    temperature=0.9)
            except ReflectionError as error:
                if error.fatal:
                    raise
                ctx.skip(PLAN_STAGE, unit)
                log_event("culture_meme_plan", night=ctx.night, planned=0, code="model")
                return
            top = " ".join(str(reply.get("top", "")).split())
            bottom = " ".join(str(reply.get("bottom", "")).split())
            picture = reply.get("picture")
            names = {g: ctx.name(g) for g in (*ctx.members(include_away=True), ctx.human)}
            actors = frozenset({butt, originator} - {0})
            codes = [] if (top or bottom) else ["empty"]
            for text in (top, bottom):
                codes += caption_codes(text, ident=ctx.ident, names=names, actors=actors)
            if picture not in choices:
                codes.append("picture")
            if codes:
                ctx.skip(PLAN_STAGE, unit)
                log_event("culture_meme_refused", night=ctx.night, joke=int(joke.key[5:]),
                          codes=sorted(set(codes)))
                return

            def write(store) -> None:
                store.conn.execute(
                    "INSERT INTO discord_meme(night,joke,base,snap,scene,top,bottom,status,created) "
                    "VALUES (?,?,?,?,?,?,?,'planned',?)",
                    (ctx.night, int(joke.key[5:]), "snap" if picture == OWN_PHOTO else "scene",
                     snap if picture == OWN_PHOTO else None, None if picture == OWN_PHOTO else picture,
                     top, bottom, ctx.clock.now()))
            if ctx.commit(PLAN_STAGE, unit, write):
                log_event("culture_meme_plan", night=ctx.night, planned=1, base=(
                    "snap" if picture == OWN_PHOTO else "scene"))
        await ctx.map_units(PLAN_STAGE, [unit], one)
    return stage_meme_plan


# -- rendering --------------------------------------------------------------------------------

def _planned(conn, now: float) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM discord_meme WHERE status='planned' AND created>? ORDER BY id", (now - READY_S,))]


def _todo(ctx) -> list[dict]:
    return [r for r in _planned(ctx.store.conn, ctx.clock.now()) if not ctx.done(RENDER_STAGE, f"meme:{r['id']}")]


def wants_gpu(ctx) -> bool:
    """Whether a meme is left to render tonight: one with nothing to render takes no GPU."""
    return bool(_todo(ctx))


def _recheck(ctx, row, *, library, nsfw_max) -> str | None:
    """Why a planned meme can no longer be made: its joke gone or private, its caption screened,
    its photo no longer public and safe."""
    from leeroy_mind.culture.seeds import _culture_seeds
    personas = {g: p for g in ctx.members(include_away=True) if (p := ctx.personas.get(g)) is not None}
    found = [s for s in _culture_seeds(ctx.store, now=ctx.clock.now(), personas=personas, human=ctx.human,
                                       include_recent=True, only=row["joke"], library=library, ident=ctx.ident)
             if s.key == f"joke:{row['joke']}"]
    if not found:
        return "joke"
    butt, originator = found[0].about
    names = {g: ctx.name(g) for g in (*ctx.members(include_away=True), ctx.human)}
    for text in (row["top"], row["bottom"]):
        if caption_codes(text, ident=ctx.ident, names=names, actors=frozenset({butt, originator} - {0})):
            return "caption"
    if row["base"] == "snap":
        if row["snap"] is None or own_snap(ctx.store.conn, ctx.settings.data_dir, butt, nsfw_max=nsfw_max,
                                            personas=personas) != row["snap"]:
            return "snap"
    elif row["scene"] not in MEME_SCENES:
        return "scene"
    return None


def _refuse(ctx, row, why: str) -> None:
    unit = f"meme:{row['id']}"

    def write(store) -> None:
        store.conn.execute("UPDATE discord_meme SET status='refused' WHERE id=? AND status='planned'", (row["id"],))
    if ctx.commit(RENDER_STAGE, unit, write):
        log_event("culture_meme_refused", night=ctx.night, meme=row["id"], codes=[why])


async def _render_one(ctx, cfg, worker, row) -> str:
    from leeroy_mind.selfies import files
    from leeroy_mind.selfies.render import _unleased, seed_for
    from leeroy_mind.selfies.slot import SetRefused, render_slot
    from leeroy_mind.selfies.worker import RenderRequest, WorkerError
    data = Path(ctx.settings.data_dir)
    meme = row["id"]
    out = meme_path(data, meme).absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    nsfw = 0.0
    if row["base"] == "snap":
        base = files.snap(data, _snap_guid(ctx, row["snap"]), row["snap"]).absolute()
    else:
        base = meme_path(data, meme, base=True).absolute()
        thumb = base.with_name(f"{meme}.base.thumb.jpg")
        phrases, negative = scene_spec(row["scene"])

        def request(attempt: int) -> RenderRequest:
            _unleased(lambda: ctx.arbiter.holders)  # never under a live lease
            return RenderRequest(phrases, negative, seed_for(ctx.night, 0, meme, 0, attempt), cfg.steps,
                                 cfg.guidance, cfg.size, base, thumb, 256, cfg.nsfw_max)

        async def room() -> None:
            await ctx.arbiter.make_room()
            _unleased(lambda: ctx.arbiter.holders)
        try:
            got, _ = await render_slot(worker, request, rid=lambda a: f"m{meme}:{a}", make_room=room)
        except SetRefused:
            _refuse(ctx, row, "flagged")
            return "refused"
        if not base.is_file():
            raise WorkerError("unwritten")
        nsfw = got.nsfw
    _unleased(lambda: ctx.arbiter.holders)
    captioned = await worker.caption(base, top=row["top"], bottom=row["bottom"], out=out,
                                     nsfw_max=cfg.nsfw_max, rid=f"m{meme}:caption")
    if captioned.flagged or not math.isfinite(captioned.nsfw) or captioned.nsfw >= cfg.nsfw_max:
        _refuse(ctx, row, "flagged")
        return "refused"
    if not out.is_file():
        raise WorkerError("unwritten")

    def write(store) -> None:
        store.conn.execute("UPDATE discord_meme SET status='ready',path=?,nsfw=? WHERE id=? AND status='planned'",
                           (out.relative_to(data.absolute()).as_posix(), max(nsfw, captioned.nsfw), meme))
        store.set_meta(SIZE_KEY.format(id=meme), f"{captioned.width}x{captioned.height}")
    if not ctx.commit(RENDER_STAGE, f"meme:{meme}", write):
        return "failed"
    log_event("culture_meme_ready", night=ctx.night, meme=meme, base=row["base"])
    return "ready"


def _snap_guid(ctx, snap: int) -> int:
    row = ctx.store.conn.execute("SELECT guid FROM selfie_snap WHERE id=?", (snap,)).fetchone()
    return row[0] if row else -1


def make_render_stage(app, cfg, *, worker=None) -> Callable:
    """The "meme_render" stage, an image stage. `worker` makes the night's worker (tests pass
    the fake); by default the selfie/ venv's plain worker (no identity) from `cfg`."""
    from leeroy_mind.nightly.gpu import LiveLeaseHeld
    from leeroy_mind.selfies.config import worker_command, worker_env
    from leeroy_mind.selfies.render import start_worker
    from leeroy_mind.selfies.worker import SelfieWorker, WorkerError, to_the_end

    def make():
        return worker() if worker is not None else SelfieWorker(worker_command(cfg), env=worker_env(cfg))

    async def start(ctx, w) -> bool:
        from leeroy_mind.selfies.render import _unleased
        _unleased(lambda: ctx.arbiter.holders)
        try:
            await start_worker(w, ctx.arbiter.make_room)
        except LiveLeaseHeld:
            raise
        except Exception as error:  # WorkerError (stopped: his login), OSError, a failed make_room
            if not (isinstance(error, WorkerError) and error.reason == "stopped"):
                ctx.fail(RENDER_STAGE, "worker", error)
            log_event("culture_meme_unavailable", level=logging.WARNING, night=ctx.night,
                      reason=getattr(error, "reason", type(error).__name__))
            return False
        return True

    async def stage_meme_render(ctx) -> None:
        todo = _todo(ctx)
        if not todo:
            return
        library = Path(ctx.settings.data_dir) / "library.db"
        w, started, contended, tally = make(), False, 0, {"ready": 0, "refused": 0, "failed": 0}
        try:
            for row in todo:
                unit = f"meme:{row['id']}"
                try:
                    why = _recheck(ctx, row, library=library, nsfw_max=cfg.nsfw_max)
                except Exception as error:
                    ctx.fail(RENDER_STAGE, unit, error)
                    tally["failed"] += 1
                    continue
                if why is not None:
                    _refuse(ctx, row, why)
                    tally["refused"] += 1
                    continue
                if not started:
                    if not await start(ctx, w):
                        break
                    started = True
                began = time.monotonic()
                try:
                    outcome = await _render_one(ctx, cfg, w, row)
                except LiveLeaseHeld:
                    raise
                except WorkerError as error:
                    ctx.fail(RENDER_STAGE, unit, error)
                    tally["failed"] += 1
                    contended += 1
                    if contended >= CONTENDED or not w.running:
                        break
                    continue
                except Exception as error:
                    ctx.fail(RENDER_STAGE, unit, error)
                    tally["failed"] += 1
                    continue
                contended = 0
                tally[outcome] = tally.get(outcome, 0) + 1
                log_event("culture_meme_rendered", night=ctx.night, meme=row["id"], outcome=outcome,
                          seconds=round(time.monotonic() - began, 1))
        finally:
            # his login's cancel too: the whole tree is gone before the live model's preload
            await to_the_end(w.stop())
        log_event("culture_memes_done", night=ctx.night, **tally)
    return stage_meme_render


# -- posting ----------------------------------------------------------------------------------

def ready_memes(conn, now: float) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM discord_meme WHERE status='ready' AND path IS NOT NULL AND created>? ORDER BY id",
        (now - READY_S,))]


SIZE_KEY = "culture:meme:size:{id}"  # meta: the captioned image's "<width>x<height>", from the worker


def _attachment(store, data_dir: Path, row) -> AttachmentIn | None:
    from leeroy_mind.imagefiles import read_image
    data = read_image(Path(data_dir) / row["path"], Path(data_dir))
    try:
        width, height = map(int, str(store.get_meta(SIZE_KEY.format(id=row["id"]))).split("x"))
    except (TypeError, ValueError):
        return None
    if data is None or not (16 <= width <= 8192 and 16 <= height <= 8192):
        return None
    return AttachmentIn("meme", row["path"], width, height, len(data))


@dataclass
class MemeOffer:
    """A ready meme carried by its poster's opening line, in the shape the talk pipeline takes a
    photo in (photos.offer.PhotoOffer): stored as a `meme` attachment, and marked posted inside
    the message's own transaction (on_sent)."""
    meme: int
    instruction: str
    attachment: AttachmentIn
    on_sent: Callable[[int, int], None]
    valid: Callable[[], bool]
    reason: str = "withdrawn"
    snap = None  # never a member's photo row

    def release(self) -> None:
        log_event("culture_meme_released", meme=self.meme, code=self.reason)


def offer(app, meme: int) -> MemeOffer | None:
    """The meme `meme` for its poster's opener, or None when it is no longer ready or its file is
    gone."""
    store = app.store
    row = store.conn.execute("SELECT * FROM discord_meme WHERE id=?", (meme,)).fetchone()
    if row is None or row["status"] != "ready" or not row["path"]:
        return None
    attachment = _attachment(store, app.data_dir, row)
    if attachment is None:
        return None
    caption = " / ".join(t for t in (row["top"], row["bottom"]) if t)

    def valid() -> bool:
        current = store.conn.execute("SELECT status FROM discord_meme WHERE id=?", (meme,)).fetchone()
        return (current is not None and current[0] == "ready" and app.clock.now() < row["created"] + READY_S
                and _attachment(store, app.data_dir, row) is not None)

    def sent(line_id: int, message: int) -> None:
        if store.conn.execute("UPDATE discord_meme SET status='posted',posted_message=? WHERE id=? "
                              "AND status='ready'", (message, meme)).rowcount != 1:
            raise RuntimeError("meme is no longer ready")
        log_event("culture_meme_posted", meme=meme, message=message)

    return MemeOffer(meme, "You're posting a meme picture you made about it; its caption reads: "
                     f"\"{caption}\". Write the short message you'd post with it, in your own way; "
                     "don't repeat the caption.", attachment, sent, valid)
