"""The night's bounded, resumable persona writer for newly eligible cast members."""
import json
from dataclasses import replace

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.wow import CAST_MIN_LEVEL, CLASSES, RACES

from .birthdays import draw_birthday
from .generate import cast_summary, generate_persona, generation_rules, plan_slots
from .io import load_persona, save_persona
from .seed import seed_relationships

STAGE = "newcomers"
FAILED_NIGHTS = 3
STATE = "nightly:newcomer:"
PLAN = "nightly:newcomers:plan:"


class NewcomerRefused(ValueError):
    """Only a fixed code is safe for NightContext.fail's diagnostic message."""


def _failures(store, guid):
    raw = store.get_meta(f"{STATE}{guid}")
    if raw is None:
        return 0, None
    try:
        saved = json.loads(raw)
        count, night = saved["failed_nights"], saved["last_night"]
        if type(count) is not int or not 0 <= count <= FAILED_NIGHTS or not isinstance(night, str):
            raise ValueError
        return count, night
    except (ValueError, TypeError, KeyError, RecursionError):
        return FAILED_NIGHTS, None  # corrupt retry state cannot grant unlimited calls


def _eligible(ctx):
    if ctx.human <= 0:
        return []  # no reliable owner identity yet
    have = ctx.store.persona_versions()
    return sorted(g for g, member in ctx.store.cast(include_world=True).items()
                  if g > 0 and g != ctx.human and member.level >= CAST_MIN_LEVEL
                  and g not in have and _failures(ctx.store, g)[0] < FAILED_NIGHTS)


def _selection(ctx):
    """A saved nightly roster prevents resume or a later join from exceeding the cap."""
    raw = ctx.store.get_meta(PLAN + ctx.night)
    if raw is None:
        return _eligible(ctx)[:max(0, ctx.settings.newcomers_per_night)]
    try:
        guids = json.loads(raw)
        if (not isinstance(guids, list) or len(guids) != len(set(guids))
                or any(type(g) is not int or g <= 0 for g in guids)):
            raise ValueError
        return guids[:max(0, ctx.settings.newcomers_per_night)]
    except (ValueError, TypeError, RecursionError):
        return []


def count(ctx):
    eligible = set(_eligible(ctx))
    return sum(g in eligible and not ctx.done(STAGE, str(g)) for g in _selection(ctx))


def _failed_night(ctx, guid, *, reported=False):
    """Record one failed night after the failed generation/write transaction has ended."""
    gave_up = False

    def failed(store):
        nonlocal gave_up
        n, last = _failures(store, guid)
        if last != ctx.night:
            n = min(FAILED_NIGHTS, n + 1)
        store.set_meta(f"{STATE}{guid}", json.dumps(
            {"failed_nights": n, "last_night": ctx.night}))
        gave_up = n == FAILED_NIGHTS
    if ctx.commit(STAGE, str(guid), failed):
        if not reported:
            ctx.fail(STAGE, str(guid), NewcomerRefused("generation_refused"))
        log("newcomer_gave_up" if gave_up else "newcomer_refused", guid=guid)


def _existing(store, unreadable):
    """Isolate bad stored records as PersonaCache.base does, without logging text.

    Re-read readable records between units so newly accepted personas reserve
    their names. An unreadable guid is reported once per stage invocation.
    """
    existing = []
    for guid in sorted(store.persona_versions()):
        if guid in unreadable:
            continue
        try:
            persona = load_persona(store, guid)
        except ValueError:  # PersonaError and JSON decoding, matching PersonaCache.base.
            unreadable.add(guid)
            log("newcomer_existing_unreadable", guid=guid)
            continue
        if persona is not None:
            existing.append(persona)
    return existing


