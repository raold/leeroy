"""A separate allowance, using the existing screened and gated photo renderer."""
from dataclasses import replace, asdict

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.personas.card import render_prompt_card
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.selfies import kind, render
from leeroy_mind.selfies.config import worker_command, worker_env
from leeroy_mind.selfies.style import style_of
from leeroy_mind.selfies.worker import SelfieWorker, to_the_end
from leeroy_mind.wow import RACES, CLASSES
from .config import load
from .targets import roster, fresh

PHOTO, IMAGE = "world_photos", "world_selfies"


class WorldPhotos:
    private_logs = False
    def __init__(self,ctx):
        self.ctx = ctx
        self._stamp = None
        self._rows, self._personas = {}, {}
        self._cfg_stamp = None
        self._cfg = None
    def __getattr__(self,name):
        return getattr(self.ctx,name)
    def _enabled(self):
        path = self.settings.data_dir / "mind.toml"
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns,stat.st_size)
        except OSError:
            stamp = None
        if self._cfg is None or stamp != self._cfg_stamp:
            self._cfg, self._cfg_stamp = load(path), stamp
        return self._cfg.enabled and self._cfg.selfies
    def _snapshot(self):
        # One roster/persona snapshot per pass, refreshed after local writes. Reads
        # during queue/todo stay O(N); a retirement or config edit still wins after
        # an awaited model/worker call.
        if not self._enabled():
            return False
        stamp = self.store.conn.total_changes
        if stamp != self._stamp:
            self._rows = roster(self.store)
            self._personas = {}
            for guid in self.store.persona_versions(tier="world"):
                if guid not in self._rows:
                    continue
                try:
                    p = load_persona(self.store,guid,include_world=True)
                except ValueError:
                    continue
                if p is not None and p.identity.age >= 25:
                    self._personas[guid] = p
            self._stamp = stamp
        return fresh(self.store,self.clock.now())
    def members(self,include_away=False):
        return list(self._personas) if self._snapshot() else []
    def allows_selfie(self,guid):
        return self._snapshot() and guid in self._personas
    def base(self,guid):
        if not self._snapshot() or guid not in self._personas:
            raise KeyError(guid)
        return self._personas[guid]
    def persona(self,guid):
        return self.base(guid)
    def name(self,guid):
        self.base(guid)
        return self._rows[guid]["name"]
    def base_card(self,guid):
        p = self.base(guid)
        r = self._rows[guid]
        return f"Character level: {r['level']}.\n" + render_prompt_card(p,
            race=RACES.get(r["race"],""),class_name=CLASSES.get(r["class"],""))


def photo_units(ctx, scoped=None):
    scoped = scoped or WorldPhotos(ctx)
    picks = []
    for guid in scoped.members():
        try:
            p = scoped.base(guid)
        except KeyError:
            continue
        if kind.needs_kind(p) or ctx.done(PHOTO,str(guid)):
            picks.append(guid)
    return picks[:max(0,ctx.settings.world_selfies_per_night)]


def photo_count(ctx, scoped=None):
    from .stage import count
    cfg = load(ctx.settings.data_dir / "mind.toml")
    if not cfg.enabled or not cfg.selfies:
        return 0
    return min(ctx.settings.world_selfies_per_night,
               sum(not ctx.done(PHOTO,str(g)) for g in photo_units(ctx,scoped)) + count(ctx))


async def photo_stage(ctx):
    scoped = WorldPhotos(ctx)
    for guid in photo_units(ctx,scoped):
        if ctx.done(PHOTO,str(guid)):
            continue
        try:
            original = scoped.base(guid)
            photo,_ = await kind.kind_one(scoped,guid)
            names = [m.name for m in ctx.store.cast().values()] + [r["name"] for r in roster(ctx.store).values()]
            if photo is not None and kind.screen_kind(original,asdict(photo),ident=ctx.ident,member_names=names):
                photo = None
            def write(store):
                try:
                    p = scoped.base(guid)
                    if p != original or photo is None:
                        return
                    r = roster(store)[guid]
                    save_persona(store,replace(p,photo=photo),source="generated",ts=ctx.clock.now(),
                        race=RACES.get(r["race"],""),class_name=CLASSES.get(r["class"],""),
                        include_world=True,level=r["level"])
                    kind._record(store,guid,style_of(p),photo)
                except KeyError:
                    return  # Retired or disabled while awaiting the model.
                except Exception:
                    raise ValueError("world_photo_persist_failed") from None
            ctx.commit(PHOTO,str(guid),write)
        except Exception:
            ctx.skip(PHOTO,str(guid))
            log("world_photo_refused",guid=guid)


def policy(ctx):
    return render.RenderPolicy(stage=IMAGE,cap=ctx.settings.world_selfies_per_night,
                               record_failures=True)


def wants_gpu(ctx):
    scoped = WorldPhotos(ctx)
    render.queue(scoped,policy(ctx))
    return bool(render.todo(scoped,policy(ctx)))


def image_stage(cfg,*,worker=None):
    async def stage(ctx):
        if not wants_gpu(ctx):
            return
        w = worker() if worker else SelfieWorker(worker_command(cfg),env=worker_env(cfg))
        try:
            await render.render(WorldPhotos(ctx),cfg,w,policy(ctx))
        finally:
            await to_the_end(w.stop())
    return stage


def image_count(ctx):
    scoped = WorldPhotos(ctx)
    from leeroy_mind.selfies.store import queued_sets, needing_first
    eligible = {g for g in scoped.members() if render._held(scoped,render._stored(scoped,g)) is None}
    pending = sum(s.guid in eligible for s in queued_sets(ctx.store)) + len(needing_first(ctx.store,eligible))
    return min(ctx.settings.world_selfies_per_night,pending + photo_count(ctx,scoped))


register_counter(PHOTO,photo_count)
register_counter(IMAGE,image_count)
