"""python -m leeroy_mind selfies status | try (P2 Task 14) | vary.

status is read-only (a mode=ro connection, never a migration): the queue's counts and the
latest night's selfie stages. try is the acceptance run before enabling: it holds the mind's
instance lock (a fresh arbiter here cannot see the running mind's live leases, so it could
unload the live model in his session), copies the database, and renders one set for each chosen
member into <data>/scratch/selfies-try/<guid>/. Nothing is written to the live data. Output
carries counts, guids, timings and paths; never a prompt, persona text or a name.

vary --stance lust plans (the default, read-only) or queues (--go) a varied set (varied.py) for
every cast member of the lust style. With the mind stopped --go holds its instance lock and
writes the rows itself; while it runs, --go asks the running mind's `selfie` op (`vary`), the
one writer of photo rows then. Rendering stays the night's and the booth's."""
import argparse
import json
import asyncio
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.app import AlreadyRunning
from leeroy_mind.clock import SystemClock
from leeroy_mind.config import load_config
from leeroy_mind.console.selfieview import queue_view
from leeroy_mind.nightly.cli import (_child, _context, _data_arg, _default_night, _exclusive, _identity,
                                     _positive, _print, _read_store, _root, _working_store)

from . import files, photo_extra
from .config import load_selfie_config, worker_command, worker_env
from .kind import STAGE as KIND_STAGE
from .kind import _saver, chosen_style, kind_one
from .prompt import build_prompt, build_varied_prompt, renderable
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
VARY_S = 30.0  # the running mind's answer to a vary: one transaction of queue rows


def varied_stances():
    from .varied import STANCES
    return STANCES
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
    sub.add_argument("--varied", action="store_true",
                     help="a varied set (four looks and poses) for lust-styled members")
    sub = subs.add_parser("restyle", help="plan or save varied photo choices (mind stopped)")
    sub.add_argument("--lust-only", action="store_true")
    sub.add_argument("--include-owner", action="store_true", help="also replace hand-set photo choices")
    mode = sub.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only plan (default)")
    mode.add_argument("--go", action="store_true", help="save choices and queue fresh sets")
    sub = subs.add_parser("vary", help="plan or queue a varied set (four looks and poses) for "
                          "every member of a stance's photo style")
    sub.add_argument("--stance", choices=varied_stances(), required=True)
    sub.add_argument("--guid", type=lambda text: tuple(sorted({_positive(g) for g in
                                                               text.split(",")})),
                     help="only these members (comma-separated guids)")
    mode = sub.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only plan (default)")
    mode.add_argument("--go", action="store_true", help="queue the sets for the night")
    sub = subs.add_parser("variety", help="plan or re-roll similar chosen faces (mind stopped)")
    sub.add_argument("--top", type=_positive, default=30)
    sub.add_argument("--release", type=lambda text: tuple(_positive(g) for g in text.split(",")),
                     default=(),help="re-roll named protected GUIDs listed in a dry-run cluster")
    mode = sub.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only plan (default)")
    mode.add_argument("--go", action="store_true", help="save looks and queue new candidates")
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
        self.last_nsfw = None

    @property
    def running(self):
        return self.worker.running

    async def render(self, req, *, rid=None):
        got = await self.worker.render(req, rid=rid)
        self.last_nsfw = got.nsfw
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