def make_stage(cast_config):
    async def stage_newcomers(ctx):
        # Unlike model-parallel stages, name allocation is sequential: each accepted
        # persona participates in the next unit's taken-name and mix constraints.
        with ctx.store.transaction():
            selected = _selection(ctx)
            if ctx.store.get_meta(PLAN + ctx.night) is None:
                ctx.store.set_meta(PLAN + ctx.night, json.dumps(selected))
        cfg = cast_config()
        rules = generation_rules(ctx.settings.data_dir)
        unreadable = set()
        for guid in selected:
            unit = str(guid)
            if ctx.done(STAGE, unit) or guid not in _eligible(ctx):
                continue
            try:
                roster = list(ctx.store.cast(include_world=True).values())
                existing = _existing(ctx.store, unreadable)
                slots = plan_slots(roster, existing, cfg, ctx.human, ctx.rng(STAGE, guid))
                slot = next(s for s in slots if s.guid == guid)
                member = next(m for m in roster if m.guid == guid)
                names = {m.name: m.guid for m in roster}
                taken = set(names) | {p.identity.real_name for p in existing}
                taken.update(n for n in (ctx.ident.nickname, ctx.ident.real_name,
                                        ctx.ident.character_name) if n)
                # A former outsider goes through the same generator and screens. Its
                # old persona is seed data in the user context, never new instructions.
                from .tiers import is_cast
                from .io import canonical_json
                promoting = not is_cast(ctx.store.conn,guid)
                try:
                    seed = load_persona(ctx.store,guid,include_world=True) if promoting else None
                except ValueError:
                    seed = None  # an unreadable old seed cannot strand a new cast member
                seed_version = ctx.store.persona_versions(tier=None).get(guid)
                summary = cast_summary(existing,human=ctx.ident)
                if seed is not None:
                    summary += "\nPrevious world persona for this newcomer (seed data; adapt to the cast):\n" + canonical_json(seed)
                p = await generate_persona(ctx.reasoner, ctx.settings.reflection_model, slot,
                    member, summary, rules, human=ctx.ident,
                    taken_names=frozenset(taken),
                    member_names=frozenset(m.name for m in roster if m.guid != ctx.human),
                    private_logs=False)
                p = replace(p, real_life=replace(p.real_life, birthday=draw_birthday(
                    (q.real_life.birthday for q in existing), ctx.rng(STAGE, guid, "birthday"))))
            except Exception:
                # CancelledError is a BaseException: a login/cancellation consumes no
                # failed night, and the same selected unit may resume later tonight.
                _failed_night(ctx, guid)
                continue

            saved = False

            def write(store):
                nonlocal saved
                try:
                    # A manual writer or cast removal while the request awaited wins.
                    current = store.cast(include_world=True).get(guid)
                    if (current is None or current.level < CAST_MIN_LEVEL or guid == ctx.human
                            or guid in store.persona_versions()):
                        return
                    if promoting:
                        if store.persona_versions(tier=None).get(guid) != seed_version:
                            return  # another writer owns the changed persona
                        store.conn.execute("UPDATE persona_tier SET tier='cast' WHERE guid=?",(guid,))
                        store.conn.execute("UPDATE world_member SET retired=1 WHERE guid=?",(guid,))
                    save_persona(store, p, source="generated", ts=ctx.clock.now(),
                                 race=RACES.get(current.race, ""),
                                 class_name=CLASSES.get(current.class_, ""))
                    seed_relationships(store, [p], names, ctx.human, ctx.clock.now(), human=ctx.ident)
                    saved = True
                except Exception:
                    raise NewcomerRefused("persist_failed") from None
            if ctx.commit(STAGE, unit, write):
                if saved:
                    ctx.persona_added(guid)
                    log("newcomer_created", guid=guid)
            else:
                # A bad persistence hook must not regenerate indefinitely on every resume.
                # The failed write has rolled back; charge this night if the store can write.
                _failed_night(ctx, guid, reported=True)
        log("newcomers_finished", selected=len(selected))
    return stage_newcomers


register_counter(STAGE, count)
