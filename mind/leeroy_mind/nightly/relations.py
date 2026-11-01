"""Relationships at night (P1c Task 9): the cap every stage's change goes through, the day's
pending nudges, co-presence (tier 0), slow regression, and the notes and pins live talk shows.

- **The night cap** (memory.relationship_speed, 10): the net change of one pair's dimension on one
  night is at most `speed` either way, whichever stage makes it: P1b's live nudges and Task 8's
  trust, co-presence, reflection, encounters, gossip, sides in a feud and confessions (Tasks 11
  and 15-17). rel_cap keeps what each (night, a, b, dim) has used; a change and its use of the
  cap commit together, in the unit's transaction. Attraction is outside it: it moves only by the
  owner's curve (Task 12), so the nudges leave its rows pending for that stage. The human is never
  simulated, so nothing here moves his own view of anyone.
- **One helper** (_apply) holds the guards, the score's range and the cap, for apply_capped and
  stage_nudges alike (pre-flight PR27). What it applies is what the stored score really moves: a
  score at its limit takes no change and uses none of the cap.
- **Co-presence** reads the window's raid fights (is_raid_fight: 5-man bosses are routine under
  mod-dungeon-clear and have no memories, ruling of P1b Task 9) and battlegrounds among the cast.
- **Regression** runs once an ISO week over the weeks that really passed since the last one, so
  days with the PC off count (Review Focus 3). It never touches attraction or the romance stage:
  time alone never cools a woman toward him, and nothing lowers a stage.
- **The notes** are rewritten for the pairs that moved most tonight, from open memories only:
  P1b shows a relationship's notes in every prompt where its other end is in the talk, so they
  may hold nothing private or secret (controller ruling N4), and the pins are open memories too.
  A note or label about a member passes P1b's relationship-note rule, as P1b's own seeded notes
  did: romance is only ever aimed at him, never between members. One about him holds P1b's
  romance terms only when its writer is a woman with an attitude toward him."""
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import combinations
from typing import TYPE_CHECKING

from leeroy_mind.human import HumanIdentity
from leeroy_mind.logs import log
from leeroy_mind.nightly.evidence import Evidence, Facts, load_facts, load_items
from leeroy_mind.nightly.ground import REL_DELTA_DIMS, Claim, Verdict, check, ground, log_verdicts
from leeroy_mind.nightly.llm import schema, system_prompt
from leeroy_mind.nightly.runner import ATTITUDE_WORDS
from leeroy_mind.nightly.settings import iso_week
from leeroy_mind.nightly.store import step_finished_at
from leeroy_mind.personas.screens import ROMANCE_RE, check_romance, redact_real_name
from leeroy_mind.storage.rows import REL_RANGES, Relationship
from leeroy_mind.storage.store import Store, clean
from leeroy_mind.talk.prompt import LABEL_TEXT
from leeroy_mind.text import strip_wow_markup
from leeroy_mind.wow import is_raid_fight

if TYPE_CHECKING:
    from leeroy_mind.nightly.runner import NightContext

STAGE_NUDGES, STAGE_TIER0, STAGE_REGRESS, STAGE_NOTES = "nudges", "tier0", "regress", "notes"
ALL = "all"  # the one unit of a stage that runs as a single transaction a night
WEEK_S = 7 * 86400.0
MAX_WEEKS = 8.0             # regression counts at most this many weeks at once
AFFINITY_KEPT = 0.99        # of affinity, per week of regression
FAMILIARITY_FLOOR = 10.0    # regression takes familiarity no lower, and never raises it to this
UNSEEN_S = WEEK_S           # familiarity regresses for a pair not seen this long
MIN_CHANGE = 0.01           # a regression smaller than this is neither made nor logged
NOTES_EVIDENCE = 12         # open memories a notes rewrite reads
PINS = 5                    # open memories pinned to a relationship
NOTES_MOVED = 2.0           # the summed |used| tonight that earns a pair a notes rewrite
ROMANCE_BETWEEN_MEMBERS = "romance between members"  # Task 6's reason for the same invariant
ROMANCE_WITHOUT_ATTITUDE = "romance without an attitude"  # toward him from a man, or stance none

