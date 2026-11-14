"""What hurt them, per boss: read-only arithmetic over stored pulls.

Numbers, guids and spell ids with their spell-book names only; never chat text.
Each context (MAP:BOSS:DIFFICULTY) reports its deaths by the killing blow's
spell, the spells that hit cast members hardest, the failed mechanics the checks
proved (``tg_failure``) and, for the boss's avoidable effects, which members the
records show being hit and how often past the mechanic's allowance.

The bases stay labelled. Deaths are observed; a killing spell is the recorded
killing blow's tag, attributed best effort (spell 0 is melee or untagged).
Damage amounts are requested before the core's final adjustments, not health
lost, so shares rank spells rather than measure them. An avoidable hit is a
recorded hit by that effect's spell, not proof a member stood in it. Only
complete killed or wiped pulls are read; incomplete ones are counted, not used.

The mind keeps the names of the spells its stored pulls name in meta
``tg:spell_names`` (``remember_names``), so a reader without the client's
Spell.dbc (the console) can still name them.
"""

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping

from leeroy_mind.wow import boss_name, is_raid_fight

from .mechanics import BOSS_MECHANICS

NAMES_KEY = "tg:spell_names"
MAX_NAMES = 4096
MAX_CONTEXTS = 60
MAX_PULLS = 500
TOP = 10
KINDS = ("his", "offline")
ALL_KINDS = ("his", "offline", "benchmark")
MELEE = "melee or untagged"
EVIDENCE = {
    "deaths": "observed deaths; the killing spell is the recorded killing blow's tag, attributed best effort",
    "damage_taken": "requested damage before the core's final adjustments, not health lost; shares rank spells",
    "avoidable": "recorded hits by an avoidable effect's spell (best-effort attribution), not proof a member stood in it",
    "mechanics": "failures the checks proved; checks that could not run are listed under coverage, never a pass",
}


def _json(value, default):
    try:
        parsed = json.loads(value) if isinstance(value, str) else None
    except (TypeError, ValueError, RecursionError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _int(value):
    return type(value) is int and value >= 0


def _taken(detail) -> list[tuple[int, int, int]]:
    """(spell, amount, hits) of a member's recorded damage taken; malformed rows are skipped."""
    rows = _json(detail, {}).get("taken_by")
    out = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict) and _int(row.get("spell")) and _int(row.get("amount")) and _int(row.get("hits")):
            out.append((row["spell"], row["amount"], row["hits"]))
    return out


def referenced_spells(conn, pulls: Iterable[str] | None = None) -> set[int]:
    """Every spell a stored pull names: killing blows, last hits and damage taken."""
    where, params = "", ()
    if pulls is not None:
        pulls = tuple(pulls)
        if not pulls:
            return set()
        where, params = " WHERE pull IN (" + ",".join("?" * len(pulls)) + ")", pulls
    spells = set()
    for spell, hits in conn.execute("SELECT spell, hits FROM tg_death" + where, params):
        if _int(spell):
            spells.add(spell)
        for hit in _json(hits, []):
            if isinstance(hit, dict) and _int(hit.get("spell")):
                spells.add(hit["spell"])
    for (detail,) in conn.execute("SELECT detail FROM tg_member" + where, params):
        spells.update(spell for spell, _, _ in _taken(detail))
    spells.discard(0)
    return spells


def stored_names(conn) -> dict[int, str]:
    raw = conn.execute("SELECT value FROM meta WHERE key=?", (NAMES_KEY,)).fetchone()
    names = _json(raw[0] if raw else None, {})
    return {int(k): v for k, v in names.items()
            if isinstance(k, str) and k.isdecimal() and isinstance(v, str) and 0 < len(v) <= 200}


def remember_names(conn, book, spells: Iterable[int]) -> int:
    """Add the spell book's names of ``spells`` to meta tg:spell_names (at most MAX_NAMES).

    The mind's own write, in one transaction; returns how many names were added. A spell
    the book does not know is left out rather than stored as a placeholder."""
    known = stored_names(conn)
    added = {}
    for spell in sorted(set(spells) - set(known)):
        if len(known) + len(added) >= MAX_NAMES:
            break
        info = book.info(spell) if book is not None else None
        if info is not None and info.name and len(info.name) <= 200:
            added[spell] = info.name
    if added:
        merged = {str(k): v for k, v in sorted({**known, **added}.items())}
        with conn:
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE "
                         "SET value=excluded.value", (NAMES_KEY, json.dumps(merged, separators=(",", ":"))))
    return len(added)


