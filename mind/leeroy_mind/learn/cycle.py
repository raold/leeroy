"""The weekly self-improvement cycle: held-out gates, promotion, retirement and rollback.

It reads numbers and ids only and writes no text: the report and every learning-state
version hold arm ids, lesson ids, member guids, context keys, counts, rates and codes.
It never changes reward weights, guardrails, personas or talk history. Only the owner's
rollback (CLI or Lab) restores weights, as part of a whole saved state.
"""
from datetime import date, timedelta
import json
import math
import sqlite3
from types import SimpleNamespace

from leeroy_mind.logs import log
from leeroy_mind.nightly.budget import register_counter
from leeroy_mind.nightly.settings import iso_week, week_bounds
from . import arms, bandit, lessons, weights
from .config import load_learn_config
from .control import REPORT_KEY, effective, vetoed
from .reward import decayed

STAGE = 'learn_cycle'
REPORT_VERSION = 1
PROBABILITY = .9  # posterior probability of beating control
USES = 30  # rewarded uses before promotion
SHARE = .05  # an arm under this weekly share for two weeks is retired
KEEP_VERSIONS = 52
LIST_CAP = 100
REPORT_BYTES = 60000  # under the Lab's 64 KiB read
CONTEXT_CAP = 200
GRID = 4000
DETECTORS = ('repetition', 'stock', 'nonsequitur', 'unawareness')
DIVERSITY = ('repetition', 'stock')
TRIPS = frozenset({'rewrite', 'react', 'drop'})
SCREENED = TRIPS | {'send'}
REPETITION_RULES = frozenset({'member_across_days', 'guild_evening_phrase'})
STOCK_RULES = frozenset({'stock_opening', 'stock_closing'})
EPSILON = 1e-12


class _Frozen(Exception):
    """Learning froze between the cycle's read and its write: no receipt, retried later."""