_OPEN_WITH = ("SELECT id FROM memory WHERE owner = ? AND status = 'active' AND scope = 'open' "
              "AND id IN (SELECT memory_id FROM memory_actor WHERE guid = ?) "
              "ORDER BY {order} LIMIT ?")
_NEWEST_OPEN = _OPEN_WITH.format(order="t_created DESC, id DESC")
_WEIGHTIEST_OPEN = _OPEN_WITH.format(order="importance DESC, t_created DESC, id DESC")

NOTES_SCHEMA = schema("notes", {"label": {"type": "string", "maxLength": 40},
                                "notes": {"type": "string", "maxLength": 400},
                                "evidence": {"type": "array", "items": {"type": "integer"}}})
NOTES_PROMPT = ("You keep {name}'s notes on {other}: a word or two for what {other} is to {name}, "
                "and a line or two of what {name} knows and thinks of {other}, in {name}'s own "
                "words.")
NOTES_ASK = (
    "What {name} remembers involving {other}, newest first, one numbered memory each:\n"
    "{memories}\n\n"
    "{name}'s notes on {other} so far: {old}\n{feeling}"
    "Rewrite the notes from these memories alone.\n"
    "label: what {other} is to {name}, in a word or two, such as {labels}.\n"
    "notes: one or two short sentences in {name}'s own words, naming only people these memories "
    "involve.\n"
    "evidence: the numbers of the memories the notes rest on.")
LABEL_WORDS = " or ".join(f'"{w}"' for w in LABEL_TEXT)  # the labels P1b's card reads


# -- the cap -------------------------------------------------------------------------------------

def capped(store: Store, night: str, a: int, b: int, dim: str, delta: float, speed: float) -> float:
    """What the night cap lets through of `delta` for (night, a, b, dim), recorded as used:
    clamp(delta, -speed - used, speed - used), neither bound past 0, so a speed the owner lowered
    mid-night never turns a change around. Call it inside the unit's transaction (it joins it), so
    the cap is used only when the change commits."""
    with store.transaction():
        row = store.conn.execute("SELECT used FROM rel_cap WHERE night = ? AND a = ? AND b = ? "
                                 "AND dim = ?", (night, a, b, dim)).fetchone()
        used = row["used"] if row else 0.0
        amount = float(min(max(delta, min(0.0, -speed - used)), max(0.0, speed - used)))
        if amount:
            store.conn.execute(
                "INSERT INTO rel_cap(night, a, b, dim, used) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(night, a, b, dim) DO UPDATE SET used = excluded.used",
                (night, a, b, dim, used + amount))
    return amount


def _apply(store: Store, *, night: str, a: int, b: int, dim: str, delta: float, speed: float,
           human: int) -> tuple[float, str]:
    """Apply to a's view of b what the night may of `delta`: the guards, then the score's range
    (REL_RANGES, which put_relationship would clamp to), then the night cap. Returns the amount
    applied and "", or 0.0 and why nothing was. It writes no rel_delta: apply_capped logs a new
    one, stage_nudges marks the pending one."""
    if a == human:
        return 0.0, "from the human"
    if a == b:
        return 0.0, "toward oneself"
    if dim not in REL_DELTA_DIMS:
        return 0.0, f"{dim} is not a night dimension"
    with store.transaction():
        r = store.get_relationship(a, b)
        score = getattr(r, dim)
        low, high = REL_RANGES[dim]
        room = min(max(delta, low - score), high - score)
        amount = capped(store, night, a, b, dim, room, speed)
        if not amount:
            return 0.0, ("no change" if not delta else "already at its limit" if not room
                         else "the night's cap is used up")
        store.put_relationship(replace(r, **{dim: score + amount}))
    return amount, ""


