"""python -m leeroy_mind selfies status | try (P2 Task 14).

status is read-only (a mode=ro connection, never a migration): the queue's counts and the
latest night's selfie stages. try is the acceptance run before enabling: it holds the mind's
instance lock (a fresh arbiter here cannot see the running mind's live leases, so it could
unload the live model in his session), copies the database, and renders one set for each chosen
member into <data>/scratch/selfies-try/<guid>/. Nothing is written to the live data. Output
carries counts, guids, timings and paths; never a prompt, persona text or a name."""
import argparse
import json
import asyncio
import shutil
import sqlite3
import sys
from dataclasses import dataclass

from leeroy_mind.app import AlreadyRunning
from leeroy_mind.clock import SystemClock
from leeroy_mind.config import load_config
from leeroy_mind.console.selfieview import queue_view
from leeroy_mind.nightly.cli import (_child, _context, _data_arg, _default_night, _exclusive,
                                     _positive, _print, _read_store, _root, _working_store)

from . import files
from .config import load_selfie_config, worker_command, worker_env
from .kind import STAGE as KIND_STAGE
from .kind import _saver, chosen_style, kind_one
from .prompt import build_prompt, renderable
from .scene import ABOUT, build_scene_prompt, build_tile_prompt
from .scenes import STAGE as SCENES_STAGE
from .scenes import ask_scene
from .slot import SetRefused
from .snaps import counts
from .snapstage import GUILD_THUMB_PX, GUTTER_PX, SNAP_THUMB_PX, TILE_PX, SnapRenderer
from .snapstage import STAGE as SNAPS_STAGE
from .stage import STAGE, render_candidates, start_worker
from .store import current
from .worker import SelfieWorker, WorkerError, to_the_end

DEFAULT_LIMIT = 2
TRY_DIR = "selfies-try"
GUILD_TILES = 4


def _parser():
    parser = argparse.ArgumentParser(prog="leeroy_mind selfies")
    _data_arg(parser)
    subs = parser.add_subparsers(dest="action", required=True)
    subs.add_parser("status", help="the photo queue and the latest night's selfie stages")
    sub = subs.add_parser("try", help="render a trial set for a few members into scratch/ "
                          "(the mind must be stopped)")
    pick = sub.add_mutually_exclusive_group()
    pick.add_argument("--guid", type=_positive)
    pick.add_argument("--limit", type=_positive, default=DEFAULT_LIMIT)
    sub.add_argument("--snaps", action="store_true",
                     help="one situational photo each with the member's own face, into "
                     "scratch/selfies-try/snaps/")
    sub.add_argument("--guild", action="store_true",
                     help="with --snaps: also one grid of up to 4 of them")
    for sub in subs.choices.values():
        _data_arg(sub, child=True)
    return parser


def _stages(conn) -> dict:
    """The latest night that has a selfies or photo stage record: the records and their unit
    counts. Empty when the tables are not there (an older database)."""
    names = (STAGE, KIND_STAGE, SCENES_STAGE, SNAPS_STAGE)
    marks = ",".join("?" * len(names))
    try:
        row = conn.execute(f"SELECT max(night) FROM night_stage WHERE stage IN ({marks})",
                           names).fetchone()
        night = row[0] if row else None
        if night is None:
            return {}
        out = {}
        for r in conn.execute(f"SELECT * FROM night_stage WHERE night=? AND stage IN ({marks}) "
                              "ORDER BY started", (night, *names)):
            rec = dict(r)
            rec["duration_s"] = max(0, rec["finished"] - rec["started"])
            rec["units"] = conn.execute("SELECT count(*) FROM night_step WHERE night=? AND "
                                        "stage=?", (night, rec["stage"])).fetchone()[0]
            out[rec["stage"]] = rec
        return out
    except sqlite3.Error:
        return {}


def _status(store, clock) -> dict:
    view = queue_view(store.conn, None, now=clock.now())
    for key in ("queue", "recent"):
        view[key] = [{k: v for k, v in item.items() if k != "name"} for item in view[key]]
    try:
        snaps = counts(store.conn, now=clock.now())
    except sqlite3.Error:  # an older database has no selfie_snap
        snaps = {}
    return {"queue": view, "snaps": snaps, "stages": _stages(store.conn),
            "booth": _booth_status(store)}


def _booth_status(store) -> dict:
    """The booth's latest pass and separate daily records, including on a day with no night."""
    from .booth import LAST, STAGE as BOOTH
    try:
        row = store.conn.execute("SELECT * FROM night_stage WHERE stage=? "
                                 "ORDER BY night DESC LIMIT 1", (BOOTH,)).fetchone()
        if row is None:
            return {}
        rec = dict(row)
        rec["day"] = rec["night"].removeprefix("booth:")
        rec["units"] = store.conn.execute("SELECT count(*) FROM night_step WHERE night=? "
            "AND stage=? AND unit<>'queue'", (rec["night"], BOOTH)).fetchone()[0]
        rec["last"] = json.loads(store.get_meta(LAST) or "{}")
        return rec
    except (sqlite3.Error, ValueError, TypeError):
        return {}


