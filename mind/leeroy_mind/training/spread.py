"""Conservative weekly desired setups, with no claim of live adoption.

Only effective healing is currently measured well enough for member rankings.
Damage/taken are requested_pre_ai counters. Standing defaults and premades need
Realm restoration and exact application receipts before they can produce social
memories. The planner is pure; the weekly boundary records desired plans only.
"""

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
import json
import math
import random
import re
import statistics
from types import MappingProxyType

from leeroy_mind.guild.raids import CATALOG
from leeroy_mind.guild.roles import premade_spec_no, role_of, spec_tab_for
from leeroy_mind.wow import is_raid_fight

from . import store
from .cards import CLASSES, LEVERS
from .ledger import combat_role
from .levers import MemberInfo
from .score import is_farm


HEALING_BASIS = "effective_gain_with_pre_absorb_raw"
WEEK_PREFIX = "tg:spread:week:"
DEFERRED = "realm_default_restoration_and_exact_setup_application_receipt_required"
METRIC_COVERAGE = {
    "healer": "aggregate_effective_hps",
    "damage": "unavailable: requested_pre_ai is not verified boss damage",
    "tank": "unavailable: requested_pre_ai is not verified damage taken",
}


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def week_key(local):
    year, week, _ = local.isocalendar()
    return f"{year:04d}-W{week:02d}"


def _week_bounds(week, tz):
    if type(week) is not str or not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", week):
        raise ValueError("week must be an ISO year-week")
    monday = date.fromisocalendar(int(week[:4]), int(week[-2:]), 1)
    return (datetime.combine(monday, time(), tz).timestamp(),
            datetime.combine(monday + timedelta(days=7), time(), tz).timestamp())


def _guild_pulls(conn, now):
    """Exact completed guild receipts, including historical raid identity."""
    raids = {r["raid_id"]: r for r in conn.execute("SELECT * FROM guild_raid")}
    result = []
    for row in store.pull_rows(conn):
        r = dict(row)
        raid = raids.get(r["raid_id"])
        catalog = CATALOG.get(raid["raid_key"]) if raid else None
        if (not raid or not catalog or r["kind"] not in {"his", "offline"}
                or not _number(raid["started"]) or raid["started"] < 0
                or raid["ended"] is not None and (not _number(raid["ended"]) or raid["ended"] < raid["started"])
                or raid["offline"] not in (0, 1) or raid["heroic"] not in (0, 1)
                or r["complete"] != 1 or r["scoreable"] != 1
                or r["outcome"] not in {"killed", "wiped"}
                or not is_raid_fight(r["map"], r["boss_id"])
                or r["context"] != f"{r['map']}:{r['boss_id']}:{r['difficulty']}"
                or r["map"] != catalog.map_id or r["size"] != raid["size"]
                or r["lead_guid"] != raid["lead_guid"]
                or bool(raid["offline"]) != (r["kind"] == "offline")
                or r["healing_basis"] != HEALING_BASIS
                or any(type(r[k]) is not int for k in ("start_ms", "end_ms"))
                or not _number(r["received"])
                or not 0 <= r["start_ms"] < r["end_ms"] <= r["received"] * 1000
                or r["received"] > now or raid["started"] * 1000 > r["start_ms"]
                or raid["ended"] is not None and raid["ended"] * 1000 < r["end_ms"]):
            continue
        # Difficulty encodes heroic in both ten/twenty-five player modes.
        if (type(r["difficulty"]) is not int or r["difficulty"] not in (0, 1, 2, 3)
                or r["size"] != (25 if r["difficulty"] & 1 else 10)
                or bool(raid["heroic"]) != (r["difficulty"] >= 2)):
            continue
        members = [dict(m) for m in conn.execute("SELECT * FROM tg_member WHERE pull=?", (r["id"],))]
        if type(r["members"]) is not int or len(members) != r["members"] or not members:
            continue
        r["member_rows"] = members
        result.append(r)
    return result