def apply_capped(store: Store, *, night: str, a: int, b: int, dim: str, delta: float,
                 speed: float, reason: str, evidence, ts: float, human: int) -> float:
    """Move a's view of b on `dim` by `delta` under the night cap and log it as an applied
    rel_delta with `reason` and `evidence`; returns the amount applied. Nothing is written and
    0.0 returned when a is the human or b, when `dim` is not in REL_DELTA_DIMS (attraction moves
    only by the owner's curve, Task 12, outside the cap) or when nothing is left to apply. Call it
    inside the unit's transaction."""
    with store.transaction():
        amount, _ = _apply(store, night=night, a=a, b=b, dim=dim, delta=delta, speed=speed,
                           human=human)
        if amount:
            store.add_rel_delta(a, b, dim, amount, reason, evidence, ts, applied=True)
    return amount


async def stage_nudges(ctx: "NightContext") -> None:
    """Stage "nudges", unit "all", no model: every pending rel_delta (P1b's live nudges, Task 8's
    trust), oldest first, through tonight's cap with apply_capped's guards (_apply), then marked
    applied with the amount applied. A cut one's reason gains " (capped from {x})"; one that
    applied nothing gets delta 0 and " (not applied: {why})". Attraction rows stay pending for
    Task 12's curve."""
    if ctx.done(STAGE_NUDGES, ALL):
        return
    speed, counts = ctx.settings.relationship_speed, Counter()

    def write(s: Store) -> None:
        for row in s.pending_rel_deltas():
            if row["dim"] == "attraction":
                counts["left"] += 1
                continue
            delta = row["delta"]
            amount, why = _apply(s, night=ctx.night, a=row["a"], b=row["b"], dim=row["dim"],
                                 delta=delta, speed=speed, human=ctx.human)
            if why:
                reason, kind = f"{row['reason']} (not applied: {why})", "not_applied"
            elif amount != delta:
                reason, kind = f"{row['reason']} (capped from {delta:g})", "capped"
            else:
                reason, kind = row["reason"], "applied"
            counts[kind] += 1
            s.conn.execute("UPDATE rel_delta SET applied = 1, delta = ?, reason = ? WHERE id = ?",
                           (amount, clean(reason), row["id"]))
    if ctx.commit(STAGE_NUDGES, ALL, write):
        log("nightly_nudges", night=ctx.night,
            **{k: counts[k] for k in ("applied", "capped", "not_applied", "left")})


# -- co-presence ---------------------------------------------------------------------------------

@dataclass
class Copresence:
    """What two members did together in the window: raid fights (kills and wipes), kills,
    battlegrounds, and the game_event refs, in time order."""
    boss_events: int = 0
    kills: int = 0
    bgs: int = 0
    refs: list[str] = field(default_factory=list)


def copresence(store: Store, window: tuple[float, float],
               cast: set[int]) -> dict[tuple[int, int], Copresence]:
    """Each pair of `cast` guids (a < b) who were together in the window: game_event rows of type
    boss, with state killed or wiped on a raid fight (is_raid_fight), and bg_end, by their
    members. Guids outside `cast` are ignored."""
    start, end = window
    with store.transaction():
        events = store.conn.execute(
            "SELECT id, type, payload FROM game_event WHERE type IN ('boss', 'bg_end') "
            "AND ts >= ? AND ts < ? ORDER BY ts, id", (start, end)).fetchall()
    out: dict[tuple[int, int], Copresence] = {}
    for e in events:
        p = json.loads(e["payload"])
        boss = e["type"] == "boss"
        if boss and (p.get("state") not in ("killed", "wiped")
                     or not is_raid_fight(p.get("map"), p.get("boss_id"))):
            continue
        for pair in combinations(sorted({g for g in p.get("members") or () if g in cast}), 2):
            c = out.setdefault(pair, Copresence())
            if boss:
                c.boss_events += 1
                c.kills += int(p["state"] == "killed")
            else:
                c.bgs += 1
            c.refs.append(f"event:{e['id']}")
    return out


def _gains(c: Copresence) -> dict[str, int]:
    """Familiarity +1 for a raid fight together and +1 more from the third, +1 for a
    battleground; respect +1 for a kill."""
    return {"familiarity": (c.boss_events >= 1) + (c.boss_events >= 3) + (c.bgs >= 1),
            "respect": int(c.kills >= 1)}


