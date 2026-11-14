"""`python -m leeroy_mind raidplay show|check|dbm-extract`. show and check only read; dbm-extract
writes one facts file in the data directory. None of them touch mind.db, the bridge or a model."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import check, dbm
from .model import DIFFICULTIES, Boss, Playbooks, PlaybookError, Raid, load

log = logging.getLogger("leeroy_mind")
REPO_ROOT = Path(__file__).resolve().parents[3]


def _raid(playbooks: Playbooks, text: str) -> Raid | None:
    for raid in playbooks.raids.values():
        if text.casefold() in (str(raid.map_id), raid.key, raid.name.casefold()):
            return raid
    return None


def _boss(raid: Raid, text: str) -> Boss | None:
    for boss in raid.bosses:
        if text.casefold() in (str(boss.index), boss.slug, boss.name.casefold()):
            return boss
    return None


def _number(boss_or_mech, name: str, difficulty: str) -> str:
    value = boss_or_mech.number(name, difficulty) if hasattr(boss_or_mech, "number") else None
    return "" if value is None else f"{value:g}"


def boss_lines(boss: Boss, difficulty: str | None) -> list[str]:
    head = f"{boss.map_id}:{boss.index} {boss.name} ({boss.slug})"
    enrage = boss.enrage.get(difficulty) if difficulty else None
    lines = [head + (f" enrage {enrage:g}s" if enrage else "")]
    if boss.call:
        lines.append(f"  call: {boss.call}")
    lines.append("  phases: " + ", ".join(p.id for p in boss.phases))
    for m in boss.mechanics:
        if difficulty and not m.applies(difficulty):
            continue
        ids = m.spell_ids(difficulty) if difficulty else sorted(m.all_ids())
        numbers = " ".join(f"{n}={_number(m, n, difficulty or m.only[0])}" for n in m.numbers
                           if _number(m, n, difficulty or m.only[0]))
        lines.append(f"  - {m.id} [{m.kind}] {m.name}; roles {'/'.join(m.roles)}; phase {m.phase}; "
                     f"handled by {m.handled_by}; check {m.check}")
        lines.append(f"      spells {', '.join(map(str, ids))}" + (f"; {numbers}" if numbers else "")
                     + ("" if m.only == boss_only(boss) else f"; only {', '.join(m.only)}"))
        lines.append(f"      {m.explain}")
    for c in boss.checks:
        lines.append(f"  check {c.id} [{c.kind}] since v{c.since}")
    return lines


def boss_only(boss: Boss) -> tuple[str, ...]:
    keys = {d for m in boss.mechanics for d in m.only}
    return tuple(d for d in DIFFICULTIES.values() if d in keys)


def boss_json(boss: Boss, difficulty: str | None) -> dict:
    return {"map": boss.map_id, "index": boss.index, "name": boss.name, "slug": boss.slug,
            "creatures": list(boss.creatures), "enrage": dict(boss.enrage), "call": boss.call,
            "melee_behind": boss.melee_behind, "phases": [p.__dict__ for p in boss.phases],
            "mechanics": [{"id": m.id, "name": m.name, "kind": m.kind, "phase": m.phase,
                           "roles": list(m.roles), "only": list(m.only), "handled_by": m.handled_by,
                           "playerbots": m.playerbots, "check": m.check, "explain": m.explain,
                           "spells": ({difficulty: list(m.spell_ids(difficulty))} if difficulty
                                      else {k: list(v) for k, v in m.spells.items()}),
                           "hits": {k: list(v) for k, v in m.hits.items()},
                           "numbers": {k: dict(v) for k, v in m.numbers.items()}}
                          for m in boss.mechanics if not difficulty or m.applies(difficulty)],
            "checks": [c.__dict__ | {"names": list(c.names), "entries": list(c.entries),
                                     "roles": list(c.roles), "exempt": list(c.exempt)}
                       for c in boss.checks]}


def _show(args) -> int:
    playbooks = load()
    raid = _raid(playbooks, args.map)
    if raid is None:
        print(f"raidplay: no playbook for {args.map}; covered: "
              + ", ".join(f"{r.map_id} {r.key}" for r in playbooks.raids.values()))
        return 1
    difficulty = DIFFICULTIES.get(args.difficulty) if args.difficulty is not None else None
    if args.difficulty is not None and difficulty not in raid.difficulties:
        print(f"raidplay: {raid.name} has no difficulty {args.difficulty}")
        return 1
    bosses = raid.bosses
    if args.boss is not None:
        boss = _boss(raid, args.boss)
        if boss is None:
            print(f"raidplay: {raid.name} has no boss {args.boss}")
            return 1
        bosses = (boss,)
    if args.json:
        print(json.dumps([boss_json(b, difficulty) for b in bosses], indent=2, sort_keys=True))
    else:
        print(f"{raid.name} ({raid.map_id}, {raid.key}): {len(raid.bosses)} bosses, "
              f"difficulties {', '.join(raid.difficulties)}")
        for boss in bosses:
            print("\n".join(boss_lines(boss, difficulty)))
    return 0


def _book_for(data_dir: Path, dbc: Path | None):
    """The client spell book from Spell.dbc only: never the world database or its password."""
    from leeroy_mind.training.spells import load_spellbook
    if dbc is None:
        from leeroy_mind.guild.config import load_guild_config
        from leeroy_mind.library.world import dbc_dir_of
        dbc = dbc_dir_of(load_guild_config(data_dir / "mind.toml").worldserver_conf)
    if dbc is None or not (Path(dbc) / "Spell.dbc").is_file():
        return None
    book = load_spellbook(Path(dbc))
    return book if "Spell.dbc" in book.loaded else None


def _check(args, data_dir: Path) -> int:
    playbooks = load()
    book = None
    if not args.no_book:
        try:
            book = _book_for(data_dir, Path(args.dbc) if args.dbc else None)
        except (OSError, ValueError) as exc:
            print(f"raidplay check: spell book unavailable ({type(exc).__name__}); --no-book skips it")
            return 2
        if book is None:
            print("raidplay check: spell book unavailable (no Spell.dbc); --dbc DIR or --no-book")
            return 2
    dbm_path = Path(args.dbm) if args.dbm else data_dir / "raidplay" / "dbm.json"
    facts = dbm.read(dbm_path) if dbm_path.is_file() else None
    found = check.validate(playbooks, book, dbm=facts)
    errors = [f for f in found if f.level == "error"]
    summary = {"raids": len(playbooks.raids), "bosses": len(playbooks.bosses()),
               "mechanics": len(playbooks.mechanics()), "spell_book": book is not None,
               "dbm": facts is not None, "errors": len(errors), "notes": len(found) - len(errors)}
    if args.json:
        print(json.dumps({"summary": summary, "findings": [f.__dict__ for f in found]}, indent=2))
    else:
        for f in found:
            if f.level == "error" or args.notes:
                print(f"{f.level} {f.code} {f.ref} {f.detail}".rstrip())
        print(" ".join(f"{k}={v}" for k, v in summary.items()))
    log.info("raidplay_check errors=%d notes=%d", len(errors), len(found) - len(errors))
    return 1 if errors else 0


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _extract(args, data_dir: Path) -> int:
    addons = Path(args.addons)
    if not addons.is_dir():
        print("raidplay dbm-extract: --addons must be an existing Interface/AddOns folder")
        return 2
    out = (Path(args.out) if args.out else data_dir / "raidplay" / "dbm.json").resolve()
    if _inside(out, REPO_ROOT) and not _inside(out, data_dir.resolve()):
        print("raidplay dbm-extract: refusing to write inside the git checkout; DBM facts stay local")
        return 2
    facts = dbm.extract(addons)
    dbm.write(facts, out)
    print(f"raidplay dbm-extract: {len(facts['mods'])} boss mods, "
          f"{sum(len(m['spells']) for m in facts['mods'])} spell ids -> {out}")
    return 0


def raidplay_main(argv=None) -> int:
    from leeroy_mind.__main__ import _add_data_dir, _root
    parser = argparse.ArgumentParser(prog="leeroy_mind raidplay")
    _add_data_dir(parser)
    subs = parser.add_subparsers(dest="command", required=True)
    show = subs.add_parser("show", help="print a raid's or a boss's playbook")
    show.add_argument("map", help="map id or raid key (533, naxx, 631, icc, 649, toc)")
    show.add_argument("boss", nargs="?", help="boss index, slug or name")
    show.add_argument("--difficulty", type=int, choices=sorted(DIFFICULTIES),
                      help="0 10-man, 1 25-man, 2 10 heroic, 3 25 heroic")
    show.add_argument("--json", action="store_true")
    _add_data_dir(show, after_action=True)
    chk = subs.add_parser("check", help="validate the playbooks against the spell book")
    chk.add_argument("--dbc", help="the server's dbc folder (default: DataDir from worldserver.conf)")
    chk.add_argument("--no-book", action="store_true", help="skip the spell book")
    chk.add_argument("--dbm", help="local DBM facts (default: <data-dir>/raidplay/dbm.json if present)")
    chk.add_argument("--notes", action="store_true", help="print notes as well as errors")
    chk.add_argument("--json", action="store_true")
    _add_data_dir(chk, after_action=True)
    ext = subs.add_parser("dbm-extract", help="facts from an installed DBM into the data dir")
    ext.add_argument("--addons", required=True, help="the client's Interface/AddOns folder")
    ext.add_argument("--out", help="output JSON (default: <data-dir>/raidplay/dbm.json)")
    _add_data_dir(ext, after_action=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            return _show(args)
        data_dir = _root(args)
        return _check(args, data_dir) if args.command == "check" else _extract(args, data_dir)
    except PlaybookError as exc:
        print(f"raidplay: the playbook data is invalid: {exc}")
        return 1
    except OSError as exc:
        print(f"raidplay: {args.command} unavailable ({type(exc).__name__})")
        return 2