class _Measured:
    """The worker, tallying what each render took and found."""

    def __init__(self, worker: SelfieWorker):
        self.worker, self.images, self.flagged, self.seconds, self.peak = worker, 0, 0, 0.0, 0.0

    @property
    def running(self):
        return self.worker.running

    async def render(self, req, *, rid=None):
        got = await self.worker.render(req, rid=rid)
        self.images += 1
        self.flagged += bool(got.flagged)
        self.seconds += got.seconds
        self.peak = max(self.peak, got.peak_gib)
        return got


@dataclass
class _Tally:
    members: int = 0
    sets: int = 0


def _chosen(ctx, args) -> list[int]:
    looks = [g for g in ctx.members(include_away=True) if ctx.base(g).appearance is not None]
    if args.guid is not None:
        if args.guid not in looks:
            raise ValueError(f"guid {args.guid} is not a cast member with looks")
        return [args.guid]
    return looks[:args.limit]


async def _kinds(ctx, store, guids: list[int]) -> list[int]:
    """The members of `guids` that can be rendered, a missing photo kind made on the trial
    copy only."""
    needing = [g for g in guids if ctx.base(g).photo is None]
    if needing:
        await ctx.reasoner.preflight()
        await ctx.arbiter.ensure_model(ctx.settings.reflection_model)
    ready = []
    for guid in guids:
        if guid in needing:
            photo, _ = await kind_one(ctx, guid)
            if photo is None:
                print(f"guid {guid}: no photo kind could be made", file=sys.stderr)
                continue
            with store.transaction():
                _saver(ctx, guid, photo)(store)
            ctx.personas.invalidate(guid)
        if renderable(ctx.base(guid), chosen_style(store, ctx.base(guid))) is not None:
            print(f"guid {guid}: cannot be rendered", file=sys.stderr)
            continue
        ready.append(guid)
    return ready


async def _room_after(ctx) -> None:
    """Once the worker is gone: mod-ollama-chat keeps asking while the mind is stopped, so a
    chat model may have loaded beside the worker partly on the CPU. It is dropped, to load
    whole on its next request, as the night does after its image stage."""
    try:
        await ctx.arbiter.make_room()
    except Exception as e:
        print(f"the chat models could not be unloaded after the try ({type(e).__name__}): "
              "check `ollama ps` before the mind starts", file=sys.stderr)


async def _try(root, store, cfg, scfg, clock, args, make_llm) -> int:
    night = _default_night(clock)
    async with _context(root, store, cfg, clock, night, make_llm=make_llm,
                        live_locked=True) as ctx:
        guids = _chosen(ctx, args)
        tally = _Tally(members=len(guids))
        ready = await _kinds(ctx, store, guids)
        out_root = _child(_child(root, "scratch"), TRY_DIR)
        worker = _Measured(SelfieWorker(worker_command(scfg), env=worker_env(scfg)))
        if ready:
            try:
                await ctx.arbiter.make_room()
                await start_worker(worker.worker, ctx.arbiter.make_room)
                for guid in ready:
                    folder = out_root / str(guid)
                    folder.mkdir(parents=True, exist_ok=True)
                    try:
                        await render_candidates(
                            worker, build_prompt(ctx.base(guid), kind_style=chosen_style(
                                store, ctx.base(guid))), folder, night=night,
                            guid=guid, set_no=1, cfg=scfg, make_room=ctx.arbiter.make_room,
                            leases=lambda: ctx.arbiter.holders)
                        tally.sets += 1
                    except WorkerError as e:
                        print(f"guid {guid}: worker {e.reason}", file=sys.stderr)
                    except Exception as e:
                        print(f"guid {guid}: {type(e).__name__}", file=sys.stderr)
            except WorkerError as e:
                print(f"worker {e.reason}", file=sys.stderr)
            finally:
                await to_the_end(worker.worker.stop())
                await _room_after(ctx)
        _print({"members": tally.members, "sets": tally.sets, "flagged": worker.flagged,
                "seconds_per_image": round(worker.seconds / worker.images, 2) if worker.images
                else None, "peak_gib": worker.peak, "path": str(out_root)})
        return int(tally.sets != tally.members)


async def _guild_grid(ctx, r, worker, members, out_root) -> list:
    """Each of `members` alone at a square tile, then one grid at snaps/guild.jpg; the tiles that
    were made, [] when the grid was not."""
    made = []
    for n, (guid, (_, ref)) in enumerate(members):
        p = ctx.base(guid)
        tile = out_root / str(guid) / "tile.jpg"
        try:
            await r.image(snap_id=0, guid=guid, idx=1, spec=build_tile_prompt(p), ref=ref,
                          framing=p.photo.framing, out=tile,
                          thumb=tile.with_name("tile.thumb.jpg"), thumb_size=128,
                          shape="square", rid=f"g{n}")
            made.append(tile)
        except (SetRefused, WorkerError) as e:
            print(f"guid {guid}: no tile ({getattr(e, 'reason', 'flagged')})", file=sys.stderr)
    if not made:
        return []
    try:
        await worker.worker.grid(made, tile=TILE_PX, gutter=GUTTER_PX, out=out_root / "guild.jpg",
                                 thumb=out_root / "guild.thumb.jpg", thumb_size=GUILD_THUMB_PX,
                                 rid="grid")
    except WorkerError as e:
        print(f"grid {e.reason}", file=sys.stderr)
        return []
    return made