async def stage_tier0(ctx: "NightContext") -> None:
    """Stage "tier0", unit "all", no model: the window's co-presence among the cast (away members
    too: the events are real), in both directions, one apply_capped (one rel_delta) per direction
    and dimension with its summed gains (_gains), reason "copresence" and the pair's first three
    refs as evidence."""
    if ctx.done(STAGE_TIER0, ALL):
        return
    pairs = copresence(ctx.store, ctx.window, set(ctx.members(include_away=True)))
    speed, now, rows = ctx.settings.relationship_speed, ctx.clock.now(), 0

    def write(s: Store) -> None:
        nonlocal rows
        for (x, y), c in sorted(pairs.items()):
            gains = {dim: d for dim, d in _gains(c).items() if d}
            for a, b in ((x, y), (y, x)):
                for dim, d in gains.items():
                    rows += bool(apply_capped(s, night=ctx.night, a=a, b=b, dim=dim, delta=d,
                                              speed=speed, reason="copresence",
                                              evidence=c.refs[:3], ts=now, human=ctx.human))
    if ctx.commit(STAGE_TIER0, ALL, write):
        log("nightly_tier0", night=ctx.night, pairs=len(pairs), rows=rows)


# -- regression ----------------------------------------------------------------------------------

def regress(store: Store, *, weeks: float, now: float, human: int) -> int:
    """Slow regression over `weeks` for every relationship whose holder is not the human: affinity
    *= AFFINITY_KEPT ** weeks, and for a pair not seen in UNSEEN_S, familiarity drops by
    round(weeks), never below FAMILIARITY_FLOOR and never raising one already under it. A change
    of at least MIN_CHANGE is made and logged as an applied rel_delta (reason "regression",
    evidence ["rule:regression"]); a smaller one is neither, so every stored change has its row.
    The night cap does not apply. Attraction and the romance stage are never touched (Task 12's
    curve decays attraction, and only on nights she was ignored or hurt). Returns how many
    relationships changed."""
    kept, drop, unseen = AFFINITY_KEPT ** weeks, round(weeks), now - UNSEEN_S
    changed = 0
    with store.transaction():
        pairs = store.conn.execute("SELECT a, b FROM relationship WHERE a != ? ORDER BY a, b",
                                   (human,)).fetchall()
        for a, b in pairs:
            r = store.get_relationship(a, b)
            familiarity = r.familiarity
            if r.last_seen < unseen:
                familiarity = max(familiarity - drop, min(familiarity, FAMILIARITY_FLOOR))
            moved = {dim: v for dim, v in (("affinity", r.affinity * kept),
                                           ("familiarity", familiarity))
                     if abs(v - getattr(r, dim)) >= MIN_CHANGE}
            if not moved:
                continue
            store.put_relationship(replace(r, **moved))
            for dim, v in moved.items():
                store.add_rel_delta(a, b, dim, v - getattr(r, dim), "regression",
                                    ["rule:regression"], now, applied=True)
            changed += 1
    return changed


def _last_regression(store: Store, week: str) -> float | None:
    """When regression last finished for an ISO week before `week`: the units sort as the weeks
    do ("2026-W09" < "2026-W10" < "2027-W01")."""
    with store.transaction():
        return store.conn.execute("SELECT max(finished) AS t FROM night_step WHERE stage = ? "
                                  "AND unit < ?", (STAGE_REGRESS, week)).fetchone()["t"]


async def stage_regress(ctx: "NightContext") -> None:
    """Stage "regress", unit iso_week(night), no model: once an ISO week, on the first night of it
    that runs (step_finished_at, any night), regress over the weeks since the last regression of
    an earlier week: 1.0 the first time, at most MAX_WEEKS and never below 0, so days and weeks
    with the PC off count in full (Review Focus 3)."""
    week = iso_week(ctx.night)
    if step_finished_at(ctx.store, STAGE_REGRESS, week) is not None:
        return
    now = ctx.clock.now()
    last = _last_regression(ctx.store, week)
    weeks = 1.0 if last is None else min(MAX_WEEKS, max(0.0, (now - last) / WEEK_S))
    changed = 0

    def write(s: Store) -> None:
        nonlocal changed
        changed = regress(s, weeks=weeks, now=now, human=ctx.human)
    if ctx.commit(STAGE_REGRESS, week, write):
        log("nightly_regress", night=ctx.night, week=week, weeks=round(weeks, 3), rows=changed)