class SpellNames:
    """Spell ids to names and avoidable effects' names to ids: the spell book when it loaded,
    else the names the mind stored."""

    def __init__(self, book=None, stored: Mapping[int, str] | None = None):
        self.book = book if book is not None and "Spell.dbc" in getattr(book, "loaded", ()) else None
        self.stored = dict(stored or {})
        self.source = ("spell_book" if self.book is not None
                       else "stored_names" if self.stored else "unavailable")
        self._by_name = defaultdict(set)
        for spell, name in self.stored.items():
            self._by_name[name.casefold()].add(spell)

    def name(self, spell: int) -> str | None:
        if spell == 0:
            return MELEE
        if self.book is not None:
            info = self.book.info(spell)
            if info is not None:
                return info.name
        return self.stored.get(spell)

    def ids(self, name: str) -> frozenset[int]:
        if self.book is not None:
            return self.book.ids(name)
        return frozenset(self._by_name.get(name.casefold(), ()))


def _context_ok(context):
    try:
        map_id, boss_id, difficulty = map(int, context.split(":"))
    except (ValueError, AttributeError):
        return False
    return context == f"{map_id}:{boss_id}:{difficulty}" and 0 <= difficulty <= 3 and is_raid_fight(map_id, boss_id)


def _matches(context, wanted):
    if wanted is None:
        return True
    want_map, want_boss, want_diff = wanted.split(":")
    have_map, have_boss, have_diff = context.split(":")
    return have_map == want_map and have_diff == want_diff and want_boss in ("*", have_boss)


def _human(conn):
    """The owner's guid as the last hello stored it (meta human_guid), else 0."""
    row = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    try:
        value = int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _top(counter, limit, key):
    return sorted(counter.items(), key=key)[:limit]


def hurt_report(conn, *, context: str | None = None, last: int | None = None,
                kinds: Iterable[str] = KINDS, spells: SpellNames | None = None,
                names: Mapping[int, str] | None = None, top: int = TOP,
                max_contexts: int = MAX_CONTEXTS) -> dict:
    """Per context, newest first. ``context`` is one MAP:BOSS:DIFFICULTY or MAP:*:DIFFICULTY;
    ``last`` keeps each context's newest N pulls; ``names`` adds members' names to their guids."""
    kinds = tuple(k for k in ALL_KINDS if k in set(kinds))
    spells = spells or SpellNames()
    names = names or {}
    owner = _human(conn)
    keep = min(MAX_PULLS, last) if type(last) is int and last > 0 else MAX_PULLS
    result = dict(contexts=[], kinds=list(kinds), last=last, spell_names=spells.source,
                  evidence=dict(EVIDENCE), truncated=[])
    if not kinds:
        return result
    marks = ",".join("?" * len(kinds))
    pulls = defaultdict(list)
    incomplete = Counter()
    for row in conn.execute(
            "SELECT id, context, map, boss_id, difficulty, size, outcome, start_ms, end_ms, kind, complete, "
            "checks_complete, checks_coverage, damage_basis, spell_basis FROM tg_pull "
            f"WHERE kind IN ({marks}) ORDER BY start_ms DESC, id DESC", kinds):
        if not _context_ok(row["context"]) or not _matches(row["context"], context):
            continue
        if not row["complete"] or row["outcome"] not in ("killed", "wiped"):
            incomplete[row["context"]] += 1
            continue
        if len(pulls[row["context"]]) < keep:
            pulls[row["context"]].append(row)
        elif (last is None or last > keep) and "pulls" not in result["truncated"]:
            result["truncated"].append("pulls")  # the cap, not the asked-for --last, cut it
    order = sorted(pulls, key=lambda c: (-pulls[c][0]["start_ms"], c))
    if len(order) > max_contexts:
        result["truncated"].append("contexts")
    for ctx in order[:max_contexts]:
        result["contexts"].append(_context(conn, ctx, pulls[ctx], incomplete[ctx], spells, names, owner, top))
    return result


def _member(guid, names):
    entry = {"guid": guid}
    if guid in names:
        entry["name"] = names[guid]
    return entry


