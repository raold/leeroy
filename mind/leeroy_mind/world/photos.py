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
    def __getattr__(self,name):
        return getattr(self.ctx,name)
    def members(self,include_away=False):
        cfg = load(self.settings.data_dir / "mind.toml")
        if not cfg.enabled or not cfg.selfies or not fresh(self.store,self.clock.now()):
            return []
        known = self.store.persona_versions(tier="world")
        return [g for g in roster(self.store) if g in known]
    def allows_selfie(self,guid):
        try:
            return guid in self.members() and self.base(guid).identity.age >= 25
        except KeyError:
            return False
    def base(self,guid):
        if guid not in self.members():
            raise KeyError(guid)
        try:
            p = load_persona(self.store,guid,include_world=True)
        except ValueError:
            p = None
        if p is None or p.identity.age < 25:
            raise KeyError(guid)
        return p
    def persona(self,guid):
        return self.base(guid)
    def name(self,guid):
        return roster(self.store)[guid]["name"]
    def base_card(self,guid):
        r = roster(self.store)[guid]
        return f"Character level: {r['level']}.\n" + render_prompt_card(self.base(guid),
            race=RACES.get(r["race"],""),class_name=CLASSES.get(r["class"],""))


def photo_units(ctx):
    scoped = WorldPhotos(ctx)
    picks = []
    for guid in scoped.members():
        try:
            p = scoped.base(guid)
        except KeyError:
            continue
        if kind.needs_kind(p) or ctx.done(PHOTO,str(guid)):
            picks.append(guid)
    return picks[:max(0,ctx.settings.world_selfies_per_night)]


def photo_count(ctx):
    from .stage import count
    cfg = load(ctx.settings.data_dir / "mind.toml")
    if not cfg.enabled or not cfg.selfies:
        return 0
    return min(ctx.settings.world_selfies_per_night,
               sum(not ctx.done(PHOTO,str(g)) for g in photo_units(ctx)) + count(ctx))


async def photo_stage(ctx):
    scoped = WorldPhotos(ctx)
    for guid in photo_units(ctx):
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
    eligible = [g for g in scoped.members() if render._held(scoped,render._stored(scoped,g)) is None]
    pending = sum(s.guid in eligible for s in queued_sets(ctx.store)) + len(needing_first(ctx.store,eligible))
    return min(ctx.settings.world_selfies_per_night,pending + photo_count(ctx))


register_counter(PHOTO,photo_count)
register_counter(IMAGE,image_count)