# -- notes and pins ------------------------------------------------------------------------------

def _open_ids(store: Store, sql: str, owner: int, about: int, limit: int) -> list[int]:
    with store.transaction():
        return [r["id"] for r in store.conn.execute(sql, (owner, about, limit))]


def _text(s: str) -> str:
    return " ".join(strip_wow_markup(s).split())


def _old_notes(r: Relationship, ident: HumanIdentity) -> str:
    """The stored label and notes as the prompt shows them, his real name as what the guild calls
    him (P1b's matcher), or "none yet"."""
    shown = [f'{key} "{redact_real_name(_text(value), ident, ident.called)}"'
             for key, value in (("label", r.label), ("notes", r.notes)) if value.strip()]
    return ", ".join(shown) or "none yet"


def _romance_at_member(text: str, member: str, ident: HumanIdentity, facts: Facts) -> bool:
    """P1b's relationship-note rule refuses `text` as a note about the member `member`: there a
    romance term is allowed only when the text names him and no member is its object (the
    relationships branch of check_romance, which P1b's seeded notes passed). Task 6's rule 7
    needs a member's name in the text, and a note about her rarely names her ("I have a crush on
    her"; review-task9 I1)."""
    try:
        check_romance({"relationships": [{"other": member, "note": text}]}, ident, (),
                      characters=facts.member_names)
    except ValueError:
        return True
    return False


def screen_claim(claim: Claim, ev: Evidence, facts: Facts, ident: HumanIdentity, *,
                 member: str | None, attitude: bool) -> Verdict:
    """Task 6's rules (check), then who may write romance into the text. About a member
    (`member` is her name): P1b's relationship-note rule, so romance is only ever aimed at him,
    never between members. About anyone else, him included: P1b's romance terms (ROMANCE_RE,
    check_romance's own) only from a woman with an attitude toward him (`attitude`), since men
    stay friends and a stance of none is no attitude (review-task9 Minor 3). Task 11's
    reflection screens its outputs with it too."""
    verdict = check(claim, ev, facts)
    if not verdict.ok:
        return verdict
    if member is not None:
        if _romance_at_member(claim.text, member, ident, facts):
            return Verdict(False, "deterministic", ROMANCE_BETWEEN_MEMBERS)
    elif not attitude and ROMANCE_RE.search(claim.text):
        return Verdict(False, "deterministic", ROMANCE_WITHOUT_ATTITUDE)
    return verdict


