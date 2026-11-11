"""Bounded world generation, separate from every cast night consumer."""
from dataclasses import replace
import json

from leeroy_mind.logs import log
from leeroy_mind.nightly import enrich
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.personas.birthdays import draw_birthday
from leeroy_mind.personas.card import render_prompt_card
from leeroy_mind.personas.generate import cast_summary, generate_persona, generation_rules, plan_slots
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.wow import CLASSES, RACES
from .config import load
from .targets import fresh, member, roster, same_member

STAGE = "world_personas"
PLAN = "world:plan:"
FAILED = "world:failed:"


def priority(store,rows):
    rivals={r[0] for r in store.conn.execute("SELECT id FROM realm_guild WHERE status='active'")}
    return sorted(rows,key=lambda g:(rows[g]["guild_id"] not in rivals,rows[g]["guild_id"],rows[g]["rank"],g))


def eligible(ctx):
    if ctx.human <= 0 or not load(ctx.settings.data_dir/"mind.toml").enabled or not fresh(ctx.store,ctx.clock.now()):
        return []
    rows=roster(ctx.store)
    have=ctx.store.persona_versions(tier=None)
    return [g for g in priority(ctx.store,rows) if g != ctx.human and g not in have
            and failures(ctx.store,g)[0] < 3]


def failures(store,guid):
    try:
        value=json.loads(store.get_meta(FAILED+str(guid)) or '[0,""]')
        n,night=value
        return (n,night) if type(n) is int and 0 <= n <= 3 and isinstance(night,str) else (3,"")
    except (ValueError,TypeError):
        return 3,""


def selection(ctx):
    raw=ctx.store.get_meta(PLAN+ctx.night)
    cap=ctx.settings.world_per_night
    if raw is None:
        return eligible(ctx)[:cap]
    try:
        saved=json.loads(raw)
        return saved[:cap] if isinstance(saved,list) and all(type(g) is int and g>0 for g in saved) and len(set(saved))==len(saved) else []
    except (ValueError,TypeError):
        return []


def count(ctx):
    if not load(ctx.settings.data_dir/"mind.toml").enabled:
        return 0
    # The native refresh runs after planning; reserve its upper bound if stale.
    if not fresh(ctx.store,ctx.clock.now()):
        return ctx.settings.world_per_night
    allowed=set(eligible(ctx))
    return sum(g in allowed and not ctx.done(STAGE,str(g)) for g in selection(ctx))


class GeneratedContext:
    """A not-yet-persisted persona for the existing enrichment path, with no cast mutation."""
    private_logs = False
    def __init__(self,ctx,p,rows):
        self.ctx,self.p,self.rows=ctx,p,rows
    def __getattr__(self,name):
        return getattr(self.ctx,name)
    def base(self,guid):
        if guid != self.p.guid:
            raise KeyError(guid)
        return self.p
    def name(self,guid):
        return self.rows[guid]["name"] if guid in self.rows else self.store.cast()[guid].name
    def members(self,include_away=False):
        return sorted(set(self.rows) | set(self.store.cast()))
    def base_card(self,guid):
        row=self.rows[guid]
        return f"Character level: {row['level']}.\n" + render_prompt_card(self.p,
            race=RACES.get(row["race"],""),class_name=CLASSES.get(row["class"],""))


def _sampler_api():
    from leeroy_mind.personas.looks_diversity import sample, LooksRefused
    return sample, LooksRefused


def _looks_sampler():
    """Task 46 hook; keep the fallback independently testable after it merges."""
    try:
        sample, refused = _sampler_api()
    except ModuleNotFoundError as exc:
        if exc.name != "leeroy_mind.personas.looks_diversity":
            raise
        return None
    def safe_sample(*args, **kwargs):
        try:
            return sample(*args, **kwargs)
        except refused:
            return None  # preserve the generation and use screened enrichment
    return safe_sample