def score_evidence(conn, week, *, now=None, tz=None):
    """Mean within-context/class/spec HPS z, with immutable source references.

    A pull is farm only when its *earlier* eligible history already established
    farm. Use elapsed fight time, not active time, to avoid rewarding downtime.
    Normalize only the requested week's comparable observations; require four
    pulls per member and at least two distinct members per comparison bucket.
    No z score implies causal improvement or overall healing skill.
    """
    local = datetime.now().astimezone()
    now = local.timestamp() if now is None else now
    tz = local.tzinfo if tz is None else tz
    begin, end = _week_bounds(week, tz)
    if not _number(now):
        raise ValueError("invalid score observation cutoff")
    history, buckets, identities = defaultdict(list), defaultdict(list), defaultdict(set)
    for r in _guild_pulls(conn, now):
        context = (r["context"], r["size"])
        farm = is_farm([outcome for ended, received, outcome in history[context]
                        if ended <= r["start_ms"] and received <= r["received"]])
        history[context].append((r["end_ms"], r["received"], r["outcome"]))
        if not farm or not begin <= r["start_ms"] / 1000 < end:
            continue
        duration = (r["end_ms"] - r["start_ms"]) / 1000
        for m in r["member_rows"]:
            if (type(m["guid"]) is not int or m["guid"] <= 0
                    or type(m["class"]) is not int or m["class"] not in CLASSES
                    or type(m["spec_tab"]) is not int or m["spec_tab"] not in (0, 1, 2)):
                continue
            identity = (m["class"], m["spec_tab"], m["role"])
            identities[m["guid"]].add(identity)
            if (m["role"] != "healer" or role_of(m["class"], m["spec_tab"], "healer") != "healer"
                    or type(m["healing"]) is not int or m["healing"] < 0
                    or type(m["active_ms"]) is not int or not 0 <= m["active_ms"] <= duration * 1000):
                continue
            hps = m["healing"] / duration
            if not math.isfinite(hps):
                continue
            buckets[(*context, *identity)].append(dict(guid=m["guid"], pull=r["id"], hps=hps,
                received=r["received"], context=r["context"], size=r["size"], identity=identity,
                start_ms=r["start_ms"], end_ms=r["end_ms"], healing=m["healing"], basis=HEALING_BASIS))
    values = defaultdict(list)
    for rows in buckets.values():
        if len({r["guid"] for r in rows}) < 2:
            continue
        mean, sd = statistics.fmean(r["hps"] for r in rows), statistics.pstdev(r["hps"] for r in rows)
        for row in rows:
            values[row["guid"]].append(row | {"z": (row["hps"] - mean) / sd if sd else 0.0})
    return {guid: {"score": statistics.fmean(r["z"] for r in rows),
                   "identity": next(iter(identities[guid])), "pulls": rows}
            for guid, rows in values.items() if len(rows) >= 4 and len(identities[guid]) == 1}


def member_scores(conn, week, *, now=None, tz=None):
    return {guid: row["score"] for guid, row in score_evidence(conn, week, now=now, tz=tz).items()}


@dataclass(frozen=True, slots=True)
class MemberPolicy:
    cls: int
    spec_tab: int
    role: str
    main_spec: str
    conscientiousness: float
    sloppy: bool = False
    theorycrafter: bool = False
    officer: bool = False


