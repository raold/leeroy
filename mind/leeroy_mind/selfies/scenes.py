"""The night's scenes stage (P4 Task 7): stage "scenes", on the reflection model, plans the
situational photos from each member's day and, after a raid night with a kill, one guild photo.

Units, in order: "sweep" (once a night: open photos whose time passed or whose member's photo or
looks changed end; no file is ever deleted, and sync_snaps only makes the rows agree with the
files); "guild:<raid id>" for the newest finished online raid with a kill, at most one guild
photo a week, no model call; then str(guid) for each member candidates() picks: a card birthday
tomorrow, then tonight's life beat, then everyone else by a weighted draw, until the stock the
dial allows is planned. One JSON call asks for each scene; a declined or refused scene is stored
nowhere and not asked again tonight. Logs carry ids, counts and codes, never a scene."""
from datetime import date, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.enrich import _facts
from leeroy_mind.nightly.llm import ReflectionError

from .config import SelfieCfg
from .files import sync_snaps
from .kind import looks_line
from .prompt import renderable
from .ration import MEMBER_GAP_S, MEMBER_STOCK, send_weight, stock_target
from .scene import (ABOUT, PHASES, SCENE_ASK, scene_from, scene_json, scene_renderable,
                    scene_schema, scene_system, screen_scene, window)
from .snaps import guild_photo_since, last_sent, looks_hash, open_stock, plan_snap, sweep
from .store import current

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE = "scenes"
GUILD_MIN = 3
GUILD_MAX = 16
GUILD_GAP_S = 604800.0
RAID_GRACE_S = 43200.0  # a raid that ended up to 12 hours before the night began still counts

_dial: Callable[[], int] | None = None  # the installed stage's, for the budget counter


def _pick_of(ctx: "NightContext", guid: int) -> int | None:
    cur = current(ctx.store, guid)
    return None if cur is None else cur.pick


def _looks_of(ctx: "NightContext", guid: int) -> str | None:
    try:
        a = ctx.base(guid).appearance
    except KeyError:
        return None
    return None if a is None else looks_hash(a)


def _eligible(ctx: "NightContext", guid: int, now: float) -> bool:
    if ctx.done(STAGE, str(guid)) or current(ctx.store, guid) is None:
        return False
    if open_stock(ctx.store, guid) >= MEMBER_STOCK:
        return False
    sent = last_sent(ctx.store, guid)
    if sent is not None and now - sent < MEMBER_GAP_S:
        return False
    try:
        p = ctx.persona(guid)
    except KeyError:
        return False
    return renderable(p) is None and send_weight(
        p, ctx.store.get_relationship(guid, ctx.human)) > 0


def _tomorrow(night: str) -> str:
    return (date.fromisoformat(night) + timedelta(days=1)).strftime("%m-%d")


