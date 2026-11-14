"""The weekly self-improvement cycle: held-out gates, promotion, retirement and rollback.

It reads numbers and ids only and writes no text: the report and every learning-state
version hold arm ids, lesson ids, member guids, context keys, counts, rates and codes.
It never changes reward weights, guardrails, personas or talk history, and it never changes
how the bandit samples: a promotion is a status the report, the Lab and the coach read, and
it keeps an arm from share retirement and puts a lesson first in the style block. Only the
owner's rollback (CLI or Lab) restores weights, and only when he asks for it.
"""
from datetime import date, timedelta
import json
import math
import sqlite3
from types import SimpleNamespace

from leeroy_mind.discord.channels import is_discord
from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.settings import iso_week, week_bounds
from . import arms, bandit, lessons, weights
from .config import load_learn_config
from .control import REPORT_KEY, effective, vetoed
from .reward import DAY, decayed

STAGE = 'learn_cycle'
REPORT_VERSION = 1
PROBABILITY = .9  # posterior probability of beating control, for a promotion
LOSING = .5  # an arm retired for its share must also be under this probability
USES = 30  # rewarded uses before promotion or share retirement, and new uses after a rollback
SHARE = .05  # an arm under this weekly share for two weeks may be retired
HOLD_DAYS = 14  # a diversity rollback waits at least this long before a new promotion
KEEP_VERSIONS = 52
LIST_CAP = 100
REPORT_BYTES = 60000  # under the Lab's 64 KiB read
CONTEXT_CAP = 200
GRID = 4000
DETECTORS = ('repetition', 'stock', 'nonsequitur', 'unawareness')
DIVERSITY = ('repetition', 'stock')
MEDIUMS = ('game', 'discord', 'dm')
RISE_P = .05  # one-sided two-proportion test
RISE_POINTS = .005  # at least half a percentage point
RISE_LINES = 10  # and at least ten more flagged lines
MIN_CARRIER = 10  # lines before a promotion can be blamed for a rise
TRIPS = frozenset({'rewrite', 'react', 'drop'})
SCREENED = TRIPS | {'send'}
REPETITION_RULES = frozenset({'member_across_days', 'guild_evening_phrase'})
STOCK_RULES = frozenset({'stock_opening', 'stock_closing'})
EPSILON = 1e-12


class _Frozen(Exception):
    """Learning froze between the cycle's read and its write: no receipt, retried later."""