async def looks(ctx,p,rows,existing,names):
    # [world] enabled already opts these newly created personas into diversity.
    sample = _looks_sampler()
    if sample is not None:
        p=replace(p,appearance=sample(p,existing,ident=ctx.ident,names=names))
    if p.appearance is None:
        p,_=await enrich.enrich_one(GeneratedContext(ctx,p,rows),p.guid)
        if p is None:
            raise ValueError("world_looks_refused")
    if p.identity.age < 25:
        raise ValueError("world_adult_floor")
    from leeroy_mind.selfies.kind import AGE_RE, EXPLICIT_RE, WEAR_RE, looks_line
    from leeroy_mind.personas.screens import has_word, names_real_name
    text = looks_line(p.appearance)
    if (any(rule.search(text) for rule in (AGE_RE, EXPLICIT_RE, WEAR_RE, enrich.LIKENESS_RE))
        or names_real_name(text,ctx.ident)
        or any(has_word(text,name) for name in (*names,p.identity.real_name) if name)):
        raise ValueError("world_looks_refused")
    return p


def make_stage(cast_config):
    async def generate(ctx):
        if not load(ctx.settings.data_dir/"mind.toml").enabled:
            return
        with ctx.store.transaction():
            chosen=selection(ctx)
            if ctx.store.get_meta(PLAN+ctx.night) is None:
                ctx.store.set_meta(PLAN+ctx.night,json.dumps(chosen))
        for guid in chosen:
            if ctx.done(STAGE,str(guid)) or guid not in eligible(ctx):
                continue
            try:
                rows=roster(ctx.store)
                existing=[]
                for g in sorted(ctx.store.persona_versions(tier=None)):
                    try:
                        p=load_persona(ctx.store,g,include_world=True)
                    except ValueError:
                        continue
                    if p is not None:
                        existing.append(p)
                members=[member(r) for r in rows.values()]
                slots=plan_slots(members,existing,cast_config(),ctx.human,ctx.rng(STAGE,guid),min_level=1)
                slot=next(s for s in slots if s.guid==guid)
                # World voices have no owner romance or automatic cast officer authority.
                slot=replace(slot,romance_stance="none",jealousy_mode=None,
                             guild_role="social" if rows[guid]["level"]<80 else slot.guild_role)
                names={r["name"] for r in rows.values()} | {m.name for m in ctx.store.cast().values()}
                taken=names | {p.identity.real_name for p in existing} | {
                    n for n in (ctx.ident.real_name,ctx.ident.nickname,ctx.ident.character_name) if n}
                rules=generation_rules(ctx.settings.data_dir) + (
                    f"\nWorld character scope: level {rows[guid]['level']}, outside the owner's guild. "
                    "Keep this level explicit; an experienced player can be levelling an alt. "
                    "Do not claim this character raids at endgame when below level 80. "
                    "No romantic interest in the owner; no membership in the owner's guild.")
                p=await generate_persona(ctx.reasoner,ctx.settings.reflection_model,slot,member(rows[guid]),
                    cast_summary(existing,human=ctx.ident),rules,human=ctx.ident,taken_names=frozenset(taken),
                    member_names=frozenset(names),private_logs=False)
                p=replace(p,real_life=replace(p.real_life,birthday=draw_birthday(
                    (q.real_life.birthday for q in existing),ctx.rng(STAGE,guid,"birthday"))))
                p=await looks(ctx,p,rows,existing,tuple(names))
                snapshot=rows[guid]
                saved = False
                def write(store):
                    nonlocal saved
                    if (not load(ctx.settings.data_dir/"mind.toml").enabled or not fresh(store,ctx.clock.now())
                        or not same_member(snapshot, roster(store).get(guid)) or guid in store.persona_versions(tier=None)):
                        return
                    try:
                        store.conn.execute("INSERT OR REPLACE INTO persona_tier VALUES(?,'world')",(guid,))
                        save_persona(store,p,source="generated",ts=ctx.clock.now(),
                            race=RACES.get(snapshot["race"],""),class_name=CLASSES.get(snapshot["class"],""),
                            include_world=True,level=snapshot["level"])
                        saved = True
                    except Exception:
                        raise ValueError("world_persist_failed") from None
                if not ctx.commit(STAGE,str(guid),write):
                    raise ValueError("world_persist_failed")
                if saved:
                    ctx.personas.invalidate(guid)
                    log("world_persona_created",guid=guid)
            except Exception:
                def failed(store):
                    try:
                        n,night=failures(store,guid)
                        store.set_meta(FAILED+str(guid),json.dumps([min(3,n+(night!=ctx.night)),ctx.night]))
                    except Exception:
                        raise ValueError("world_failure_receipt_failed") from None
                ctx.commit(STAGE,str(guid),failed)
                ctx.failures[STAGE] += 1
                log("world_persona_refused",guid=guid)
    return generate


register_counter(STAGE,count)