async def _kinds(ctx, store, guids: list[int], *, variety=False) -> list[int]:
    """The members of `guids` that can be rendered, a missing photo kind made on the trial
    copy only."""
    needing = [g for g in guids if ctx.base(g).photo is None]
    if needing:
        await ctx.reasoner.preflight()
        await ctx.arbiter.ensure_model(ctx.settings.reflection_model)
    ready = []
    for guid in guids:
        if guid in needing:
            photo, _ = await kind_one(ctx, guid, variety=variety)
            if photo is None:
                print(f"guid {guid}: no photo kind could be made", file=sys.stderr)
                continue
            with store.transaction():
                _saver(ctx, guid, photo, variety=variety)(store)
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
        plans = {}
        if args.varied:  # no photo kind needed and no model call: the looks come from lists
            from . import varied
            from .style import suggestive
            pool = [g for g in _chosen(ctx, argparse.Namespace(guid=args.guid, limit=10 ** 6))
                    if suggestive(ctx.base(g))]
            guids = pool if args.guid is not None else pool[:args.limit]
            if args.guid is not None and not guids:
                print(f"guid {args.guid}: not of the lust style", file=sys.stderr)
            plans = {g: varied.compose(ctx.base(g), f"try:{g}:{night}") for g in guids}
            for guid in [g for g in guids if plans[g] is None]:
                print(f"guid {guid}: no safe varied look", file=sys.stderr)
            ready = [g for g in guids if plans[g] is not None]
            tally = _Tally(members=max(1, len(guids)) if args.guid is not None else len(guids))
        else:
            guids = _chosen(ctx, args)
            tally = _Tally(members=len(guids))
            if scfg.variety:
                from .kind import _personas
                photo_extra.seed(store,_personas(ctx),clock.now())
            ready = await _kinds(ctx, store, guids, variety=scfg.variety)
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
                        p = ctx.base(guid)
                        spec = (tuple(build_varied_prompt(p, x) for x in plans[guid])
                                if args.varied else build_prompt(
                                    p, kind_style=chosen_style(store, p),
                                    light=photo_extra.light_for(store, p)))
                        await render_candidates(
                            worker, spec, folder, night=night,
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
                else None, "peak_gib": worker.peak, "path": str(out_root),
                **({"looks": {str(g): [x.codes() for x in plans[g]] for g in ready}}
                   if args.varied else {})})
        return int(tally.sets != tally.members)


def _snap_result(results, guid, status, *, codes=(), nsfw=None, kind="situation"):
    results.append({"guid": guid, "kind": kind, "status": status,
                    "codes": list(codes), "nsfw": nsfw})
    score = "unavailable" if nsfw is None else f"{nsfw:.4f}"
    print(f"guid {guid}: {kind} {status} ({','.join(codes)}) nsfw={score}", file=sys.stderr)


async def _guild_grid(ctx, r, worker, members, out_root, results, diagnostic) -> list:
    """Each of `members` alone at a square tile, then one grid at snaps/guild.jpg; the tiles that
    were made, [] when the grid was not."""
    made = []
    made_guids = []
    for n, (guid, (_, ref)) in enumerate(members):
        p = ctx.base(guid)
        tile = out_root / str(guid) / "tile.jpg"
        worker.last_nsfw = None
        try:
            got, _, _ = await r.image(snap_id=0, guid=guid, idx=1, spec=build_tile_prompt(p), ref=ref,
                          framing=p.photo.framing, out=tile,
                          thumb=tile.with_name("tile.thumb.jpg"), thumb_size=128,
                          shape="square", rid=f"g{n}")
            made.append(tile)
            made_guids.append(guid)
            _snap_result(results, guid, "rendered", nsfw=got.nsfw, kind="tile")
        except (SetRefused, WorkerError) as e:
            _snap_result(results, guid, "refused", codes=[getattr(e, "reason", "flagged")],
                         nsfw=worker.last_nsfw, kind="tile")
    if not made:
        return []
    try:
        grid = await worker.worker.grid(made, tile=TILE_PX, gutter=GUTTER_PX, out=out_root / "guild.jpg",
                                 thumb=out_root / "guild.thumb.jpg", thumb_size=GUILD_THUMB_PX,
                                 rid="grid")
        diagnostic.update(grid.similarity or {"code": "worker_unsupported"})
        diagnostic["metric"] = "uncalibrated_head_crop_cosine"
        if diagnostic["code"] == "ok":
            diagnostic["guids"] = [made_guids[i] for i in diagnostic.pop("pair")]
            print(f"guild faces alike (uncalibrated): max={diagnostic['max']:.4f} "
                  f"mean={diagnostic['mean']:.4f}", file=sys.stderr)
        else:
            print(f"guild faces alike: skipped ({diagnostic['code']})", file=sys.stderr)
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
        results, diagnostic = [], {"code": "not_rendered" if args.guild else "not_requested"}
        for guid in looks:
            if guid not in withs:
                _snap_result(results, guid, "refused", codes=["no_photo"])
            elif guid not in kinded:
                _snap_result(results, guid, "refused", codes=["no_kind"])
        if guids:
            await ctx.reasoner.preflight()
            await ctx.arbiter.ensure_model(ctx.settings.reflection_model)
        for guid in guids:
            scene, codes = await ask_scene(ctx, guid, about=ABOUT["everyday"])
            if scene is None:
                _snap_result(results, guid, "declined" if codes == ["declined"] else "refused",
                             codes=codes)
                continue
            ref = files.history(ctx.settings.data_dir, guid, picked[guid].pick)
            if not ref.is_file():
                _snap_result(results, guid, "refused", codes=["missing_ref"])
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
                    worker.last_nsfw = None
                    try:
                        got, _, _ = await r.image(snap_id=0, guid=guid, idx=0,
                                      spec=build_scene_prompt(p, scene), ref=ref,
                                      framing=p.photo.framing, out=folder / "snap.jpg",
                                      thumb=folder / "snap.thumb.jpg", thumb_size=SNAP_THUMB_PX,
                                      shape="portrait", rid=f"t{n}")
                        rendered += 1
                        _snap_result(results, guid, "rendered", nsfw=got.nsfw)
                    except SetRefused:
                        _snap_result(results, guid, "refused", codes=["flagged"],
                                     nsfw=worker.last_nsfw)
                    except WorkerError as e:
                        _snap_result(results, guid, "refused", codes=[e.reason],
                                     nsfw=worker.last_nsfw)
                if args.guild:
                    tiles = await _guild_grid(ctx, r, worker, list(scenes.items())[:GUILD_TILES],
                                              out_root, results, diagnostic)
            except WorkerError as e:
                print(f"worker {e.reason}", file=sys.stderr)
                recorded = {row["guid"] for row in results if row["kind"] == "situation"}
                for guid in scenes.keys() - recorded:
                    _snap_result(results, guid, "refused", codes=[e.reason])
            finally:
                await to_the_end(worker.worker.stop())
                await _room_after(ctx)
        _print({"members": len(guids), "no_photo": no_photo, "no_kind": no_kind, "rendered": rendered,
                "flagged": worker.flagged, "results": results, "guild_similarity": diagnostic,
                "seconds_per_image": round(worker.seconds / worker.images, 2) if worker.images
                else None, "peak_gib": worker.peak, "path": str(out_root)})
        return int(not guids or rendered != len(guids) or (args.guild and not tiles))