@dataclass(frozen=True, slots=True)
class RotationOption:
    id: int
    cls: int
    spec_tab: int
    status: str


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    members: Mapping[int, MemberPolicy]
    mentors: Mapping[int, int] = field(default_factory=dict)
    affinities: Mapping[tuple[int, int], float] = field(default_factory=dict)
    rotations: tuple[RotationOption, ...] = ()
    premades: Mapping[int, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self):
        for name in ("members", "mentors", "affinities"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        object.__setattr__(self, "rotations", tuple(self.rotations))
        object.__setattr__(self, "premades", MappingProxyType({k: tuple(v) for k, v in self.premades.items()}))

    def persona(self, guid):
        return self.members.get(guid)

    def mentor_of(self, guid):
        return self.mentors.get(guid)

    def affinity(self, copier, top):
        return self.affinities.get((copier, top), 0)


@dataclass(frozen=True, slots=True)
class SetupChange:
    guid: int
    rotation: int | None = None
    toggles: tuple[str, ...] = ()
    vals: Mapping[str, str | int] = field(default_factory=dict)
    premade: int | None = None
    copied_from: int | None = None
    explored: str = ""

    def __post_init__(self):
        object.__setattr__(self, "toggles", tuple(self.toggles))
        object.__setattr__(self, "vals", MappingProxyType(dict(self.vals)))


def prestige(copier, top, ports):
    persona = ports.persona(top)
    if isinstance(persona, MemberPolicy):
        officer, theory = persona.officer, persona.theorycrafter
    else:
        officer = getattr(getattr(persona, "play", None), "guild_role", None) == "officer"
        theory = getattr(getattr(persona, "game", None), "thinks_as", None) == "theorycrafter"
    affinity = ports.affinity(copier, top)
    affinity = affinity if _number(affinity) else 0
    return (3 if ports.mentor_of(copier) == top else 1) * (2 if officer else 1) * (
        2 if theory else 1) * (1 + max(0, affinity) / 100)


def _identity(policy):
    if (not isinstance(policy, MemberPolicy) or type(policy.cls) is not int or policy.cls not in CLASSES
            or type(policy.spec_tab) is not int or policy.spec_tab not in (0, 1, 2)
            or policy.role not in {"tank", "healer", "melee", "ranged"}
            or any(type(v) is not bool for v in (policy.sloppy, policy.theorycrafter, policy.officer))
            or not _number(policy.conscientiousness) or not 0 <= policy.conscientiousness <= 10
            or spec_tab_for(policy.cls, policy.main_spec, policy.role) != policy.spec_tab
            or combat_role(policy.cls, policy.spec_tab, policy.role) != policy.role):
        return None
    return policy.cls, policy.spec_tab, policy.role


def _rotations(policy, ports):
    return sorted({r.id for r in ports.rotations if isinstance(r, RotationOption)
                   and type(r.id) is int and r.id > 0 and r.status in {"field", "champion"}
                   and (r.cls, r.spec_tab) == (policy.cls, policy.spec_tab)})


def _premade_ok(value, policy, ports):
    if value is None:
        return True
    names = ports.premades.get(policy.cls, ())
    if type(value) is not int or not 0 <= value < len(names) or type(names[value]) is not str:
        return False
    # Test the actual candidate name with P1d's own persona/tab mapping. Using
    # a one-element vocabulary preserves the mapping without guessing indices.
    return premade_spec_no(policy.cls, policy.main_spec, policy.role,
                           {policy.cls: [names[value]]}) == 0


def _setup(guid, raw, policy, ports):
    if raw is None:
        raw = {}
    if isinstance(raw, SetupChange):
        raw = {k: getattr(raw, k) for k in ("guid", "rotation", "toggles", "vals", "premade")}
    try:
        raw = dict(raw)
        if raw.keys() - {"id", "guid", "since", "rotation", "toggles", "vals", "premade",
                         "copied_from", "explored", "applied"} or raw.get("guid", guid) != guid:
            return None
        toggles, vals = raw.get("toggles", ()), raw.get("vals", {})
        toggles = json.loads(toggles) if isinstance(toggles, str) else toggles
        vals = json.loads(vals) if isinstance(vals, str) else vals
        if not isinstance(toggles, (tuple, list)) or not isinstance(vals, Mapping):
            return None
        if any(type(t) is not str or len(t) < 2 or t[1:] not in LEVERS
               or LEVERS[t[1:]].kind != "strategy" or t[0] not in LEVERS[t[1:]].values
               or policy.role not in LEVERS[t[1:]].roles for t in toggles):
            return None
        if len({t[1:] for t in toggles}) != len(toggles):
            return None
        if any(k not in LEVERS or LEVERS[k].kind != "value" or policy.role not in LEVERS[k].roles
               or type(v) not in (int, str) or v not in LEVERS[k].values for k, v in vals.items()):
            return None
        rotation, premade = raw.get("rotation"), raw.get("premade")
        if (rotation is not None and (type(rotation) is not int or rotation not in _rotations(policy, ports))
                or not _premade_ok(premade, policy, ports)):
            return None
        return SetupChange(guid, rotation, tuple(sorted(toggles)), vals, premade)
    except (TypeError, ValueError):
        return None


def _signature(setup):
    return setup.rotation, setup.toggles, tuple(sorted(setup.vals.items())), setup.premade


def _explore(setup, policy, ports, rng):
    choices = []
    for name, lever in LEVERS.items():
        if policy.role not in lever.roles:
            continue
        if lever.kind == "strategy":
            toggle = lever.values[0] + name
            toggles = set(setup.toggles)
            toggles.symmetric_difference_update({toggle})
            choices.append(replace(setup, toggles=tuple(sorted(toggles)), explored=name))
        elif lever.kind == "value":
            current = setup.vals.get(name)
            if current is None:
                alternatives = lever.values[:1]
            else:
                index = lever.values.index(current)
                alternatives = lever.values[max(0, index-1):index] + lever.values[index+1:index+2]
            for value in alternatives:
                choices.append(replace(setup, vals=dict(setup.vals) | {name: value}, explored=name))
    rotations = _rotations(policy, ports)
    if rotations:
        next_rotation = rotations[(rotations.index(setup.rotation) + 1) % len(rotations)] if setup.rotation in rotations else rotations[0]
        if next_rotation != setup.rotation:
            choices.append(replace(setup, rotation=next_rotation, explored="rotation"))
    return rng.choice(choices) if choices else setup


def plan_week(groups, scores, setups, ports, rng):
    """Pure seeded policy over explicit scores and authoritative policy facts.

    Quarter sizes use ranked members; diversity counts every known member of
    the same class/spec, including other roles and already proposed changes.
    Missing desired rows mean no desired override, never proof of live stock.
    """
    if not isinstance(ports, PolicySnapshot):
        raise TypeError("plan_week requires an immutable PolicySnapshot")
    valid, population, seen, unknown_specs = {}, defaultdict(list), set(), set()
    for key, guids in sorted(groups.items()):
        if len(set(guids)) != len(guids) or seen.intersection(guids):
            raise ValueError("a member may occur in one group only")
        seen.update(guids)
        for guid in guids:
            policy = ports.persona(guid)
            if _identity(policy) != key:
                unknown_specs.add(key[:2])
                continue
            setup = _setup(guid, setups.get(guid), policy, ports)
            if setup is not None:
                valid[guid] = setup
                population[key[:2]].append(guid)
            else:
                unknown_specs.add(key[:2])
    prospective = dict(valid)
    changes = []
    for key, guids in sorted(groups.items()):
        # A malformed desired setup cannot quietly disappear from diversity.
        if len(guids) < 3 or key[:2] in unknown_specs:
            continue
        ranked = sorted((g for g in guids if _number(scores.get(g))), key=lambda g: (scores[g], g))
        if len(ranked) < 3:
            continue
        quarter = math.ceil(len(ranked) / 4)
        for guid in ranked[:quarter]:
            policy, original = ports.persona(guid), valid[guid]
            mentor = ports.mentor_of(guid)
            sources = [g for g in ranked[-quarter:] if scores[g] > scores[guid]]
            preferred = mentor if mentor in ranked and scores[mentor] > scores[guid] else None
            if preferred is not None and preferred not in sources:
                sources.append(preferred)
            while policy.theorycrafter or sources:
                # Theorycrafters explore their own setup and do not copy.
                if policy.theorycrafter:
                    candidate = _explore(original, policy, ports, rng)
                else:
                    source = preferred if preferred in sources else rng.choices(sources,
                        weights=[prestige(guid, g, ports) for g in sources], k=1)[0]
                    sources.remove(source)
                    fidelity = .5 if policy.sloppy else policy.conscientiousness / 10
                    values = {part: getattr(valid[source], part) if rng.random() < fidelity else getattr(original, part)
                              for part in ("rotation", "toggles", "vals", "premade")}
                    candidate = SetupChange(guid, **values, copied_from=source)
                    if _signature(candidate) != _signature(original) and rng.random() < .2:
                        candidate = _explore(candidate, policy, ports, rng)
                if _signature(candidate) == _signature(original):
                    # Fidelity gets one draw per part. A no-op does not earn
                    # extra copy attempts; only diversity requests a redraw.
                    break
                members = population[key[:2]]
                counts = Counter(_signature(candidate if g == guid else prospective[g]) for g in members)
                if counts[_signature(candidate)] * 3 > 2 * len(members):
                    if policy.theorycrafter:
                        break
                    continue
                prospective[guid] = candidate
                changes.append(candidate)
                break
    return changes


@dataclass(frozen=True, slots=True)
class SpreadSchedule:
    """Provider proof includes retained/cleanup work and acquiring schedulers.

    ``members`` is a current observed identity snapshot, not desired personas.
    ``premades`` comes from the actual Guild vocabulary. No provider means no
    permission to plan. Service owns this snapshot and its freshness.
    """
    observed_at: float
    connected: bool
    human_online: bool
    raid_active: bool
    lab_active: bool
    benchmark_active: bool
    acquiring: bool
    members: Mapping[int, MemberInfo]
    premades: Mapping[int, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "members", MappingProxyType(dict(self.members)))
        object.__setattr__(self, "premades", MappingProxyType({k: tuple(v) for k, v in self.premades.items()}))


def _snapshot(ports, proof):
    members, mentors, affinities = {}, {}, {}
    cast = ports.cast()
    for guid, current in proof.members.items():
        live, persona = cast.get(guid), ports.persona(guid)
        if (not isinstance(current, MemberInfo) or current.guid != guid or live is None or persona is None
                or guid == ports.human_guid or getattr(persona, "guid", None) != guid
                or (live.guid, live.class_, live.spec_tab) != (guid, current.cls, current.spec_tab)):
            continue
        try:
            policy = MemberPolicy(current.cls, current.spec_tab, current.role, persona.play.main_spec,
                persona.big_five.conscientiousness.score, persona.play.skill == "sloppy",
                persona.game.thinks_as == "theorycrafter", persona.play.guild_role == "officer")
        except AttributeError:
            continue
        if _identity(policy) is None:
            continue
        members[guid] = policy
        mentor = ports.mentor_of(guid)
        if type(mentor) is int:
            mentors[guid] = mentor
    for a in members:
        for b in members:
            affinities[a, b] = ports.affinity(a, b)
    rotations = tuple(RotationOption(r["id"], r["class"], r["spec_tab"], r["status"])
                      for r in store.rotation_rows(ports.conn))
    return PolicySnapshot(members, mentors, affinities, rotations, proof.premades)


def _admitted(ports, applier, week, schedule):
    now, local = ports.clock.now(), ports.clock.local()
    if (not ports.cfg.enabled or not ports.cfg.spread or not ports.cfg.act
            or local.weekday() != 6 or local.hour < 12 or week_key(local) != week
            or ports.human_online() is not False or applier.acting(now) is not True):
        return None
    try:
        proof = schedule() if schedule is not None else None
    except Exception:
        return None
    if (not isinstance(proof, SpreadSchedule) or not _number(proof.observed_at)
            or not 0 <= now - proof.observed_at <= 60
            or proof.connected is not True or proof.human_online is not False
            or any(type(g) is not int or g <= 0 or not isinstance(m, MemberInfo) or m.guid != g
                   or type(m.cls) is not int or m.cls not in CLASSES
                   or type(m.spec_tab) is not int or m.spec_tab not in (0, 1, 2)
                   or m.role not in {"tank", "healer", "melee", "ranged"} for g, m in proof.members.items())
            or any(getattr(proof, k) is not False for k in ("raid_active", "lab_active", "benchmark_active", "acquiring"))
            or ports.conn.execute("SELECT 1 FROM tg_applied WHERE undone IS NULL LIMIT 1").fetchone()
            or ports.conn.execute("SELECT 1 FROM guild_raid WHERE ended IS NULL LIMIT 1").fetchone()):
        return None
    receipts = [r["received"] for r in _guild_pulls(ports.conn, now)]
    if not receipts or max(receipts) - min(receipts) < max(28, ports.cfg.observe_days) * 86400:
        return None
    gate = ports.conn.execute("SELECT value FROM meta WHERE key='tg_gate'").fetchone()
    try:
        gate = json.loads(gate[0]) if gate else None
    except (TypeError, ValueError):
        return None
    if (not isinstance(gate, dict) or gate.get("phase") != "passed"
            or not _number(gate.get("verdict_ms")) or not 0 <= gate["verdict_ms"] <= ports.cfg.gate_max_ms):
        return None
    return proof


async def run_week(ports, applier, week, *, schedule=None, rng=None):
    """Atomically store one week's desired plans; return planned row count.

    No awaits or commands are needed while outside-raid adoption is deferred.
    Cancellation/restart therefore cannot split the operation receipt from its
    setup rows. No memory, aside or ``applied`` marker represents a mere plan.
    The caller must not wrap this operation in another Store transaction.
    """
    conn = ports.conn
    # open_db uses autocommit=False: even an idle connection has a transaction.
    # This synchronous operation owns the with-connection commit/rollback below;
    # callers must not invoke it inside another logical Store transaction.
    proof = _admitted(ports, applier, week, schedule)
    if proof is None or conn.execute("SELECT 1 FROM meta WHERE key=?", (WEEK_PREFIX + week,)).fetchone():
        return 0
    now, local = ports.clock.now(), ports.clock.local()
    policy = _snapshot(ports, proof)
    groups = defaultdict(list)
    for guid, member in proof.members.items():
        if isinstance(member, MemberInfo) and guid != ports.human_guid:
            groups[member.cls, member.spec_tab, member.role].append(guid)
    evidence = score_evidence(conn, week, now=now, tz=local.tzinfo)
    scores = {g: r["score"] for g, r in evidence.items()
              if g in policy.members and r["identity"] == _identity(policy.members[g])
              and ports.holder(g) is None}
    setups = {g: dict(row) for g, row in store.latest_setups(conn).items()}
    rotations = [dict(row) for row in store.rotation_rows(conn)]
    changes = plan_week(groups, scores, setups, policy, rng or random.Random(week))
    if not changes:
        return 0
    # Claim before any future await. The unique receipt also serializes a
    # second process; all version/identity checks run after acquiring the write.
    with conn:
        inserted = conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
            (WEEK_PREFIX + week, store._json({"state": "planning", "at": now}))).rowcount
        if not inserted:
            return 0
        fresh = _admitted(ports, applier, week, schedule)
        if (fresh is None or _snapshot(ports, fresh) != policy
                or dict(fresh.members) != dict(proof.members)
                or any(ports.holder(g) is not None for change in changes
                       for g in (change.guid, change.copied_from) if g is not None)
                or {g: dict(row) for g, row in store.latest_setups(conn).items()} != setups
                or [dict(row) for row in store.rotation_rows(conn)] != rotations
                or score_evidence(conn, week, now=now, tz=local.tzinfo) != evidence):
            conn.execute("UPDATE meta SET value=? WHERE key=?", (store._json({
                "state": "cancelled", "reason": "planning_inputs_changed", "at": now}), WEEK_PREFIX + week))
            return 0
        rows = []
        for change in changes:
            ident = conn.execute("INSERT INTO tg_setup(guid,since,rotation,toggles,vals,premade,copied_from,explored) "
                "VALUES (?,?,?,?,?,?,?,?)", (change.guid, now, change.rotation, store._json(change.toggles),
                store._json(change.vals), change.premade, change.copied_from, change.explored)).lastrowid
            sources = {g for g in (change.guid, change.copied_from) if g is not None}
            rows.append(dict(setup=ident, guid=change.guid, copied_from=change.copied_from, explored=change.explored,
                previous_setup=setups.get(change.guid, {}).get("id"),
                source_setup=setups.get(change.copied_from, {}).get("id"),
                evidence={str(g): evidence[g] for g in sources}, application="deferred"))
        receipt = dict(state="desired", at=now, week=week, reason=DEFERRED, metric_coverage=METRIC_COVERAGE,
            changes=rows, schedule_observed_at=fresh.observed_at,
            rotations=rotations, premades=dict(policy.premades))
        conn.execute("UPDATE meta SET value=? WHERE key=?", (store._json(receipt), WEEK_PREFIX + week))
    return len(rows)