def _context(conn, ctx, rows, incomplete, spells, names, owner, top):
    first = rows[0]
    map_id, boss_id, difficulty = map(int, ctx.split(":"))
    ids = [r["id"] for r in rows]
    marks = ",".join("?" * len(ids))
    roles, taken, seen = {}, defaultdict(lambda: [0, 0, set(), set()]), set()
    per_pull_hits = defaultdict(Counter)  # (pull, guid) -> spell -> hits
    amounts = defaultdict(Counter)
    for m in conn.execute(f"SELECT pull, guid, role, detail FROM tg_member WHERE pull IN ({marks})", ids):
        if m["guid"] == owner:
            continue  # the owner is not a cast member; his own hits are not theirs
        seen.add(m["guid"])
        roles[m["pull"], m["guid"]] = m["role"]
        for spell, amount, hits in _taken(m["detail"]):
            entry = taken[spell]
            entry[0] += amount
            entry[1] += hits
            entry[2].add(m["guid"])
            entry[3].add(m["pull"])
            per_pull_hits[m["pull"], m["guid"]][spell] += hits
            amounts[m["pull"], m["guid"]][spell] += amount
    total_taken = sum(e[0] for e in taken.values())

    deaths = defaultdict(lambda: [0, set(), set()])
    causes = Counter()
    death_total = 0
    for d in conn.execute(f"SELECT pull, guid, spell, cause FROM tg_death WHERE pull IN ({marks})", ids):
        if d["guid"] == owner:
            continue
        death_total += 1
        spell = d["spell"] if _int(d["spell"]) else 0
        deaths[spell][0] += 1
        deaths[spell][1].add(d["guid"])
        deaths[spell][2].add(d["pull"])
        causes[d["cause"] if isinstance(d["cause"], str) else "unknown"] += 1

    failures = defaultdict(lambda: [0, set(), set(), 0.0])
    by_member = defaultdict(Counter)
    for f in conn.execute(f"SELECT pull, kind, key, guid, count, deaths FROM tg_failure "
                          f"WHERE pull IN ({marks}) AND count>0", ids):
        entry = failures[f["kind"], f["key"]]
        entry[0] += f["count"]
        entry[2].add(f["pull"])
        entry[3] += f["deaths"] if type(f["deaths"]) in (int, float) else 0
        if f["guid"]:
            entry[1].add(f["guid"])
            by_member[f["kind"], f["key"]][f["guid"]] += f["count"]

    coverage = set()
    for r in rows:
        reasons = _json(r["checks_coverage"], [])
        coverage.update(x for x in reasons if isinstance(x, str))
        if not r["checks_complete"] and not reasons:
            coverage.add("coverage_unavailable")

    avoidable = []
    for mech in BOSS_MECHANICS.get((map_id, boss_id), ()):
        if mech.kind != "avoid":
            continue
        wanted = set()
        for name in mech.names:
            wanted |= spells.ids(name)
        hits = defaultdict(lambda: [0, 0, set(), 0])  # guid -> hits, over, pulls, amount
        for (pull, guid), counter in per_pull_hits.items():
            if mech.roles and roles.get((pull, guid)) not in mech.roles:
                continue
            count = sum(counter[s] for s in wanted)
            if not count:
                continue
            entry = hits[guid]
            entry[0] += count
            entry[1] += max(0, count - mech.max_hits)
            entry[2].add(pull)
            entry[3] += sum(amounts[pull, guid][s] for s in wanted)
        members = [dict(_member(g, names), hits=h, over_allowance=o, pulls=len(p), amount=a)
                   for g, (h, o, p, a) in sorted(hits.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))]
        avoidable.append(dict(names=list(mech.names), spells=sorted(wanted), roles=list(mech.roles),
                              allowance_per_pull=mech.max_hits, resolved=bool(wanted),
                              hits=sum(m["hits"] for m in members), members=members[:top]))

    kills = sum(r["outcome"] == "killed" for r in rows)
    return dict(
        context=ctx, boss=boss_name(map_id, boss_id), map=map_id, boss_id=boss_id, difficulty=difficulty,
        size=first["size"], pulls=len(rows), kills=kills, wipes=len(rows) - kills,
        by_kind=dict(sorted(Counter(r["kind"] for r in rows).items())),
        incomplete_pulls=incomplete, first_ms=rows[-1]["start_ms"], last_ms=first["start_ms"],
        members=len(seen),
        bases=dict(damage=sorted({r["damage_basis"] for r in rows}), spell=sorted({r["spell_basis"] for r in rows})),
        deaths=dict(total=death_total, per_pull=round(death_total / len(rows), 3),
                    by_spell=[dict(spell=s, name=spells.name(s), deaths=n, members=len(g), pulls=len(p))
                              for s, (n, g, p) in _top(deaths, top, lambda kv: (-kv[1][0], kv[0]))],
                    by_cause=dict(sorted(causes.items()))),
        damage_taken=dict(total=total_taken, top=[
            dict(spell=s, name=spells.name(s), amount=a, hits=h, members=len(g), pulls=len(p),
                 share=round(a / total_taken, 4) if total_taken else None)
            for s, (a, h, g, p) in _top(taken, top, lambda kv: (-kv[1][0], kv[0]))]),
        mechanics=[dict(kind=k, key=key, count=c, pulls=len(p), members=len(g), deaths=round(d, 3),
                        worst=[dict(_member(guid, names), count=n)
                               for guid, n in sorted(by_member[k, key].items(), key=lambda kv: (-kv[1], kv[0]))[:5]])
                   for (k, key), (c, g, p, d) in _top(failures, top, lambda kv: (-kv[1][0], kv[0]))],
        avoidable=avoidable,
        checks=dict(complete_pulls=sum(bool(r["checks_complete"]) for r in rows), coverage=sorted(coverage)))