def _call_running_mind(root, args: dict) -> dict:
    """The running mind's answer to one `selfie` op, through its control endpoint, located as the
    console locates it (this checkout's mind process family, control.json, the TCP table)."""
    from leeroy_mind import lifecycle
    from leeroy_mind.console.mindlink import MindLink
    checkout = Path(lifecycle.__file__).resolve().parents[2]
    found = lifecycle.family(lifecycle.WindowsProcesses().snapshot(),
                             lifecycle.venv_python(checkout),
                             lifecycle.mind_base_python(checkout), root)
    return MindLink(root).call([p.pid for p in found], "selfie", args, timeout=VARY_S)


def _vary(root, clock, args, call_mind) -> int:
    from . import varied
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    cfg = load_config(root / "mind.toml")
    scfg = load_selfie_config(root / "mind.toml")
    if not args.go:
        with _read_store(root / "mind.db", clock) as store:
            report = varied.queue(store, human=_identity(cfg, store).guid, now=clock.now(),
                                  day=clock.local().date().isoformat(), go=False,
                                  stance=args.stance, guids=args.guid)
        _print({**report, "switch": scfg.varied_sets})
        return 0
    if not (scfg.enabled and scfg.varied_sets):
        print("selfies vary: needs [selfies] enabled = true and varied_sets = true in mind.toml",
              file=sys.stderr)
        return 1
    try:
        with _exclusive(root, gpu=False):
            from leeroy_mind.__main__ import _snapshot_file
            _snapshot_file(root / "mind.db", root / "snapshots", "pre-vary")  # open_db migrates
            with closing(Store(open_db(root / "mind.db"), clock=clock)) as store:
                report = varied.queue(store, human=_identity(cfg, store).guid, now=clock.now(),
                                      day=clock.local().date().isoformat(), go=True,
                                      stance=args.stance, guids=args.guid)
            _print(report)
            return 0
    except AlreadyRunning:
        pass
    from leeroy_mind.console.mindlink import LinkError
    from leeroy_mind.lifecycle import OwnershipError
    try:
        ask = {"action": "vary", "go": True,
               **({"guids": list(args.guid)} if args.guid else {})}
        reply = (call_mind or _call_running_mind)(root, ask)
    except (LinkError, OwnershipError, OSError) as e:
        print(f"selfies vary: the mind is running and could not be asked ({e})", file=sys.stderr)
        return 1
    _print(reply)
    return 0 if reply.get("ok") is True else 1