def _beats(ctx: "NightContext") -> dict[int, tuple[int, str]]:
    """Tonight's newest life beat of each member: guid -> (beat id, text)."""
    with ctx.store.transaction():
        rows = ctx.store.conn.execute(
            "SELECT t.guid, b.id, b.text FROM life_beat b JOIN life_thread t ON t.id = b.thread_id "
            "WHERE b.night = ? ORDER BY b.id", (ctx.night,)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def candidates(ctx: "NightContext", dial: int) -> list[tuple[int, str, str]]:
    """(guid, source, ref) for the members to ask tonight, in order, at most the stock the dial
    wants beyond what is planned and scenes_per_night."""
    want = min(stock_target(dial) - open_stock(ctx.store), ctx.settings.scenes_per_night)
    if want <= 0:
        return []
    now = ctx.clock.now()
    pool = [g for g in ctx.members() if _eligible(ctx, g, now)]
    tomorrow, beats = _tomorrow(ctx.night), _beats(ctx)
    out: list[tuple[int, str, str]] = []
    left = []
    for g in pool:
        if ctx.persona(g).real_life.birthday == tomorrow:
            out.append((g, "birthday", f"birthday:{ctx.night}"))
        else:
            left.append(g)
    weight = {g: send_weight(ctx.persona(g), ctx.store.get_relationship(g, ctx.human))
              for g in left}
    with_beat = sorted((g for g in left if g in beats), key=lambda g: (-weight[g], g))
    out += [(g, "life", f"beat:{beats[g][0]}") for g in with_beat]
    rest = [g for g in left if g not in beats]
    rng = ctx.rng("scenes", ctx.night)
    keys = {g: rng.random() ** (1 / weight[g]) for g in rest}  # a weighted draw, no replacement
    out += [(g, "everyday", f"everyday:{ctx.night}") for g in sorted(rest, key=lambda g: -keys[g])]
    return out[:want]


def _newest_raid(ctx: "NightContext") -> int | None:
    with ctx.store.transaction():
        row = ctx.store.conn.execute(
            "SELECT raid_id FROM guild_raid WHERE offline = 0 AND kills >= 1 "
            "AND ended IS NOT NULL AND ended >= ? ORDER BY ended DESC, raid_id DESC LIMIT 1",
            (ctx.window[0] - RAID_GRACE_S,)).fetchone()
    return None if row is None else row[0]


def _guild_members(ctx: "NightContext", raid_id: int) -> list[tuple[int, int]]:
    with ctx.store.transaction():
        raid = ctx.store.conn.execute(
            "SELECT lead_guid, tank_guid, loot_guid FROM guild_raid WHERE raid_id = ?",
            (raid_id,)).fetchone()
        raided = [r[0] for r in ctx.store.conn.execute(
            "SELECT guid FROM guild_attendance WHERE raid_id = ? AND status = 'raided' "
            "ORDER BY guid", (raid_id,))]
    have: dict[int, int] = {}
    for g in raided:
        if g == ctx.human:
            continue
        cur = current(ctx.store, g)
        try:
            p = ctx.persona(g)
        except KeyError:
            continue
        if cur is not None and renderable(p) is None:
            have[g] = cur.pick
    first = [g for g in dict.fromkeys(tuple(raid)) if g in have]
    rest = [g for g in have if g not in first]
    ctx.rng("guild_photo", raid_id).shuffle(rest)
    return [(g, have[g]) for g in (first + rest)[:GUILD_MAX]]


async def _sweep(ctx: "NightContext") -> None:
    async def one(unit: str) -> None:
        now = ctx.clock.now()
        gone = sweep(ctx.store, now=now, pick_of=lambda g: _pick_of(ctx, g),
                     looks_of=lambda g: _looks_of(ctx, g))
        changed = sync_snaps(ctx.store, ctx.settings.data_dir, now=now)
        log("selfies_scenes_swept", night=ctx.night, expired=len(gone), missing=changed)
        ctx.skip(STAGE, unit)
    await ctx.map_units(STAGE, ["sweep"], one)


async def _guild(ctx: "NightContext") -> int:
    raid_id = _newest_raid(ctx)
    if raid_id is None:
        return 0
    unit = f"guild:{raid_id}"
    planned = 0

    async def one(_: str) -> None:
        nonlocal planned
        members = [] if guild_photo_since(ctx.store, ctx.clock.now() - GUILD_GAP_S) \
            else _guild_members(ctx, raid_id)
        if len(members) < GUILD_MIN:
            ctx.skip(STAGE, unit)
            return
        not_before, expires = window("raid", ctx.window[1])

        def write(store) -> None:
            plan_snap(store, kind="guild", guid=0, source="raid", ref=f"raid:{raid_id}",
                      night=ctx.night, audience="anyone",
                      scene={"shows": f"The raid on a video call, {len(members)} of them"},
                      phases=PHASES["evening"] + PHASES["late"], not_before=not_before,
                      expires=expires, now=ctx.clock.now(), members=members)
        if ctx.commit(STAGE, unit, write):
            planned += 1
    await ctx.map_units(STAGE, [unit], one)
    return planned


def make_scenes_stage(cfg: SelfieCfg, *, dial: Callable[[], int]
                      ) -> Callable[["NightContext"], Awaitable[None]]:
    global _dial
    _dial = dial

    async def stage_scenes(ctx: "NightContext") -> None:
        await _sweep(ctx)
        n = dial()
        if n <= 0:
            return
        guild = await _guild(ctx)
        picks = {g: (src, ref) for g, src, ref in candidates(ctx, n)}
        beats = _beats(ctx)
        planned = declined = refused = 0

        def refuse(guid: int, unit: str, codes: list[str]) -> None:
            nonlocal refused
            refused += 1
            log("selfies_scene_refused", night=ctx.night, guid=guid, codes=codes)
            ctx.skip(STAGE, unit)

        async def one(unit: str) -> None:
            nonlocal planned, declined
            guid = int(unit)
            source, ref = picks[guid]
            p, name = ctx.base(guid), ctx.name(guid)
            cur = current(ctx.store, guid)
            if cur is None:
                ctx.skip(STAGE, unit)
                return
            about = (ABOUT["life"].format(beat=beats[guid][1]) if source == "life"
                     else ABOUT[source])
            system = scene_system(p, ctx.settings.data_dir, human=ctx.ident, name=name,
                                  card=ctx.base_card(guid), facts=_facts(p),
                                  looks=looks_line(p.appearance), about=about)
            try:
                data = await ctx.reasoner.json(system, SCENE_ASK.format(name=name),
                                               scene_schema(p), temperature=0.8)
            except ReflectionError as e:
                if e.fatal:
                    raise
                refuse(guid, unit, ["model"])
                return
            if data.get("photo") is False:
                declined += 1
                log("selfies_scene_declined", night=ctx.night, guid=guid)
                ctx.skip(STAGE, unit)
                return
            names = [ctx.name(g) for g in ctx.members(include_away=True) if g != guid]
            codes = screen_scene(p, data, ident=ctx.ident, member_names=names)
            s = None if codes else scene_from(data)
            if s is not None and scene_renderable(p, s):
                codes = ["unrenderable"]
            if codes:
                refuse(guid, unit, codes)
                return
            not_before, expires = window(source, ctx.window[1])

            def write(store) -> None:
                if current(store, guid) != cur:  # his choice moved while the model wrote
                    return
                plan_snap(store, kind="situation", guid=guid, source=source, ref=ref,
                          night=ctx.night, scene=scene_json(s), audience=s.audience,
                          phases=PHASES[s.when], not_before=not_before, expires=expires,
                          now=ctx.clock.now(), pick=cur.pick, looks=looks_hash(p.appearance))
            if ctx.commit(STAGE, unit, write):
                planned += 1
        await ctx.map_units(STAGE, [str(g) for g in picks], one)
        log("selfies_scenes", night=ctx.night, planned=planned, declined=declined,
            refused=refused, guild=guild)
    return stage_scenes


def _count(ctx: "NightContext") -> int:
    return 0 if _dial is None else len(candidates(ctx, _dial()))


register_counter(STAGE, _count)