async def rewrite_notes(ctx: "NightContext", a: int, b: int, *, facts: Facts | None = None) -> None:
    """Unit f"{a}:{b}" of stage "notes": a's notes on b, from a's NOTES_EVIDENCE newest active
    open memories that involve b (private and secret ones never, ruling N4), shown with the old
    notes; when b is the human he is named by what the guild calls him, with a's attitude toward
    him in generic words (ATTITUDE_WORDS). One JSON call (NOTES_SCHEMA). The notes are grounded
    as kind "notes" about b; the label, stored with them, must pass the same rules (check), and
    both must pass the romance rules (screen_claim): about a member P1b's relationship-note rule, or
    the rewrite is refused as "romance between members"; about him no romance term unless a is a
    woman with an attitude, or it is refused as "romance without an attitude". The commit sets
    the label and notes only when the claim is accepted, and the pins always: the ids of a's PINS
    most important active open memories that involve b. With no open memory to write from,
    nothing is asked. A view held by the human, or of oneself, is never written: the unit is
    skipped. `facts` is the night's Facts, loaded here when not given."""
    unit = f"{a}:{b}"
    if ctx.done(STAGE_NOTES, unit):
        return
    if a == ctx.human or a == b:
        ctx.skip(STAGE_NOTES, unit)
        return
    store = ctx.store
    memories = store.get_memories(_open_ids(store, _NEWEST_OPEN, a, b, NOTES_EVIDENCE))
    label = notes = ""
    pairs: list[tuple[Claim, Verdict]] = []
    if memories:
        ev = Evidence()
        for item in load_items(store, [f"memory:{m.id}" for m in memories], ident=ctx.ident):
            ev.add(item)
        name = ctx.name(a)
        other = ctx.ident.called if b == ctx.human else ctx.name(b)
        stance = ctx.attitudes().get(a) if b == ctx.human else None
        feeling = ATTITUDE_WORDS[stance].format(name=name, human=other) + "\n" if stance else ""
        facts = facts if facts is not None else load_facts(ctx)
        system = system_prompt(STAGE_NOTES, NOTES_PROMPT, ctx.settings.data_dir, human=ctx.ident,
                               name=name, other=other)
        user = NOTES_ASK.format(name=name, other=other, memories=ev.render(ctx.tz),
                                old=_old_notes(store.get_relationship(a, b), ctx.ident),
                                feeling=feeling, labels=LABEL_WORDS)
        reply = await ctx.reasoner.json(system, user, NOTES_SCHEMA)
        label, notes = _text(reply["label"]), _text(reply["notes"])
        claim = Claim(kind="notes", owner=a, text=notes, about=b,
                      evidence=[ev.ref(i) for i in dict.fromkeys(reply["evidence"])])
        rules = {"member": None if b == ctx.human else other, "attitude": stance is not None}
        verdict = screen_claim(claim, ev, facts, ctx.ident, **rules)
        if verdict.ok and label:
            said = screen_claim(replace(claim, text=label), ev, facts, ctx.ident, **rules)
            if not said.ok:
                verdict = Verdict(False, said.check, f"label: {said.reason}")
        if verdict.ok:
            ((_, verdict),) = await ground(ctx, [claim], ev, facts)
        pairs = [(claim, verdict)]
    accepted = bool(pairs) and pairs[0][1].ok
    now = ctx.clock.now()

    def write(s: Store) -> None:
        r = s.get_relationship(a, b)
        new = replace(r, pinned=tuple(_open_ids(s, _WEIGHTIEST_OPEN, a, b, PINS)))
        if accepted:
            new = replace(new, label=label, notes=notes)
        if new != r:
            s.put_relationship(new)
        log_verdicts(s, ctx.night, STAGE_NOTES, pairs, now, ctx.ident)
    if ctx.commit(STAGE_NOTES, unit, write):
        log("nightly_notes", night=ctx.night, a=a, b=b, asked=bool(pairs), accepted=accepted)


def _notes_pairs(ctx: "NightContext") -> list[tuple[int, int]]:
    """Tonight's pairs whose summed |used| in rel_cap is at least NOTES_MOVED, the holder never
    the human: the most moved first (ties by guid), the holder in the cast (away members too) and
    the other end in it or the human, at most settings.notes_per_night."""
    with ctx.store.transaction():
        rows = ctx.store.conn.execute(
            "SELECT a, b, sum(abs(used)) AS moved FROM rel_cap WHERE night = ? AND a != ? "
            "GROUP BY a, b HAVING moved >= ? ORDER BY moved DESC, a, b",
            (ctx.night, ctx.human, NOTES_MOVED)).fetchall()
    cast = set(ctx.members(include_away=True))
    pairs = [(r["a"], r["b"]) for r in rows
             if r["a"] in cast and (r["b"] in cast or r["b"] == ctx.human)]
    return pairs[:max(0, ctx.settings.notes_per_night)]


async def stage_notes(ctx: "NightContext") -> None:
    """Stage "notes" (the reflection model), units f"{a}:{b}": rewrite_notes for the pairs that
    moved most tonight (_notes_pairs), at most settings.parallel at once. It runs after the
    attraction stage, so the attitudes are read fresh."""
    pairs = {f"{a}:{b}": (a, b) for a, b in _notes_pairs(ctx)}
    if not pairs:
        return
    ctx.attitudes(fresh=True)
    facts = load_facts(ctx)
    await ctx.map_units(STAGE_NOTES, list(pairs),
                        lambda unit: rewrite_notes(ctx, *pairs[unit], facts=facts))