async def _try_snaps(root, store, cfg, scfg, clock, args, make_llm) -> int:
    night = _default_night(clock)
    async with _context(root, store, cfg, clock, night, make_llm=make_llm,
                        live_locked=True) as ctx:
        looks = _chosen(ctx, argparse.Namespace(guid=args.guid, limit=10 ** 6))
        picked = {g: current(store, g) for g in looks}
        withs = [g for g in looks if picked[g] is not None]
        kinded = [g for g in withs if ctx.base(g).photo is not None]
        guids = kinded if args.guid is not None else kinded[:args.limit]
        no_photo = len(looks) - len(withs) if args.guid is None else int(not withs)
        no_kind = len(withs) - len(kinded)
        out_root = _child(_child(_child(root, "scratch"), TRY_DIR), "snaps")
        worker = _Measured(SelfieWorker(worker_command(scfg, identity=True),
                                        env=worker_env(scfg)))
        scenes, rendered, tiles = {}, 0, []
        if guids:
            await ctx.reasoner.preflight()
            await ctx.arbiter.ensure_model(ctx.settings.reflection_model)
        for guid in guids:
            scene, codes = await ask_scene(ctx, guid, about=ABOUT["everyday"])
            if scene is None:
                print(f"guid {guid}: no scene ({','.join(codes)})", file=sys.stderr)
                continue
            ref = files.history(ctx.settings.data_dir, guid, picked[guid].pick)
            if not ref.is_file():
                print(f"guid {guid}: the photo in force has no history file", file=sys.stderr)
                continue
            scenes[guid] = (scene, ref)
        if scenes:
            r = SnapRenderer(ctx, scfg, worker)
            try:
                await ctx.arbiter.make_room()
                await start_worker(worker.worker, ctx.arbiter.make_room)
                for n, (guid, (scene, ref)) in enumerate(scenes.items()):
                    folder = out_root / str(guid)
                    folder.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ref, folder / "ref.jpg")
                    p = ctx.base(guid)
                    try:
                        await r.image(snap_id=0, guid=guid, idx=0,
                                      spec=build_scene_prompt(p, scene), ref=ref,
                                      framing=p.photo.framing, out=folder / "snap.jpg",
                                      thumb=folder / "snap.thumb.jpg", thumb_size=SNAP_THUMB_PX,
                                      shape="portrait", rid=f"t{n}")
                        rendered += 1
                    except SetRefused:
                        print(f"guid {guid}: flagged on every seed", file=sys.stderr)
                    except WorkerError as e:
                        print(f"guid {guid}: worker {e.reason}", file=sys.stderr)
                if args.guild:
                    tiles = await _guild_grid(ctx, r, worker, list(scenes.items())[:GUILD_TILES],
                                              out_root)
            except WorkerError as e:
                print(f"worker {e.reason}", file=sys.stderr)
            finally:
                await to_the_end(worker.worker.stop())
                await _room_after(ctx)
        _print({"members": len(guids), "no_photo": no_photo, "no_kind": no_kind, "rendered": rendered,
                "flagged": worker.flagged,
                "seconds_per_image": round(worker.seconds / worker.images, 2) if worker.images
                else None, "peak_gib": worker.peak, "path": str(out_root)})
        return int(not guids or rendered != len(guids) or (args.guild and not tiles))


def selfies_main(argv=None, *, make_llm=None) -> int:
    args = _parser().parse_args(argv)
    clock = SystemClock()
    try:
        root = _root(args)
        if args.action == "status":
            with _read_store(root / "mind.db", clock) as store:
                _print(_status(store, clock))
            return 0
        if args.guild and not args.snaps:
            print("selfies try: --guild goes with --snaps", file=sys.stderr)
            return 2
        cfg = load_config(root / "mind.toml")
        scfg = load_selfie_config(root / "mind.toml")
        with _exclusive(root, gpu=True):
            with _working_store(root, clock, live=False, label="selfies-try") as (store, _):
                run = _try_snaps if args.snaps else _try
                return asyncio.run(run(root, store, cfg, scfg, clock, args, make_llm))
    except AlreadyRunning as exc:
        print(f"{exc}; selfies try would share its GPU and could unload its live model")
        return 1
    except KeyboardInterrupt:
        print("selfies interrupted")
        return 130
    except Exception as exc:
        print(f"selfies {args.action} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
