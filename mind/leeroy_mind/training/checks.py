"""Checks over observed evidence; missing observations never imply a pass.

The current tap's damage requests and thread-paired spell tags do not establish
actual damage, avoidable hits, or a killer. No provenance string is silently
promoted to a stronger measurement contract here.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import TYPE_CHECKING, Literal

from leeroy_mind.wow import is_raid_fight

from . import protocol as p, store
from .ledger import PullRecord
from .mechanics import boss_mechanics, no_behind

if TYPE_CHECKING:
    from .spells import SpellBook


CHECKS_VERSION = 1
# The raid playbooks' further checks ([training] playbooks) are a newer contract.
PLAYBOOK_CHECKS_VERSION = 2


def checks_version(playbooks: bool = False) -> int:
    return PLAYBOOK_CHECKS_VERSION if playbooks else CHECKS_VERSION


@dataclass(frozen=True)
class Failure:
    kind: Literal["avoid", "debuff", "buff", "adds", "spread", "behind", "aggro", "healer_oom", "tank_range"]
    key: str
    guid: int
    count: int
    deaths: int
    damage_share: float


@dataclass(frozen=True)
class CheckResult:
    failures: tuple[Failure, ...]
    causes: tuple[str, ...]
    failed_mechanics: int
    coverage: tuple[str, ...] = ()
    complete: bool = False
    version: int = CHECKS_VERSION


def run_checks(record: PullRecord, roles: dict[int, str], book: SpellBook, *,
               playbooks: bool = False) -> CheckResult:
    """Return proven violations and explicit reasons checks remain incomplete.

    ``failed_mechanics`` is the observed lower bound. Only ``complete`` permits
    interpreting it as the checked total; an empty failure list is not a pass.
    ``coverage`` contains stable, sorted reason strings, not coverage estimates.
    ``playbooks`` adds the raid playbooks' version 2 checks for every covered raid.
    """
    h = record.header
    version = checks_version(playbooks)
    table, exempt_behind = boss_mechanics(playbooks), no_behind(playbooks)
    causes = ("unknown",) * len(record.deaths)
    if not record.complete or h.outcome not in {"killed", "wiped"} or not is_raid_fight(h.map, h.boss_id):
        return CheckResult((), causes, 0, ("pull:ineligible",), version=version)
    duration = (h.end_ms - h.start_ms) / 1000
    if duration <= 0 or not record.members or len(record.members) != h.members:
        return CheckResult((), causes, 0, ("pull:empty",), version=version)

    failures, missing = {}, set()
    if (h.map, h.boss_id) not in table:
        missing.add("mechanics:generic_only")
    members = {m.guid: m for m in record.members}
    known_roles = {g: roles[g] for g in members if roles.get(g) in {"tank", "healer", "melee", "ranged"}}
    if len(known_roles) != len(members):
        missing.add("roles:missing")
    samples = {g: _samples(record.tracks.get((g, 0), {}), ("xs", "ys", "hp", "power"), duration)
               for g in members}
    boss = _samples(record.tracks.get((0, h.boss_entry), {}), ("xs", "ys", "hp", "o", "target"), duration)
    expected = math.ceil(duration)

    def add(kind, key, guid, count):
        if count > 0:
            failures[guid, kind, key] = Failure(kind, key, guid, count, 0, 0.0)

    for mechanic in table.get((h.map, h.boss_id), ()):
        selected = [m for m in record.members if not mechanic.roles or known_roles.get(m.guid) in mechanic.roles]
        if mechanic.kind in {"avoid", "adds"}:
            # Neither current nor arbitrary future labels are proof of stronger
            # instrumentation. A new supported contract needs explicit code.
            if selected:
                missing.add(f"{mechanic.kind}:unverified_damage")
            continue
        if mechanic.kind in {"debuff", "buff"}:
            if mechanic.kind == "buff" and duration < mechanic.min_pull_s:
                continue
            for name in mechanic.names:
                ids = book.ids(name)
                if not ids:
                    missing.add(f"spell:unresolved:{name}")
                    continue
                raid_count = 0
                for m in selected:
                    rows = [d for d in m.debuffs if d.spell in ids]
                    if mechanic.kind == "buff":
                        if max((d.max_stack for d in rows), default=0) >= mechanic.min_stack:
                            continue
                        if samples[m.guid].get(expected - 1, {}).get("hp") == 0:
                            continue
                        # Watch activation and uninterrupted aura collection are
                        # not recorded. A low recorded maximum is not proof that
                        # the member failed to obtain stacks during the pull.
                        missing.add(f"buff:watch_coverage:{m.guid}:{name}")
                        continue
                    if not rows and len(m.debuffs) >= 24:
                        missing.add(f"debuff:capped:{m.guid}:{name}")
                    count = sum(d.count for d in rows)
                    raid_count += count
                    if mechanic.scope == "member":
                        excess = (max((d.max_stack for d in rows), default=0) - mechanic.max_stack
                                  if mechanic.max_stack else count - mechanic.max_count)
                        add("debuff", name, m.guid, excess)
                if mechanic.kind == "debuff" and mechanic.scope == "raid":
                    add("debuff", name, 0, raid_count - mechanic.max_count)
        elif mechanic.kind == "spread":
            exempt_ids = set()
            unresolved = False
            for name in mechanic.exempt:
                ids = book.ids(name)
                if not ids:
                    missing.add(f"spell:unresolved:{name}")
                    unresolved = True
                exempt_ids.update(ids)
            if unresolved or mechanic.exempt and len(record.casts) >= 64:
                missing.add("spread:exemption_coverage")
                continue
            exempt = {t for c in record.casts if c.spell in exempt_ids
                      for t in range(c.at_s, min(expected, c.at_s + mechanic.exempt_s))}
            for m in selected:
                own = samples[m.guid]
                peers = [samples[other.guid] for other in selected if other.guid != m.guid]
                if len(own) < expected or any(len(peer) < expected for peer in peers):
                    missing.add(f"spread:tracks:{m.guid}")
                bad = [t for t, point in own.items() if t not in exempt and point["hp"] > 0
                       and any(t in peer and peer[t]["hp"] > 0 and _near(point, peer[t], 8) for peer in peers)]
                if sum(_interval(t, duration) for t in bad) > .3 * _possible_alive(own, duration, exempt):
                    add("spread", "spread", m.guid, len(bad))

    for guid, role in known_roles.items():
        own = samples[guid]
        living = {t: point for t, point in own.items() if point["hp"] > 0}
        if role == "melee" and (h.map, h.boss_id) not in exempt_behind:
            if len(own) < expected or len(boss) < expected:
                missing.add(f"behind:tracks:{guid}")
            bad = []
            for t, point in living.items():
                if t not in boss or boss[t]["hp"] == 0:
                    continue
                dx, dy = point["xs"] - boss[t]["xs"], point["ys"] - boss[t]["ys"]
                if dx == dy == 0:
                    missing.add(f"behind:coincident:{guid}")
                    continue
                facing = boss[t]["o"] / 100
                if dx * math.cos(facing) + dy * math.sin(facing) > 0:
                    bad.append(t)
            if sum(_interval(t, duration) for t in bad) > .3 * _possible_alive(own, duration):
                add("behind", "behind", guid, len(bad))
        if role != "tank":
            if len(own) < expected or len(boss) < expected:
                missing.add(f"aggro:tracks:{guid}")
            seconds = [t for t in living if t in boss and boss[t]["hp"] > 0 and boss[t]["target"] == guid]
            longest = _longest(seconds, duration)
            if longest >= 2:
                add("aggro", "aggro", guid, math.floor(longest))
        if role == "healer":
            if len(own) < expected:
                missing.add(f"healer_oom:tracks:{guid}")
            longest = _longest([t for t, point in living.items() if point["power"] < 10], duration)
            if longest >= 10:
                add("healer_oom", "healer_oom", guid, math.floor(longest))

    healers = [g for g, role in known_roles.items() if role == "healer"]
    range_failures = {}
    for death in record.deaths:
        if known_roles.get(death.guid) != "tank":
            continue
        second = death.at_ms // 1000
        if len(known_roles) != len(members) or any(second not in samples[g] for g in healers):
            missing.add(f"tank_range:tracks:{death.guid}:{death.at_ms}")
            continue
        origin = {"xs": death.x, "ys": death.y}
        nearby = sum(samples[g][second]["hp"] > 0 and _near(origin, samples[g][second], 40) for g in healers)
        if nearby < (2 if h.size == 25 else 1):
            range_failures[death.guid] = range_failures.get(death.guid, 0) + 1
    for guid, count in range_failures.items():
        add("tank_range", "tank_range", guid, count)
    if record.deaths:
        # The recorded "killing" source is the last damage request, not a
        # killer callback. Position/HP correlations alone cannot prove cause.
        missing.add("deaths:causal_attribution")
    ordered = tuple(failures[key] for key in sorted(failures))
    failed = sum(f.kind in {"avoid", "debuff", "buff", "spread", "behind", "adds"} for f in ordered)
    return CheckResult(ordered, causes, failed, tuple(sorted(missing)), not missing, version)


def _samples(data, fields, duration):
    """Reject ambiguous legacy list offsets rather than inventing timestamps."""
    seconds = data.get("at_s", ())
    if (any(len(data.get(key, ())) != len(seconds) for key in fields)
            or any(type(t) is not int or t < 0 or t >= duration for t in seconds)
            or any(a >= b for a, b in zip(seconds, seconds[1:]))):
        return {}
    return {t: {key: data[key][i] for key in fields} for i, t in enumerate(seconds)}


def _interval(t, duration):
    return min(1.0, duration - t)


def _longest(seconds, duration):
    longest = current = 0.0
    previous = -2
    for t in sorted(seconds):
        current = current + _interval(t, duration) if t == previous + 1 else _interval(t, duration)
        longest, previous = max(longest, current), t
    return longest


def _possible_alive(samples, duration, exempt=frozenset()):
    # Unobserved seconds could be alive. The upper denominator makes a reported
    # threshold violation a lower bound even when tracks have gaps.
    return duration - sum(_interval(t, duration) for t in range(math.ceil(duration))
                          if t in exempt or t in samples and samples[t]["hp"] == 0)


def _near(a, b, distance):
    # Positions are tenths of a yard, orientation is radians times one hundred.
    return (a["xs"] - b["xs"]) ** 2 + (a["ys"] - b["ys"]) ** 2 <= (distance * 10) ** 2


def _restore(conn, row):
    """Reconstitute the persisted protocol, preserving arrival-order deaths."""
    members, roles = [], {}
    for m in store.member_rows(conn, row["id"]):
        detail = json.loads(m["detail"])
        members.append(p.PullMember(ts=row["end_ms"], pull=row["id"], guid=m["guid"],
            class_=m["class"], spec_tab=m["spec_tab"], tank=detail["tank"],
            **{k: m[k] for k in ("ilvl", "damage", "damage_boss", "healing", "overheal", "taken",
                                "deaths", "interrupts", "active_ms")},
            done=tuple(p.SpellTotal(**x) for x in detail["done"]),
            taken_by=tuple(p.TakenTotal(**x) for x in detail["taken_by"]),
            debuffs=tuple(p.DebuffTotal(**x) for x in detail["debuffs"]),
            casts=tuple(p.CastTotal(**x) for x in detail["casts"])))
        roles[m["guid"]] = m["role"]
    deaths = tuple(p.PullDeath(ts=row["end_ms"], pull=row["id"],
        **{k: d[k] for k in ("guid", "at_ms", "spell", "source", "x", "y")},
        hits=tuple(p.LastHit(**x) for x in json.loads(d["hits"])))
        for d in store.death_rows(conn, row["id"]))
    tracks, casts = {}, []
    for t in store.track_rows(conn, row["id"]):
        data = json.loads(t["data"])
        casts.extend(p.TrackCast(**x) for x in data.pop("casts", ()))
        tracks[t["who"], t["entry"]] = data
    header = p.Pull(ts=row["end_ms"],
        **{k: row[k] for k in ("id", "map", "instance_id", "boss_id", "difficulty", "size", "outcome",
                              "start_ms", "end_ms", "members", "deaths", "boss_entry", "upd_mean_ms",
                              "damage_basis", "healing_basis", "spell_basis", "interrupt_basis")},
        tracks=len(tracks), assisted=bool(row["assisted"]),
        source="benchmark" if row["kind"] == "benchmark" else "tracked")
    return PullRecord(header, tuple(members), deaths, tracks, tuple(casts), bool(row["complete"])), roles


async def rescore(ports, book: SpellBook, *, since_ms=None, playbooks: bool = False) -> int:
    """Recheck eligible stored pulls; pruned tracks stay explicitly unavailable."""
    updated = 0
    for row in store.pull_rows(ports.conn, since_ms=since_ms, scoreable=True):
        if row["checks_version"] >= checks_version(playbooks):
            continue
        record, roles = _restore(ports.conn, row)
        if not record.complete or record.header.outcome == "aborted" or not is_raid_fight(record.header.map, record.header.boss_id):
            continue
        result = run_checks(record, roles, book, playbooks=playbooks)
        store.set_checks(ports.conn, row["id"], result.failures, result.causes,
                         deaths=len(record.deaths), failed=result.failed_mechanics, version=result.version,
                         complete=result.complete, coverage=result.coverage)
        updated += 1
    return updated