def have(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return ({'learn_version', 'learn_promotion', 'learn_decision', 'learn_hold', 'meta', 'night_step',
             'bandit_credit', 'bandit_rejection'} <= names
            and weights.available(conn) and arms.available(conn) and lessons.available(conn))


def due_week(night):
    """The ISO week that ended on the Sunday before this night; none on a Sunday night,
    which is the weekly evaluation's own night. Monday runs it; later nights retry."""
    day = date.fromisoformat(night)
    if day.isoweekday() == 7:
        return None
    return iso_week((day - timedelta(days=day.isoweekday())).isoformat())


def previous_week(week):
    year, number = week.split('-W')
    monday = date.fromisocalendar(int(year), int(number), 1)
    return iso_week((monday - timedelta(days=7)).isoformat())


def done(conn, week):
    return conn.execute('SELECT 1 FROM night_step WHERE stage=? AND unit=? LIMIT 1',
                        (STAGE, week)).fetchone() is not None


def medium(channel):
    """The diversity floor's three mediums: Discord DMs, other Discord rooms (group DMs
    included) and the game (whispers, officer and voice included)."""
    return 'dm' if channel.startswith('dm:') else 'discord' if is_discord(channel) else 'game'


# -- the posterior gate ---------------------------------------------------------------------------

def beats(a, b, *, grid=GRID):
    """P(X > Y) for X ~ Beta(*a) and Y ~ Beta(*b), by deterministic midpoint quadrature."""
    xs = [(i + .5) / grid for i in range(grid)]

    def density(alpha, beta):
        logs = [(alpha - 1) * math.log(x) + (beta - 1) * math.log1p(-x) for x in xs]
        top = max(logs)
        values = [math.exp(v - top) for v in logs]
        total = sum(values)
        return [v / total for v in values]
    px, py = density(*a), density(*b)
    below = result = 0.
    for p, q in zip(px, py):
        result += p * (below + q / 2)
        below += q
    return min(1., max(0., result))


def _mass(rows, now):
    """Beta(1,1) plus each line's whole, decayed reward, mapped as the bandit maps it but never
    divided among the families the line carried."""
    alpha = beta = 1.
    for row in rows:
        weight = row.cap if row.value > 0 else 1.
        alpha += decayed(weight * (1 + row.value) / 2, age_seconds=max(0., now - row.ts))
        beta += decayed(weight * (1 - row.value) / 2, age_seconds=max(0., now - row.ts))
    return alpha, beta


def significant_rise(before, before_lines, after, after_lines):
    """A rate rose only when it is a real rise: at least RISE_LINES more flagged lines, at
    least RISE_POINTS higher, and a one-sided two-proportion test under RISE_P."""
    if not before_lines or not after_lines or after - before < RISE_LINES:
        return False
    p1, p2 = before / before_lines, after / after_lines
    if p2 - p1 < RISE_POINTS - EPSILON:
        return False
    pooled = (before + after) / (before_lines + after_lines)
    spread = math.sqrt(pooled * (1 - pooled) * (1 / before_lines + 1 / after_lines))
    return spread > 0 and .5 * math.erfc((p2 - p1) / spread / math.sqrt(2)) < RISE_P


# -- the held-out evaluation ----------------------------------------------------------------------

class Tally:
    """Lines and detector hits; guard trips per generation, never per fragment."""
    __slots__ = ('lines', 'any', 'hits', 'screened', 'trips')

    def __init__(self):
        self.lines = self.any = 0
        self.hits = dict.fromkeys(DETECTORS, 0)
        self.screened, self.trips = set(), set()

    def add(self, kinds, anchor=None, verdict=None):
        self.lines += 1
        self.any += bool(kinds)
        for kind in kinds:
            self.hits[kind] += 1
        if anchor is not None and verdict in SCREENED:
            self.screened.add(anchor)
            if verdict in TRIPS:
                self.trips.add(anchor)

    def rate(self, kind=None):
        hits = self.any if kind is None else self.hits[kind]
        return hits / self.lines if self.lines else 0.

    def guard(self):
        return len(self.trips) / len(self.screened) if self.screened else 0.

    def summary(self):
        return dict(lines=self.lines, flagged=round(self.rate(), 6),
                    **{k: round(self.rate(k), 6) for k in DETECTORS},
                    guard=round(self.guard(), 6), guard_screened=len(self.screened))


def _ids(encoded, kind):
    try:
        values = json.loads(encoded)
    except (TypeError, ValueError):
        return ()
    return tuple(v for v in values if type(v) is kind) if type(values) is list else ()


def measure(conn, since, until, owner):
    """Task 12's detectors, lexical and model-free, over [since, until), with each line's recipe.

    Every medium counts (game, whispers, officer, voice, Discord channels, DMs and group DMs):
    only counts leave this function, so no line or audience is exposed by the result.
    Contradiction needs a model and is never measured here; non sequiturs are only the filler
    rule (relevance needs embeddings); unawareness checks only full-sentence claims against
    verified kills, raids and places.
    """
    from leeroy_mind.eval.talk import nonsequitur, repetition, unawareness
    from leeroy_mind.eval.talk.common import mark_scratch, read_lines
    lines, context = read_lines(conn, since, until, owner)
    lines = [line for line in lines if line.ts < until]
    kinds = {line.id: set() for line in lines}
    scratch = mark_scratch(SimpleNamespace(conn=sqlite3.connect(':memory:')))
    try:
        flags, _ = repetition.detect(scratch, lines, {})
    finally:
        scratch.conn.close()
    for flag in flags:
        if flag.rule in REPETITION_RULES:
            kinds[flag.line_id].add('repetition')
        elif flag.rule in STOCK_RULES:
            kinds[flag.line_id].add('stock')
    for flag in nonsequitur.detect(lines, context, {})[0]:
        kinds[flag.line_id].add('nonsequitur')
    measured = dict(repetition='measured', stock='measured', nonsequitur='partial', unawareness='partial',
                    contradiction='not_measured', guard='measured')
    try:
        for flag in unawareness.detect(conn, lines)[0]:
            kinds[flag.line_id].add('unawareness')
    except sqlite3.OperationalError:
        measured['unawareness'] = 'not_measured'
    recipes = {}
    for lid, anchor, arm_ids, lesson_ids, verdict, key in conn.execute(
            "SELECT c.id,coalesce(m.recipe_line_id,c.id),p.arm_ids,p.lesson_ids,p.guard_verdict,p.bandit_context "
            "FROM chat_line c LEFT JOIN talk_recipe_line m ON m.line_id=c.id "
            "JOIN talk_recipe p ON p.line_id=coalesce(m.recipe_line_id,c.id) "
            "WHERE c.origin='mind' AND c.ts>=? AND c.ts<?", (since, until)):
        recipes[lid] = (anchor, _ids(arm_ids, str), _ids(lesson_ids, int), verdict, key)
    rows = []
    for line in lines:
        anchor, arm_ids, lesson_ids, verdict, key = recipes.get(line.id, (None, (), (), None, None))
        rows.append(SimpleNamespace(id=line.id, guid=line.guid, channel=line.channel, medium=medium(line.channel),
                                    kinds=kinds[line.id], anchor=anchor, arms=arm_ids, lessons=lesson_ids,
                                    verdict=verdict, key=key))
    return rows, measured


def _tally(rows, keep=lambda row: True):
    tally = Tally()
    for row in rows:
        if keep(row):
            tally.add(row.kinds, row.anchor, row.verdict)
    return tally


def rises(this, last):
    """(medium, metric) pairs whose repetition or stock rate rose significantly."""
    out = []
    for name in MEDIUMS:
        now_tally = _tally(this, lambda r: r.medium == name)
        then_tally = _tally(last, lambda r: r.medium == name)
        for metric in DIVERSITY:
            if significant_rise(then_tally.hits[metric], then_tally.lines, now_tally.hits[metric], now_tally.lines):
                out.append((name, metric))
    return out


def _shares(rows, registry, frozen_members, contexts):
    """Each arm's share of its family's generations that week, in the bandit contexts where
    it has been used (a length arm is never offered on Discord, so Discord does not dilute
    it), frozen members left out. None when the family had no generation there."""
    family_turns, arm_turns = {}, {}
    for anchor, (arm_ids, key) in {row.anchor: (row.arms, row.key) for row in rows
                                   if row.anchor is not None and row.arms and row.guid not in frozen_members}.items():
        for aid in arm_ids:
            arm = registry.get(aid)
            if arm is None:
                continue
            family_turns[arm.family, key] = family_turns.get((arm.family, key), 0) + 1
            arm_turns[aid, key] = arm_turns.get((aid, key), 0) + 1
    out = {}
    for aid, arm in registry.items():
        keys = contexts.get(aid, ())
        total = sum(family_turns.get((arm.family, key), 0) for key in keys)
        out[aid] = sum(arm_turns.get((aid, key), 0) for key in keys) / total if total else None
    return out


def _not_worse(carrier, comparison):
    return all(carrier.rate(k) <= comparison.rate(k) + EPSILON for k in DETECTORS)


def gate(*, uses, probability, carrier, comparison, guard_worse=False, vetoed=False, held=None, rose=()):
    """Why a candidate waits; an empty list promotes it. Every gate must pass:
    30 rewarded uses, a posterior probability above 0.9 of beating its comparison, last
    week's lines with no detector rate above the comparison's, no guard regression, no
    owner veto, no unmet hold from an earlier rollback, and no significant rise in
    repetition or stock phrases."""
    reasons = []
    if held:
        reasons.append(held)
    if vetoed:
        reasons.append('veto')
    if uses < USES:
        reasons.append('uses')
    if probability <= PROBABILITY:
        reasons.append('probability')
    if not carrier.lines:
        reasons.append('no_week_lines')
    elif not comparison.lines:
        reasons.append('no_control_lines')
    elif not _not_worse(carrier, comparison):
        reasons.append('detectors')
    if guard_worse or carrier.screened and comparison.screened and carrier.guard() > comparison.guard() + EPSILON:
        reasons.append('guard')
    if rose:
        reasons.append('diversity')
    return reasons


def held(hold, rows, now):
    """None when no hold applies, else its reason code. A diversity rollback waits two
    weeks and 30 new rewarded uses; an owner rollback waits for 30 new rewarded uses."""
    if hold is None:
        return None
    reason, since = hold
    code = 'rolled_back' if reason == 'diversity' else 'owner_rollback'
    if reason == 'diversity' and now - since < HOLD_DAYS * DAY:
        return code
    return code if sum(row.ts > since for row in rows) < USES else None


def _key(encoded):
    try:
        return json.loads(encoded) if bandit.valid_key(encoded) else None
    except (TypeError, ValueError):
        return None


def _promotions(conn):
    return {(r[0], r[1], r[2]): dict(status=r[3], week=r[4], promoted=r[5], changed=r[6])
            for r in conn.execute('SELECT kind,ident,context,status,week,promoted,changed FROM learn_promotion')}


def _holds(conn):
    return {(r[0], r[1], r[2]): (r[3], r[4])
            for r in conn.execute('SELECT kind,ident,context,reason,since FROM learn_hold')}


def _rewarded(conn, frozen_members):
    """Every rewarded generation with arms, its whole reward (never the bandit's split)."""
    return [SimpleNamespace(key=key, arms=_ids(arm_ids, str), value=value, cap=cap, ts=ts, guid=guid)
            for key, arm_ids, value, cap, ts, guid in conn.execute(
                "SELECT p.bandit_context,p.arm_ids,r.value,r.cap_weight,c.ts,r.guid FROM talk_reward r "
                "JOIN talk_recipe p ON p.line_id=r.line_id JOIN chat_line c ON c.id=r.line_id "
                "WHERE p.bandit_context IS NOT NULL AND p.arm_ids!='[]'")
            if guid not in frozen_members]


def _guard_history(conn, frozen_members):
    """Screened generations and trips per (arm, context), unsent rejections included."""
    out = {}
    for key, arm_ids, verdict, guid in conn.execute(
            "SELECT p.bandit_context,p.arm_ids,p.guard_verdict,c.from_guid FROM talk_recipe p "
            "JOIN chat_line c ON c.id=p.line_id WHERE p.bandit_context IS NOT NULL AND p.arm_ids!='[]'"):
        if guid in frozen_members or verdict not in SCREENED:
            continue
        for aid in _ids(arm_ids, str):
            item = out.setdefault((aid, key), [0, 0])
            item[0] += 1
            item[1] += verdict in TRIPS
    for aid, key, guid, count in conn.execute(
            'SELECT arm_id,context,guid,count(*) FROM bandit_rejection GROUP BY arm_id,context,guid'):
        if guid not in frozen_members:
            item = out.setdefault((aid, key), [0, 0])
            item[0] += count
            item[1] += count
    return out


def _worse(mine, theirs):
    """(screened, trips) pairs: any trip at a higher rate than the comparison's is worse."""
    return bool(mine[1]) and (not theirs[0] or mine[1] / mine[0] > theirs[1] / theirs[0] + EPSILON)


def _lesson_evidence(conn, lesson_id, guid):
    """The member's rewarded and screened generations with and without the lesson, in the
    rooms where it was used and since its first use."""
    rows = [SimpleNamespace(lessons=_ids(ids, int), verdict=verdict, channel=channel, ts=ts, value=value, cap=cap)
            for ids, verdict, channel, ts, value, cap in conn.execute(
                "SELECT p.lesson_ids,p.guard_verdict,c.channel,c.ts,r.value,r.cap_weight FROM talk_recipe p "
                "JOIN chat_line c ON c.id=p.line_id LEFT JOIN talk_reward r ON r.line_id=p.line_id "
                "WHERE c.from_guid=?", (guid,))]
    used = [r for r in rows if lesson_id in r.lessons]
    if not used:
        return None
    rooms, first = {r.channel for r in used}, min(r.ts for r in used)
    without = [r for r in rows if r.channel in rooms and r.ts >= first and lesson_id not in r.lessons]

    def screened(items):
        return (sum(r.verdict in SCREENED for r in items), sum(r.verdict in TRIPS for r in items))
    return ([r for r in used if r.value is not None], [r for r in without if r.value is not None],
            _worse(screened(used), screened(without)))


def evaluate(conn, *, week, cfg, now, tz, owner):
    """The cycle's plan from one read snapshot. Writes nothing; the stage applies it."""
    start, end = week_bounds(week, tz)
    last_week = previous_week(week)
    last_start, last_end = week_bounds(last_week, tz)
    this, measured = measure(conn, start, end, owner)
    last, _ = measure(conn, last_start, last_end, owner)
    overall, before = _tally(this), _tally(last)
    rose = rises(this, last)
    frozen_members = set(cfg.frozen_members)
    frozen_families = set(cfg.frozen_families)
    mine = [row for row in this if row.guid not in frozen_members]
    registry = {a.id: a for a in arms.read(conn)}
    controls = arms.controls()
    promotions, holds = _promotions(conn), _holds(conn)
    in_force = {k: v for k, v in promotions.items() if v['status'] == 'promoted'}
    plan = dict(week=week, previous_week=last_week, measured=measured, rose=rose,
                rates=dict(week=overall.summary(), previous=before.summary(),
                           mediums={m: dict(week=_tally(this, lambda r: r.medium == m).summary(),
                                            previous=_tally(last, lambda r: r.medium == m).summary())
                                    for m in MEDIUMS}),
                promote=[], held=[], retire_arms=[], lapsed=[], rollback=[], arms=[], lessons=[])

    # The bandit's own posteriors (split credit) are kept in the version for inspection only.
    posteriors = {}
    keys = sorted({r[0] for r in conn.execute('SELECT DISTINCT context FROM bandit_credit')})
    for key in keys[:CONTEXT_CAP]:
        posteriors[key] = bandit.posterior(conn, None, key, now=now)
    plan['posteriors'] = [dict(context=_key(key), arm_id=aid, **{k: round(v, 9) if type(v) is float else v
                                                                  for k, v in stats.items()})
                          for key, stats in posteriors.items() for aid, stats in sorted(stats.items())
                          if _key(key) is not None][:2000]

    # Arms: per bandit context, on each line's whole reward (frozen members left out).
    rewarded = _rewarded(conn, frozen_members)
    by_key = {}
    for row in rewarded:
        by_key.setdefault(row.key, []).append(row)
    history = _guard_history(conn, frozen_members)
    contexts = {}
    for row in rewarded:
        for aid in row.arms:
            contexts.setdefault(aid, set()).add(row.key)
    for aid in sorted(contexts):
        arm = registry.get(aid)
        if arm is None or arm.status != 'active' or arm.family in frozen_families:
            continue
        control = controls[arm.family]
        for key in sorted(contexts[aid]):
            if _key(key) is None or ('arm', aid, key) in in_force:
                continue
            used = [r for r in by_key[key] if aid in r.arms]
            base = [r for r in by_key[key] if control in r.arms]
            probability = beats(_mass(used, now), _mass(base, now))
            carrier = _tally(mine, lambda r: r.key == key and aid in r.arms)
            comparison = _tally(mine, lambda r: r.key == key and control in r.arms)
            reasons = gate(uses=len(used), probability=probability, carrier=carrier, comparison=comparison,
                           guard_worse=_worse(history.get((aid, key), (0, 0)), history.get((control, key), (0, 0))),
                           vetoed=vetoed(conn, 'arm', aid), rose=rose,
                           held=held(holds.get(('arm', aid, key)), used, now))
            entry = dict(kind='arm', id=aid, context=_key(key), probability=round(probability, 6),
                         uses=len(used), rate=round(carrier.rate(), 6), control_rate=round(comparison.rate(), 6))
            if reasons:
                plan['held'].append(dict(entry, reasons=reasons))
            else:
                plan['promote'].append(dict(entry, key=key))

    # Lessons: with-versus-without for the same member in the same rooms.
    for lid, guid in conn.execute(
            "SELECT id,guid FROM talk_lesson WHERE status='active' AND expires>? ORDER BY id", (now,)).fetchall():
        if guid in frozen_members or ('lesson', str(lid), '') in in_force:
            continue
        evidence = _lesson_evidence(conn, lid, guid)
        if evidence is None or not evidence[0]:
            continue
        used, without, guard_worse = evidence
        carrier = _tally(mine, lambda r: lid in r.lessons)
        rooms = {r.channel for r in mine if lid in r.lessons}
        comparison = _tally(mine, lambda r: r.guid == guid and r.channel in rooms and r.anchor is not None
                            and lid not in r.lessons)
        probability = beats(_mass(used, now), _mass(without, now))
        reasons = gate(uses=len(used), probability=probability, carrier=carrier, comparison=comparison,
                       guard_worse=guard_worse, vetoed=vetoed(conn, 'lesson', lid), rose=rose,
                       held=held(holds.get(('lesson', str(lid), '')), used, now))
        entry = dict(kind='lesson', id=lid, guid=guid, probability=round(probability, 6), uses=len(used),
                     rate=round(carrier.rate(), 6), control_rate=round(comparison.rate(), 6))
        (plan['held'] if reasons else plan['promote']).append(dict(entry, reasons=reasons) if reasons else entry)

    # Retirement: a measured loser the bandit has stopped choosing. Thirty rewarded uses, under
    # 0.5 probability of beating control on whole rewards, and under 5% of its family's
    # generations in both weeks; never a promoted arm, nor one an owner rollback restored
    # until it has 30 new uses. A lesson's own 14-day expiry retires it.
    shares, last_shares = (_shares(rows, registry, frozen_members, contexts) for rows in (this, last))
    promoted_ids = {ident for kind, ident, _ in in_force if kind == 'arm'}
    for aid, arm in sorted(registry.items()):
        if arm.status != 'active' or arm.family in frozen_families or aid in promoted_ids:
            continue
        share, previous = shares.get(aid), last_shares.get(aid)
        if share is None or previous is None or share >= SHARE or previous >= SHARE:
            continue
        used = [r for r in rewarded if aid in r.arms]
        if len(used) < USES or held(holds.get(('arm', aid, '')), used, now):
            continue
        rooms = {r.key for r in used}
        probability = beats(_mass(used, now), _mass([r for r in rewarded if r.key in rooms
                                                      and controls[arm.family] in r.arms], now))
        if probability < LOSING - 1e-9:  # a tie (quadrature gives 0.4999999999999993) is not a loser
            plan['retire_arms'].append(dict(id=aid, share=round(share, 6), share_previous=round(previous, 6),
                                            probability=round(probability, 6), uses=len(used)))
    # Lapsed during the evaluated week, so each lapse is reported by exactly one cycle.
    for lid, guid in conn.execute(
            "SELECT id,guid FROM talk_lesson WHERE status='active' AND expires>? AND expires<=? ORDER BY id",
            (start, min(end, now))):
        if guid not in frozen_members and not vetoed(conn, 'lesson', lid):
            plan['lapsed'].append(dict(id=lid, guid=guid))

    # The diversity floor: after a significant rise, roll back the most recent promotions whose
    # own lines, in the medium that rose, ran higher on what rose than their comparison lines.
    if rose:
        risen = {}
        for name, metric in rose:
            risen.setdefault(metric, set()).add(name)
        correlated = []
        for (kind, ident, key), item in in_force.items():
            if kind == 'arm':
                arm = registry.get(ident)
                if arm is None or arm.family in frozen_families:
                    continue
                carrier = [r for r in mine if r.key == key and ident in r.arms]
                comparison = [r for r in mine if r.key == key and controls[arm.family] in r.arms]
            else:
                row = conn.execute('SELECT guid FROM talk_lesson WHERE id=?', (int(ident),)).fetchone()
                if row is None or row[0] in frozen_members:
                    continue
                lid = int(ident)
                carrier = [r for r in mine if lid in r.lessons]
                rooms = {r.channel for r in carrier}
                comparison = [r for r in mine if r.guid == row[0] and r.channel in rooms
                              and r.anchor is not None and lid not in r.lessons]
            hit = []
            for metric, names in sorted(risen.items()):
                ours = _tally(carrier, lambda r: r.medium in names)
                theirs = _tally(comparison, lambda r: r.medium in names)
                if ours.lines >= MIN_CARRIER and ours.rate(metric) > theirs.rate(metric) + EPSILON:
                    hit.append(metric)
            if hit:
                correlated.append((item['promoted'], kind, ident, key, hit))
        if correlated:
            latest = max(c[0] for c in correlated)
            for promoted, kind, ident, key, hit in sorted(correlated, key=lambda c: (c[1], c[2], c[3])):
                if promoted == latest:
                    plan['rollback'].append(dict(kind=kind, id=ident, context=key, reasons=hit))

    # Numeric comparisons for the report, this week against last week.
    for aid, arm in sorted(registry.items()):
        if arm.status == 'control':
            continue
        now_tally = _tally(mine, lambda r: aid in r.arms)
        then_tally = _tally([r for r in last if r.guid not in frozen_members], lambda r: aid in r.arms)
        if now_tally.lines or then_tally.lines or arm.status == 'active':
            plan['arms'].append(dict(id=aid, family=arm.family, status=arm.status,
                                     week=now_tally.summary(), previous=then_tally.summary(),
                                     share=None if shares.get(aid) is None else round(shares[aid], 6),
                                     share_previous=None if last_shares.get(aid) is None
                                     else round(last_shares[aid], 6)))
    used_lessons = sorted({lid for r in [*this, *last] for lid in r.lessons})
    owners = dict(conn.execute('SELECT id,guid FROM talk_lesson WHERE id IN (SELECT value FROM json_each(?))',
                               (json.dumps(used_lessons),)))
    for lid in used_lessons:
        if owners.get(lid) in frozen_members:
            continue
        plan['lessons'].append(dict(id=lid, guid=owners.get(lid), week=_tally(this, lambda r: lid in r.lessons).summary(),
                                    previous=_tally(last, lambda r: lid in r.lessons).summary()))
    return plan


# -- learning-state versions ----------------------------------------------------------------------

def state(conn, *, now, posteriors=()):
    """The governed learning state, ids and numbers only. Lessons are those still live."""
    current = weights.read(conn, at=now, schema_available=True)
    return dict(
        version=1, at=now,
        arms=[dict(id=a.id, family=a.family, params=a.params, version=a.version, status=a.status,
                   origin=a.origin, created=a.created) for a in arms.read(conn)],
        lessons=[dict(zip(('id', 'guid', 'scope', 'status', 'reinforced', 'expires', 'origin_night'), r))
                 for r in conn.execute('SELECT id,guid,scope,status,reinforced,expires,origin_night '
                                       'FROM talk_lesson WHERE expires>? ORDER BY id', (now,))],
        promotions=[dict(kind=k[0], ident=k[1], context=k[2], **v) for k, v in sorted(_promotions(conn).items())],
        weights=dict(version=current.version, values=current.values),
        decisions_upto=conn.execute('SELECT coalesce(max(id),0) FROM learn_decision').fetchone()[0],
        posteriors=list(posteriors))


def governed(snapshot):
    """What a rollback restores; posteriors are evidence and stay as they are."""
    return dict(arms={a['id']: a['status'] for a in snapshot['arms']},
                lessons={l['id']: (l['status'], l['reinforced'], l['expires']) for l in snapshot['lessons']},
                promotions=sorted((p['kind'], p['ident'], p['context'], p['status'], p['week'], p['promoted'],
                                   p['changed']) for p in snapshot['promotions']),
                weights=dict(snapshot['weights']['values']))


def save_version(conn, snapshot, *, reason, now, week=None, restored_from=None):
    version = conn.execute('INSERT INTO learn_version(created,reason,week,restored_from,state) VALUES (?,?,?,?,?)',
                           (now, reason, week, restored_from,
                            json.dumps(snapshot, sort_keys=True, allow_nan=False))).lastrowid
    conn.execute('DELETE FROM learn_version WHERE version NOT IN '
                 '(SELECT version FROM learn_version ORDER BY version DESC LIMIT ?)', (KEEP_VERSIONS,))
    return version


def versions(conn, *, limit=20):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='learn_version'").fetchone() is None:
        return []
    return [dict(zip(('version', 'created', 'reason', 'week', 'restored_from'), r)) for r in conn.execute(
        'SELECT version,created,reason,week,restored_from FROM learn_version ORDER BY version DESC LIMIT ?',
        (limit,))]


def _valid(snapshot):
    try:
        return (type(snapshot) is dict and snapshot.get('version') == 1
                and type(snapshot['decisions_upto']) is int
                and all(type(a['id']) is str and a['status'] in arms.STATES for a in snapshot['arms'])
                and all(type(l['id']) is int and l['status'] in ('active', 'proposed')
                        and type(l['reinforced']) is int and l['reinforced'] >= 0
                        and type(l['expires']) in (int, float) for l in snapshot['lessons'])
                and all(p['kind'] in ('arm', 'lesson') and type(p['ident']) is str and type(p['context']) is str
                        and p['status'] in ('promoted', 'rolled_back', 'retired') and type(p['week']) is str
                        for p in snapshot['promotions'])
                and weights.validate(snapshot['weights']['values']) is not None)
    except (KeyError, TypeError, ValueError):
        return False


def rollback(conn, version=None, *, now, with_weights=False):
    """Restore a saved state; the caller owns the transaction.

    The current state is saved first as a new version, so a rollback can itself be undone.
    Every guard or owner decision made after the version (guard retirements, his accepts,
    rejects, retires, approvals, vetoes and expiries) is applied again, and his vetoes win.
    What the rollback reverted (a promotion, or a cycle retirement) waits for 30 new
    rewarded uses before the cycle decides it again. Reward weights are restored only with
    `with_weights`. Freezes, personas, talk history and the bandit's evidence are never
    touched. Returns {restored, saved, kept, weights}.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='learn_version'").fetchone() is None:
        raise ValueError('No learning-state versions are saved in this database yet.')
    row = conn.execute('SELECT version,state FROM learn_version WHERE version=?', (version,)).fetchone() \
        if version is not None else conn.execute(
            "SELECT version,state FROM learn_version WHERE reason='cycle' ORDER BY version DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError('That learning-state version is not saved.' if version is not None
                         else 'No weekly learning-state version is saved yet.')
    try:
        target = json.loads(row[1])
    except ValueError:
        target = None
    if not _valid(target):
        raise ValueError('That learning-state version cannot be read.')
    before = state(conn, now=now)
    saved = save_version(conn, before, reason='rollback', now=now, restored_from=row[0])
    kept, restored_weights = restore(conn, target, now=now, before=before, with_weights=with_weights)
    return dict(restored=row[0], saved=saved, kept=kept, weights=restored_weights)


def restore(conn, target, *, now, before, with_weights=False):
    """Returns (decisions applied again, whether the weights were restored)."""
    statuses = {a['id']: a['status'] for a in target['arms']}
    for aid, status in conn.execute('SELECT id,status FROM learn_arm').fetchall():
        if status == 'control':
            continue
        # An arm the version lacks was not live then: a proposal again, reversible from the Lab.
        want = statuses.get(aid, 'proposed' if status == 'active' else status)
        if want == 'control':
            want = status
        if want != status:
            conn.execute('UPDATE learn_arm SET status=? WHERE id=?', (want, aid))
    saved = {l['id']: l for l in target['lessons']}
    for lid, status, reinforced, expires in conn.execute(
            'SELECT id,status,reinforced,expires FROM talk_lesson').fetchall():
        if vetoed(conn, 'lesson', lid):
            continue
        old = saved.get(lid)
        if old is None:
            if expires > now:
                conn.execute('UPDATE talk_lesson SET expires=? WHERE id=?', (now, lid))
        elif (status, reinforced, expires) != (old['status'], old['reinforced'], old['expires']):
            conn.execute('UPDATE talk_lesson SET status=?,reinforced=?,expires=? WHERE id=?',
                         (old['status'], old['reinforced'], old['expires'], lid))
    conn.execute('DELETE FROM learn_promotion')
    for p in target['promotions']:
        conn.execute('INSERT INTO learn_promotion VALUES (?,?,?,?,?,?,?)',
                     (p['kind'], p['ident'], p['context'], p['status'], p['week'], p['promoted'], p['changed']))
    # Later guard and owner decisions stand: apply them again, in order.
    lived = {l['id']: l['expires'] for l in before['lessons']}
    kept = 0
    for ts, kind, ident, action in conn.execute('SELECT ts,kind,ident,action FROM learn_decision WHERE id>? '
                                                'ORDER BY id', (target['decisions_upto'],)).fetchall():
        kept += 1
        if kind == 'arm':
            conn.execute("UPDATE learn_arm SET status=? WHERE id=? AND status!='control'",
                         ('active' if action == 'accept' else 'retired', ident))
        elif action == 'approve':
            conn.execute("UPDATE talk_lesson SET status='active',expires=max(expires,?) WHERE id=?",
                         (lived.get(int(ident), 0), int(ident)))
        else:
            conn.execute('UPDATE talk_lesson SET expires=min(expires,?) WHERE id=?', (ts, int(ident)))
    for (aid,) in conn.execute("SELECT id FROM learn_arm WHERE status='active'").fetchall():
        if vetoed(conn, 'arm', aid):
            conn.execute("UPDATE learn_arm SET status='retired' WHERE id=?", (aid,))
    # What this rollback reverted waits for new evidence, so the next cycle does not redo it.
    now_promoted = {(p['kind'], p['ident'], p['context']) for p in target['promotions'] if p['status'] == 'promoted'}
    for p in before['promotions']:
        key = (p['kind'], p['ident'], p['context'])
        if p['status'] == 'promoted' and key not in now_promoted:
            conn.execute("INSERT OR REPLACE INTO learn_hold VALUES (?,?,?,'rollback',?)", (*key, now))
    was = {a['id']: a['status'] for a in before['arms']}
    for (aid,) in conn.execute("SELECT id FROM learn_arm WHERE status='active'").fetchall():
        if was.get(aid) == 'retired':
            conn.execute("INSERT OR REPLACE INTO learn_hold VALUES ('arm',?,'','rollback',?)", (aid, now))
    values = weights.validate(target['weights']['values'])
    restored = with_weights and weights.read(conn, at=now, schema_available=True).values != values
    if restored:
        weights.change(conn, values, now=now)
    return kept, with_weights


# -- live consumers (only with [learn] cycle on) --------------------------------------------------

def promoted_first(conn, notes):
    """Promoted lessons lead the style block, so they are the last trimmed."""
    try:
        promoted = {int(r[0]) for r in conn.execute(
            "SELECT ident FROM learn_promotion WHERE kind='lesson' AND status='promoted'")}
    except sqlite3.OperationalError:
        return notes
    return sorted(notes, key=lambda note: note['id'] not in promoted)


def promoted(conn):
    """{(kind, ident, context)} in force, for the Lab; empty on an older schema."""
    try:
        return {(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT kind,ident,context FROM learn_promotion WHERE status='promoted'")}
    except sqlite3.OperationalError:
        return set()


def report(conn):
    try:
        row = conn.execute('SELECT value FROM meta WHERE key=?', (REPORT_KEY,)).fetchone()
        value = json.loads(row[0]) if row else None
    except (sqlite3.OperationalError, ValueError, RecursionError):
        return None
    return value if type(value) is dict and value.get('version') == REPORT_VERSION else None


def coach_input(conn, guid):
    """Last week's report for one member's coaching call: closed arm parameters, this member's
    lesson ids and guild rates. Numbers and closed enums only, never any text."""
    value = report(conn)
    if value is None:
        return None
    registry = {a.id: a for a in arms.read(conn)}

    def params(entries):
        out = []
        for entry in entries:
            arm = registry.get(entry.get('id')) if entry.get('kind') == 'arm' else None
            item = dict(family=arm.family, params=arm.params) if arm and arm.status != 'control' else None
            if item and item not in out:
                out.append(item)
        return out[:10]

    def mine(entries):
        return sorted({e['id'] for e in entries if e.get('kind') == 'lesson' and e.get('guid') == guid
                       and type(e.get('id')) is int})[:10]
    rates = value.get('rates', {})
    return dict(week=value.get('week'),
                promoted_arms=params(value.get('promoted', [])),
                retired_arms=params(value.get('retired', [])),
                rolled_back_arms=params(value.get('rolled_back', [])),
                lessons=dict(promoted=mine(value.get('promoted', [])), retired=mine(value.get('retired', [])),
                             rolled_back=mine(value.get('rolled_back', []))),
                rates={k: dict(week=rates.get('week', {}).get(k), previous=rates.get('previous', {}).get(k))
                       for k in (*DETECTORS, 'guard')},
                diversity_ok=value.get('diversity', {}).get('ok'))


# -- the night stage ------------------------------------------------------------------------------

def _apply(conn, plan, *, now, settings, posteriors):
    """One transaction: the version first, then the gated changes still valid now."""
    cfg = effective(conn, settings)
    if cfg.freeze:
        raise _Frozen()
    week = plan['week']
    version = save_version(conn, state(conn, now=now, posteriors=posteriors), reason='cycle', now=now, week=week)
    frozen_families, frozen_members = set(cfg.frozen_families), set(cfg.frozen_members)
    registry = {a.id: a for a in arms.read(conn)}
    promoted_items, retired, rolled = [], [], []

    def lesson_live(lid):
        row = conn.execute("SELECT guid FROM talk_lesson WHERE id=? AND status='active' AND expires>?",
                           (lid, now)).fetchone()
        return row is not None and row[0] not in frozen_members and not vetoed(conn, 'lesson', lid)

    for item in plan['rollback']:
        kind, ident, key = item['kind'], item['id'], item['context']
        entry = dict(kind=kind, reasons=item['reasons'])
        if kind == 'arm':
            if ident not in registry or registry[ident].family in frozen_families:
                continue
            entry.update(id=ident, context=_key(key))
        else:
            row = conn.execute('SELECT guid FROM talk_lesson WHERE id=?', (int(ident),)).fetchone()
            if row is None or row[0] in frozen_members:
                continue
            entry.update(id=int(ident), guid=row[0])
        if conn.execute("UPDATE learn_promotion SET status='rolled_back',changed=? WHERE kind=? AND ident=? "
                        "AND context=? AND status='promoted'", (now, kind, ident, key)).rowcount:
            conn.execute("INSERT OR REPLACE INTO learn_hold VALUES (?,?,?,'diversity',?)", (kind, ident, key, now))
            rolled.append(entry)
    for item in plan['promote']:
        if item['kind'] == 'arm':
            arm = registry.get(item['id'])
            if (arm is None or arm.status != 'active' or arm.family in frozen_families
                    or vetoed(conn, 'arm', arm.id)):
                continue
            ident, key = arm.id, item['key']
        else:
            if not lesson_live(item['id']):
                continue
            ident, key = str(item['id']), ''
        if conn.execute('INSERT INTO learn_promotion VALUES (?,?,?,?,?,?,?) ON CONFLICT(kind,ident,context) '
                        "DO UPDATE SET status='promoted',week=excluded.week,promoted=excluded.promoted,"
                        "changed=excluded.changed WHERE learn_promotion.status!='promoted'",
                        (item['kind'], ident, key, 'promoted', week, now, now)).rowcount:
            conn.execute('DELETE FROM learn_hold WHERE kind=? AND ident=? AND context=?', (item['kind'], ident, key))
            promoted_items.append({k: v for k, v in item.items() if k != 'key'})
    for item in plan['retire_arms']:
        arm = registry.get(item['id'])
        if arm is None or arm.family in frozen_families:
            continue
        if conn.execute("UPDATE learn_arm SET status='retired' WHERE id=? AND status='active' AND NOT EXISTS "
                        "(SELECT 1 FROM learn_promotion WHERE kind='arm' AND ident=? AND status='promoted')",
                        (arm.id, arm.id)).rowcount:
            retired.append(dict(kind='arm', id=arm.id, reason='share', share=item['share'],
                                share_previous=item['share_previous'], probability=item['probability'],
                                uses=item['uses']))
    # A lesson past its 14-day reinforcement window has lapsed: its promotion ends with it.
    for ident, in conn.execute("SELECT ident FROM learn_promotion WHERE kind='lesson' AND status='promoted'").fetchall():
        row = conn.execute('SELECT guid,expires FROM talk_lesson WHERE id=?', (int(ident),)).fetchone()
        if row is not None and row[1] <= now and row[0] not in frozen_members:
            conn.execute("UPDATE learn_promotion SET status='retired',changed=? WHERE kind='lesson' AND ident=?",
                         (now, ident))
    for item in plan['lapsed']:
        if item['guid'] not in frozen_members:
            retired.append(dict(kind='lesson', id=item['id'], guid=item['guid'], reason='not_reinforced'))
    rose = plan['rose']
    value = dict(version=REPORT_VERSION, week=week, previous_week=plan['previous_week'], at=now,
                 state_version=version, measured=plan['measured'], rates=plan['rates'],
                 diversity=dict(ok=int(not rose), **{f'{m}_rose': int(any(x == m for _, x in rose))
                                                     for m in DIVERSITY},
                                rose=[f'{name}:{metric}' for name, metric in rose]),
                 promoted=promoted_items[:LIST_CAP], retired=retired[:LIST_CAP], rolled_back=rolled[:LIST_CAP],
                 held=dict(count=len(plan['held']), items=plan['held'][:LIST_CAP]),
                 arms=plan['arms'][:LIST_CAP], lessons=plan['lessons'][:LIST_CAP],
                 frozen=dict(families=sorted(frozen_families), members=len(frozen_members)))
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (REPORT_KEY, _fit(value)))
    return value


def _fit(value):
    """The Lab reads at most 64 KiB: drop the longest detail lists' tails, never the decisions."""
    while True:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
        lists = [v for v in (value['arms'], value['lessons'], value['held']['items']) if v]
        if len(encoded) <= REPORT_BYTES or not lists:
            return encoded
        max(lists, key=len).pop()


async def stage(ctx):
    from leeroy_mind.nightly.store import commit_unit
    from .night import read_work
    settings = load_learn_config(ctx.settings.data_dir / 'mind.toml')
    conn = ctx.store.conn
    cfg = effective(conn, settings)
    week = due_week(ctx.night)
    if not cfg.enabled or not cfg.cycle or week is None or not have(conn) or done(conn, week):
        return
    if cfg.freeze:
        log('learn_cycle_frozen', week=week)
        return
    now, tz, owner = ctx.clock.now(), ctx.tz, ctx.human
    result = {}

    def write(store):
        result.update(_apply(store.conn, plan, now=ctx.clock.now(), settings=settings,
                             posteriors=plan['posteriors']))
    try:
        plan = await read_work(ctx, lambda c: evaluate(c.store.conn, week=week, cfg=cfg, now=now, tz=tz,
                                                       owner=owner))
        commit_unit(ctx.store, ctx.night, STAGE, week, write, now=ctx.clock.now())
    except _Frozen:
        log('learn_cycle_frozen', week=week)
        return
    except Exception as error:
        # A failed cycle is one failed unit, retried on a later night of the week; the message
        # carries only the exception's type, never a value it might quote.
        ctx.fail(STAGE, week, RuntimeError('learn_cycle_failed:' + type(error).__name__))
        return
    log('learn_cycle_done', week=week, version=result['state_version'], promoted=len(result['promoted']),
        retired=len(result['retired']), rolled_back=len(result['rolled_back']), held=result['held']['count'],
        diversity_ok=result['diversity']['ok'])


def attach(runner, data_dir, store=None):
    cfg = load_learn_config(data_dir / 'mind.toml')
    if cfg.enabled and cfg.cycle and (store is None or have(store.conn)):
        names = [s.name for s in runner.stages]
        after = next((n for n in ('bandit', 'talk_coach', 'rewards') if n in names), None)
        if after is not None:
            runner.add_stage(STAGE, stage, after=after, model=None)


register_counter(STAGE, lambda ctx: 0)  # model-free: no generation calls or GPU reservation