def have(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return ({'learn_version', 'learn_promotion', 'meta', 'night_step'} <= names and weights.available(conn)
            and arms.available(conn) and lessons.available(conn) and 'bandit_credit' in names)


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
    """Beta(1,1) plus decayed reward mass, mapped as the bandit maps it."""
    alpha = beta = 1.
    for value, cap, ts in rows:
        weight = cap if value > 0 else 1.
        alpha += decayed(weight * (1 + value) / 2, age_seconds=max(0., now - ts))
        beta += decayed(weight * (1 - value) / 2, age_seconds=max(0., now - ts))
    return alpha, beta


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
    Contradiction needs a model and is never measured here.
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
    measured = dict(repetition=1, stock=1, nonsequitur=1, unawareness=1, contradiction=0, guard=1)
    try:
        for flag in unawareness.detect(conn, lines)[0]:
            kinds[flag.line_id].add('unawareness')
    except sqlite3.OperationalError:
        measured['unawareness'] = 0
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
        rows.append(SimpleNamespace(id=line.id, guid=line.guid, channel=line.channel, kinds=kinds[line.id],
                                    anchor=anchor, arms=arm_ids, lessons=lesson_ids, verdict=verdict, key=key))
    return rows, measured


def _tally(rows, keep=lambda row: True):
    tally = Tally()
    for row in rows:
        if keep(row):
            tally.add(row.kinds, row.anchor, row.verdict)
    return tally


def _shares(rows, registry, frozen_members, credited):
    """Each arm's share of its family's generations that week, in the bandit contexts where
    it has been measured (a length arm is never offered on Discord, so Discord does not
    dilute it), frozen members left out. None when the family had no generation there."""
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
        keys = credited.get(aid, ())
        total = sum(family_turns.get((arm.family, key), 0) for key in keys)
        out[aid] = sum(arm_turns.get((aid, key), 0) for key in keys) / total if total else None
    return out


def _not_worse(carrier, comparison):
    return all(carrier.rate(k) <= comparison.rate(k) + EPSILON for k in DETECTORS)


def gate(*, uses, probability, carrier, comparison, guard_worse=False, vetoed=False, rolled_back=False,
         rose=()):
    """Why a candidate waits; an empty list promotes it. Every gate must pass:
    30 rewarded uses, a posterior probability above 0.9 of beating its comparison, last
    week's lines with no detector rate above the comparison's, no guard regression, no
    owner veto, no earlier rollback, and no rise in guild repetition or stock phrases."""
    reasons = []
    if rolled_back:
        reasons.append('rolled_back')
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


def _key(encoded):
    try:
        return json.loads(encoded) if bandit.valid_key(encoded) else None
    except (TypeError, ValueError):
        return None


def _promotions(conn):
    return {(r[0], r[1], r[2]): dict(status=r[3], week=r[4], promoted=r[5], changed=r[6])
            for r in conn.execute('SELECT kind,ident,context,status,week,promoted,changed FROM learn_promotion')}


def _lesson_rewards(conn, lesson_id, guid):
    used = conn.execute(
        "SELECT r.value,r.cap_weight,c.ts,c.channel FROM talk_reward r JOIN talk_recipe p ON p.line_id=r.line_id "
        "JOIN chat_line c ON c.id=r.line_id WHERE r.guid=? AND EXISTS "
        "(SELECT 1 FROM json_each(p.lesson_ids) j WHERE j.value=?)", (guid, lesson_id)).fetchall()
    if not used:
        return [], []
    channels = sorted({r[3] for r in used})
    first = min(r[2] for r in used)
    without = conn.execute(
        "SELECT r.value,r.cap_weight,c.ts FROM talk_reward r JOIN talk_recipe p ON p.line_id=r.line_id "
        "JOIN chat_line c ON c.id=r.line_id WHERE r.guid=? AND c.ts>=? AND c.channel IN "
        f"({','.join('?' for _ in channels)}) AND NOT EXISTS "
        "(SELECT 1 FROM json_each(p.lesson_ids) j WHERE j.value=?)", (guid, first, *channels, lesson_id)).fetchall()
    return [r[:3] for r in used], without


def evaluate(conn, *, week, cfg, now, tz, owner):
    """The cycle's plan from one read snapshot. Writes nothing; the stage applies it."""
    start, end = week_bounds(week, tz)
    last_week = previous_week(week)
    last_start, last_end = week_bounds(last_week, tz)
    this, measured = measure(conn, start, end, owner)
    last, _ = measure(conn, last_start, last_end, owner)
    overall, before = _tally(this), _tally(last)
    rose = [m for m in DIVERSITY if overall.lines and before.lines and overall.rate(m) > before.rate(m) + EPSILON]
    frozen_members = set(cfg.frozen_members)
    frozen_families = set(cfg.frozen_families)
    mine = [row for row in this if row.guid not in frozen_members]
    registry = {a.id: a for a in arms.read(conn)}
    controls = arms.controls()
    promotions = _promotions(conn)
    in_force = {k: v for k, v in promotions.items() if v['status'] == 'promoted'}
    plan = dict(week=week, previous_week=last_week, measured=measured, rose=rose,
                rates=dict(week=overall.summary(), previous=before.summary()),
                promote=[], held=[], retire_arms=[], lapsed=[], rollback=[], arms=[], lessons=[])

    # Arms: evidence and gates per bandit context, the bandit's own granularity.
    posteriors, credited = {}, {}
    pairs = conn.execute('SELECT DISTINCT context,arm_id FROM bandit_credit ORDER BY context,arm_id').fetchall()
    for key, aid in pairs:
        credited.setdefault(aid, set()).add(key)
    for key in sorted({k for k, _ in pairs})[:CONTEXT_CAP]:
        posteriors[key] = bandit.posterior(conn, None, key, now=now)
    plan['posteriors'] = [dict(context=_key(key), arm_id=aid, **{k: round(v, 9) if type(v) is float else v
                                                                  for k, v in stats.items()})
                          for key, stats in posteriors.items() for aid, stats in sorted(stats.items())
                          if _key(key) is not None][:2000]
    for aid in sorted(credited):
        arm = registry.get(aid)
        if arm is None or arm.status != 'active' or arm.family in frozen_families:
            continue
        control = controls[arm.family]
        for key in sorted(credited[aid]):
            if key not in posteriors or _key(key) is None:
                continue
            if ('arm', aid, key) in in_force:
                continue
            stats = posteriors[key].get(aid)
            base = posteriors[key].get(control, dict(alpha=1., beta=1., uses=0, bad=0, attempts=0))
            carrier = _tally(mine, lambda r: r.key == key and aid in r.arms)
            comparison = _tally(mine, lambda r: r.key == key and control in r.arms)
            probability = beats((stats['alpha'], stats['beta']), (base['alpha'], base['beta']))
            # Guard trips over all its screened attempts (rejected drafts too) against control's.
            ever_worse = bool(stats['bad']) and (not base['attempts'] or stats['bad'] / stats['attempts']
                                                 > base['bad'] / base['attempts'] + EPSILON)
            reasons = gate(uses=stats['uses'], probability=probability, carrier=carrier, comparison=comparison,
                           guard_worse=ever_worse, vetoed=vetoed(conn, 'arm', aid), rose=rose,
                           rolled_back=promotions.get(('arm', aid, key), {}).get('status') == 'rolled_back')
            entry = dict(kind='arm', id=aid, context=_key(key), probability=round(probability, 6),
                         uses=stats['uses'], rate=round(carrier.rate(), 6), control_rate=round(comparison.rate(), 6))
            if reasons:
                plan['held'].append(dict(entry, reasons=reasons))
            else:
                plan['promote'].append(dict(entry, key=key))

    # Lessons: with-versus-without for the same member in the same rooms.
    for lid, guid, status, expires in conn.execute(
            "SELECT id,guid,status,expires FROM talk_lesson WHERE status='active' AND expires>? ORDER BY id", (now,)):
        if guid in frozen_members or ('lesson', str(lid), '') in in_force:
            continue
        used, without = _lesson_rewards(conn, lid, guid)
        if not used:
            continue
        carrier = _tally(mine, lambda r: lid in r.lessons)
        rooms = {r.channel for r in mine if lid in r.lessons}
        comparison = _tally(mine, lambda r: r.guid == guid and r.channel in rooms and r.anchor is not None
                            and lid not in r.lessons)
        probability = beats(_mass(used, now), _mass(without, now))
        reasons = gate(uses=len(used), probability=probability, carrier=carrier, comparison=comparison,
                       vetoed=vetoed(conn, 'lesson', lid), rose=rose,
                       rolled_back=promotions.get(('lesson', str(lid), ''), {}).get('status') == 'rolled_back')
        entry = dict(kind='lesson', id=lid, guid=guid, probability=round(probability, 6), uses=len(used),
                     rate=round(carrier.rate(), 6), control_rate=round(comparison.rate(), 6))
        (plan['held'] if reasons else plan['promote']).append(dict(entry, reasons=reasons) if reasons else entry)

    # Retirement: an arm the bandit has measured that stays under 5% of its family's
    # generations for both weeks; a lesson whose 14-day reinforcement window has passed.
    shares, last_shares = (_shares(rows, registry, frozen_members, credited) for rows in (this, last))
    uses = dict(conn.execute('SELECT arm_id,count(*) FROM bandit_credit GROUP BY arm_id'))
    in_force_arms = {ident for kind, ident, _ in in_force if kind == 'arm'}
    for aid, arm in sorted(registry.items()):
        if arm.status != 'active' or arm.family in frozen_families:
            continue
        share, previous = shares.get(aid), last_shares.get(aid)
        if (share is not None and previous is not None and share < SHARE and previous < SHARE
                and (uses.get(aid, 0) >= USES or aid in in_force_arms)):
            plan['retire_arms'].append(dict(id=aid, share=round(share, 6), share_previous=round(previous, 6)))
    # Lapsed during the evaluated week, so each lapse is reported by exactly one cycle.
    for lid, guid in conn.execute(
            "SELECT id,guid FROM talk_lesson WHERE status='active' AND expires>? AND expires<=? ORDER BY id",
            (start, min(end, now))):
        if guid not in frozen_members and not vetoed(conn, 'lesson', lid):
            plan['lapsed'].append(dict(id=lid, guid=guid))

    # The diversity floor: when repetition or stock phrases rose, roll back the most recent
    # promotions whose carriers ran higher on what rose than their comparison lines.
    if rose:
        correlated = []
        for (kind, ident, key), item in in_force.items():
            if kind == 'arm':
                arm = registry.get(ident)
                if arm is None or arm.family in frozen_families:
                    continue
                carrier = _tally(mine, lambda r: r.key == key and ident in r.arms)
                comparison = _tally(mine, lambda r: r.key == key and controls[arm.family] in r.arms)
            else:
                row = conn.execute('SELECT guid FROM talk_lesson WHERE id=?', (int(ident),)).fetchone()
                if row is None or row[0] in frozen_members:
                    continue
                lid = int(ident)
                carrier = _tally(mine, lambda r: lid in r.lessons)
                rooms = {r.channel for r in mine if lid in r.lessons}
                comparison = _tally(mine, lambda r: r.guid == row[0] and r.channel in rooms
                                    and r.anchor is not None and lid not in r.lessons)
            hit = [m for m in rose if carrier.lines and carrier.rate(m) > comparison.rate(m) + EPSILON]
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
        posteriors=list(posteriors))