def selfies_main(argv=None, *, make_llm=None, make_worker=None, call_mind=None) -> int:
    args = _parser().parse_args(argv)
    clock = SystemClock()
    try:
        root = _root(args)
        if args.action == "vary":
            return _vary(root, clock, args, call_mind)
        if args.action == "status":
            with _read_store(root / "mind.db", clock) as store:
                _print(_status(store, clock))
            return 0
        if args.action == "restyle":
            from .restyle import run
            from leeroy_mind.storage.db import open_db
            from leeroy_mind.storage.store import Store
            cfg = load_config(root / "mind.toml")
            with _exclusive(root, gpu=False):
                if args.go:
                    from leeroy_mind.__main__ import _snapshot_file
                    _snapshot_file(root / "mind.db", root / "snapshots", "pre-restyle")
                context = (closing(Store(open_db(root / "mind.db"), clock=clock)) if args.go
                           else _read_store(root / "mind.db", clock))
                with context as store:
                    _print(run(store,_identity(cfg,store),clock,lust_only=args.lust_only,
                               go=args.go,include_owner=args.include_owner))
            return 0
        if args.action == "variety":
            from .variety import run
            from .similarity import MAX_PHOTOS
            from leeroy_mind.storage.db import open_db
            from leeroy_mind.storage.store import Store
            if args.top > MAX_PHOTOS:
                raise ValueError("top_out_of_range")
            cfg = load_config(root / "mind.toml")
            scfg = load_selfie_config(root / "mind.toml")
            with _exclusive(root, gpu=True):
                if args.go:
                    from leeroy_mind.__main__ import _snapshot_file
                    _snapshot_file(root / "mind.db", root / "snapshots", "pre-variety")
                context = (closing(Store(open_db(root / "mind.db"), clock=clock)) if args.go
                           else _read_store(root / "mind.db", clock))
                with context as store:
                    _print(asyncio.run(run(root, store, scfg, _identity(cfg, store), clock,
                        top=args.top, go=args.go, make_worker=make_worker,release=args.release)))
            return 0
        if args.guild and not args.snaps:
            print("selfies try: --guild goes with --snaps", file=sys.stderr)
            return 2
        if args.varied and args.snaps:
            print("selfies try: --varied renders profile sets, not --snaps", file=sys.stderr)
            return 2
        cfg = load_config(root / "mind.toml")
        scfg = load_selfie_config(root / "mind.toml")
        with _exclusive(root, gpu=True):
            with _working_store(root, clock, live=False, label="selfies-try") as (store, _):
                run = _try_snaps if args.snaps else _try
                return asyncio.run(run(root, store, cfg, scfg, clock, args, make_llm))
    except AlreadyRunning as exc:
        if args.action == "restyle":
            print("selfies restyle: mind must be stopped", file=sys.stderr)
        elif args.action == "variety":
            print("selfies variety: mind must be stopped", file=sys.stderr)
        else:
            print(f"{exc}; selfies try would share its GPU and could unload its live model")
        return 1
    except KeyboardInterrupt:
        print("selfies interrupted")
        return 130
    except Exception as exc:
        if args.action == "restyle":
            from leeroy_mind.config import ConfigError
            known = {"personas_changed":"personas_changed", "too_many_members":"too_many_members",
                     "cannot identify the human from the stored roster":"human_unidentified",
                     "stored human is absent from the roster":"human_not_on_roster"}
            message = (known[str(exc)] if type(exc) is ValueError and str(exc) in known else
                       "config_invalid" if isinstance(exc, ConfigError) else
                       "database_error" if isinstance(exc, sqlite3.Error) else
                       "filesystem_error" if isinstance(exc, OSError) else "restyle_failed")
        elif args.action == "variety":
            from leeroy_mind.personas.looks_diversity import LooksRefused
            message = (str(exc) if isinstance(exc, (LooksRefused, WorkerError)) else
                       "filesystem_error" if isinstance(exc, OSError) else "variety_failed")
        else:
            message = f"{type(exc).__name__}: {exc}"
        print(f"selfies {args.action} failed: {message}", file=sys.stderr)
        return 1