def governed(snapshot):
    """What a rollback restores exactly; posteriors are evidence and stay as they are."""
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


def rollback(conn, version=None, *, now):
    """Restore a saved state; the caller owns the transaction. Returns (restored, saved).

    The current state is saved first as a new version, so a rollback can itself be undone.
    The owner's vetoes win: a vetoed arm stays retired and a vetoed lesson stays expired.
    Freezes, personas, talk history and the bandit's evidence are never touched.
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
    saved = save_version(conn, state(conn, now=now), reason='rollback', now=now, restored_from=row[0])
    restore(conn, target, now=now)
    return row[0], saved


def restore(conn, target, *, now):
    statuses = {a['id']: a['status'] for a in target['arms']}
    for aid, status in conn.execute('SELECT id,status FROM learn_arm').fetchall():
        if status == 'control':
            continue
        want = statuses.get(aid, 'retired' if status == 'active' else status)
        if want == 'control' or want == 'active' and vetoed(conn, 'arm', aid):
            want = 'retired' if want == 'active' else status
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
    values = weights.validate(target['weights']['values'])
    if weights.read(conn, at=now, schema_available=True).values != values:
        weights.change(conn, values, now=now)


# -- live consumers (only with [learn] cycle on) --------------------------------------------------

def promoted_arms(conn, key):
    try:
        return {r[0] for r in conn.execute("SELECT ident FROM learn_promotion WHERE kind='arm' AND context=? "
                                           "AND status='promoted'", (key,))}
    except sqlite3.OperationalError:
        return set()


def promoted_first(conn, notes):
    """Promoted lessons lead the style block, so they are the last trimmed."""
    try:
        promoted = {int(r[0]) for r in conn.execute(
            "SELECT ident FROM learn_promotion WHERE kind='lesson' AND status='promoted'")}
    except sqlite3.OperationalError:
        return notes
    return sorted(notes, key=lambda note: note['id'] not in promoted)


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
    promoted, retired, rolled = [], [], []

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
                        "changed=excluded.changed WHERE learn_promotion.status='retired'",
                        (item['kind'], ident, key, 'promoted', week, now, now)).rowcount:
            promoted.append({k: v for k, v in item.items() if k != 'key'})
    for item in plan['retire_arms']:
        arm = registry.get(item['id'])
        if arm is None or arm.family in frozen_families:
            continue
        if conn.execute("UPDATE learn_arm SET status='retired' WHERE id=? AND status='active'", (arm.id,)).rowcount:
            conn.execute("UPDATE learn_promotion SET status='retired',changed=? WHERE kind='arm' AND ident=? "
                         "AND status='promoted'", (now, arm.id))
            retired.append(dict(kind='arm', id=arm.id, reason='share', share=item['share'],
                                share_previous=item['share_previous']))
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
                 diversity=dict(ok=int(not rose), **{f'{m}_rose': int(m in rose) for m in DIVERSITY}),
                 promoted=promoted[:LIST_CAP], retired=retired[:LIST_CAP], rolled_back=rolled[:LIST_CAP],
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
