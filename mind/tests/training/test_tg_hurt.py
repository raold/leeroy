import hashlib
import json
from types import SimpleNamespace

import pytest

from leeroy_mind.training import cli, protocol as p, store
from leeroy_mind.training.hurt import (MARK_KEY, MELEE, NAMES_KEY, SpellNames, avoidable_spells, hurt_report,
                                       name_new_pulls, referenced_spells, remember_names, stored_names)

VILE_GAS, GAS_SPORE, SLIME = 900, 901, 902
NAMES = {VILE_GAS: "Vile Gas", GAS_SPORE: "Gas Spore", SLIME: "Slime Spray"}
OWNER = 1


def book(names=NAMES):
    return SimpleNamespace(
        loaded=frozenset({"Spell.dbc"}),
        info=lambda spell: SimpleNamespace(name=names[spell]) if spell in names else None,
        ids=lambda name: frozenset(s for s, n in names.items() if n.casefold() == name.casefold()))


def member(pull, guid, taken=(), *, tank=False):
    return p.PullMember(0, pull, guid, 1, 0, tank, 232, 0, 0, 0, 0, sum(a for _, a, _ in taken), 0, 0, 0,
                        (), tuple(p.TakenTotal(s, 0, a, h) for s, a, h in taken), (), ())


def save(fake, ident, *, boss=4, start, outcome="wiped", kind="his", members=(), deaths=(),
         complete=True, failures=()):
    head = p.Pull(0, ident, 631, 77, boss, 0, 10, outcome, start, start + 120_000, len(members),
                  len(deaths), 0, False, "tracked", 36626, 30, damage_basis="requested_pre_ai",
                  spell_basis="best_effort_thread_pair")
    rows = tuple(member(ident, guid, taken) for guid, _, taken in members)
    dead = tuple(p.PullDeath(0, ident, guid, 1000, spell, 36626, 0, 0, (p.LastHit(990, spell, 36626, 5, 5),))
                 for guid, spell in deaths)
    rec = SimpleNamespace(header=head, members=rows, deaths=dead, tracks={}, casts=(), complete=complete)
    ctx = SimpleNamespace(kind=kind, lead_guid=OWNER, raid_id=None, card_id=None, arm_id=None, change="",
                          covariates={})
    store.put_pull(fake.conn, rec, ctx, {guid: role for guid, role, _ in members}, received=start / 1000)
    if failures or deaths:
        store.set_checks(fake.conn, ident, [dict(kind=k, key=key, guid=g, count=c, deaths=0, damage_share=0.0)
                                            for k, key, g, c in failures],
                         ["unknown"] * len(deaths), deaths=len(deaths), failed=len(failures), version=1,
                         coverage=("avoid:unverified_damage",))


@pytest.fixture
def pulls(fake):
    with fake.conn:
        fake.conn.execute("INSERT INTO meta(key,value) VALUES ('human_guid','1')")
    # Festergut 10: a wipe, then a kill. The owner's own hits and death are not the cast's.
    save(fake, "1000-77-4", start=1_000_000, outcome="wiped",
         members=((101, "ranged", ((VILE_GAS, 3000, 3), (GAS_SPORE, 1000, 1))),
                  (102, "melee", ((VILE_GAS, 1000, 1),)),
                  (103, "tank", ((0, 20000, 40),)),
                  (OWNER, "ranged", ((VILE_GAS, 9999, 9),))),
         deaths=((101, VILE_GAS), (102, 0), (OWNER, VILE_GAS)),
         failures=(("debuff", "Gas Spore", 101, 2),))
    save(fake, "2000-77-4", start=2_000_000, outcome="killed",
         members=((101, "ranged", ((VILE_GAS, 1000, 1),)), (103, "tank", ((0, 10000, 20),))))
    save(fake, "2500-77-4", start=2_500_000, outcome="aborted", members=((101, "ranged", ()),))
    save(fake, "2600-77-4", start=2_600_000, kind="benchmark",
         members=((201, "ranged", ((VILE_GAS, 50000, 50),)),), deaths=((201, VILE_GAS),))
    # Rotface 10, from an offline raid.
    save(fake, "3000-77-5", boss=5, start=3_000_000, outcome="killed", kind="offline",
         members=((104, "ranged", ((SLIME, 4000, 2),)),))
    return fake


def by_context(report):
    return {c["context"]: c for c in report["contexts"]}


def test_report_numbers_from_stored_pulls(pulls):
    report = hurt_report(pulls.conn, spells=SpellNames(book()), names={101: "Testa", 103: "Testo"})
    assert [c["context"] for c in report["contexts"]] == ["631:5:0", "631:4:0"]  # newest first
    fester = by_context(report)["631:4:0"]
    assert (fester["boss"], fester["pulls"], fester["kills"], fester["wipes"]) == ("Festergut", 2, 1, 1)
    assert fester["by_kind"] == {"his": 2} and fester["incomplete_pulls"] == 1 and fester["members"] == 3
    assert fester["bases"] == {"damage": ["requested_pre_ai"], "spell": ["best_effort_thread_pair"]}
    deaths = fester["deaths"]
    assert deaths["total"] == 2 and deaths["per_pull"] == 1.0 and deaths["by_cause"] == {"unknown": 2}
    assert deaths["by_spell"] == [
        dict(spell=0, name=MELEE, deaths=1, members=1, pulls=1),
        dict(spell=VILE_GAS, name="Vile Gas", deaths=1, members=1, pulls=1)]
    taken = fester["damage_taken"]
    assert taken["total"] == 36000
    assert taken["top"] == [
        dict(spell=0, name=MELEE, amount=30000, hits=60, members=1, pulls=2, share=0.8333),
        dict(spell=VILE_GAS, name="Vile Gas", amount=5000, hits=5, members=2, pulls=2, share=0.1389),
        dict(spell=GAS_SPORE, name="Gas Spore", amount=1000, hits=1, members=1, pulls=1, share=0.0278)]
    assert fester["mechanics"] == [dict(kind="debuff", key="Gas Spore", count=2, pulls=1, members=1, deaths=0,
                                        worst=[dict(guid=101, name="Testa", count=2)])]
    # Vile Gas is ranged-only with one hit allowed a pull: the melee's hit and the owner's are not counted.
    [vile] = fester["avoidable"]
    assert vile["names"] == ["Vile Gas"] and vile["spells"] == [VILE_GAS] and vile["resolved"]
    assert (vile["roles"], vile["allowance_per_pull"], vile["hits"]) == (["ranged"], 1, 4)
    assert vile["members"] == [dict(guid=101, name="Testa", hits=4, over_allowance=2, pulls=2, amount=4000)]
    assert fester["checks"] == {"complete_pulls": 0, "coverage": ["avoid:unverified_damage", "coverage_unavailable"]}
    assert report["spell_names"] == "spell_book" and set(report["evidence"]) == {
        "deaths", "damage_taken", "avoidable", "mechanics"}


def test_last_context_and_kind_filters(pulls):
    newest = by_context(hurt_report(pulls.conn, last=1, spells=SpellNames(book())))["631:4:0"]
    assert (newest["pulls"], newest["kills"], newest["deaths"]["total"]) == (1, 1, 0)
    assert newest["avoidable"][0]["members"][0]["over_allowance"] == 0
    only = hurt_report(pulls.conn, context="631:4:0")
    assert [c["context"] for c in only["contexts"]] == ["631:4:0"]
    assert [c["context"] for c in hurt_report(pulls.conn, context="631:*:0")["contexts"]] == ["631:5:0", "631:4:0"]
    offline = hurt_report(pulls.conn, kinds=("offline",))
    assert [c["context"] for c in offline["contexts"]] == ["631:5:0"]
    bench = by_context(hurt_report(pulls.conn, kinds=("benchmark",), spells=SpellNames(book())))["631:4:0"]
    assert bench["by_kind"] == {"benchmark": 1} and bench["deaths"]["total"] == 1


def test_names_come_from_the_store_without_the_spell_book(pulls):
    report = hurt_report(pulls.conn)
    fester = by_context(report)["631:4:0"]
    assert report["spell_names"] == "unavailable"
    assert fester["damage_taken"]["top"][1]["name"] is None
    assert not fester["avoidable"][0]["resolved"] and fester["avoidable"][0]["hits"] == 0
    assert referenced_spells(pulls.conn) == {VILE_GAS, GAS_SPORE, SLIME}
    assert referenced_spells(pulls.conn, ["2000-77-4"]) == {VILE_GAS}
    assert remember_names(pulls.conn, book({VILE_GAS: "Vile Gas"}), referenced_spells(pulls.conn)) == 1
    assert remember_names(pulls.conn, book(), referenced_spells(pulls.conn)) == 2
    assert remember_names(pulls.conn, book(), referenced_spells(pulls.conn)) == 0
    assert stored_names(pulls.conn) == NAMES
    report = hurt_report(pulls.conn, spells=SpellNames(None, stored_names(pulls.conn)))
    fester = by_context(report)["631:4:0"]
    assert report["spell_names"] == "stored_names"
    assert fester["damage_taken"]["top"][1]["name"] == "Vile Gas"
    assert fester["avoidable"][0]["resolved"] and fester["avoidable"][0]["hits"] == 4
    # A book without Spell.dbc is no book: the stored names stand.
    empty = SimpleNamespace(loaded=frozenset(), info=lambda spell: None, ids=lambda name: frozenset())
    assert SpellNames(empty, stored_names(pulls.conn)).source == "stored_names"


def test_the_owner_is_not_counted_in_failed_mechanics(pulls):
    store.set_checks(pulls.conn, "2000-77-4", [dict(kind="debuff", key="Gas Spore", guid=OWNER, count=5, deaths=0,
                                                    damage_share=0.0)], [], deaths=0, failed=1, version=1)
    [gas] = by_context(hurt_report(pulls.conn))["631:4:0"]["mechanics"]
    assert (gas["count"], gas["members"], gas["pulls"]) == (2, 1, 1)
    assert [w["guid"] for w in gas["worst"]] == [101]


def test_avoidable_effects_named_from_the_book_even_when_nobody_was_hit(pulls):
    # A Marrowgar pull where nobody stood in Coldflame or Bone Storm.
    save(pulls, "5000-77-0", boss=0, start=5_000_000, outcome="killed", members=((101, "ranged", ()),))
    names = {**NAMES, 950: "Coldflame", 952: "Bone Storm"}
    remember_names(pulls.conn, book(names), referenced_spells(pulls.conn))  # hit spells only
    marrowgar = by_context(hurt_report(pulls.conn, spells=SpellNames(None, stored_names(pulls.conn))))["631:0:0"]
    assert [m["resolved"] for m in marrowgar["avoidable"]] == [False, False]
    assert {900, 902, 950, 952} == avoidable_spells(book(names))
    remember_names(pulls.conn, book(names), avoidable_spells(book(names)))
    marrowgar = by_context(hurt_report(pulls.conn, spells=SpellNames(None, stored_names(pulls.conn))))["631:0:0"]
    assert [(m["names"], m["resolved"], m["hits"]) for m in marrowgar["avoidable"]] == [
        (["Coldflame"], True, 0), (["Bone Storm"], True, 0)]


def test_the_naming_pass_reads_only_new_pulls(pulls):
    assert name_new_pulls(pulls.conn, book(), batch=2) == 2
    assert name_new_pulls(pulls.conn, book(), batch=10) == 3
    assert name_new_pulls(pulls.conn, book()) == 0
    mark = pulls.conn.execute("SELECT value FROM meta WHERE key=?", (MARK_KEY,)).fetchone()[0]
    assert int(mark) == pulls.conn.execute("SELECT max(rowid) FROM tg_pull").fetchone()[0]
    assert stored_names(pulls.conn) == NAMES
    save(pulls, "4000-77-4", start=4_000_000, members=((101, "ranged", ((960, 1, 1),)),))
    assert name_new_pulls(pulls.conn, book({**NAMES, 960: "Gaseous Blight"})) == 1
    assert stored_names(pulls.conn)[960] == "Gaseous Blight"


def test_corrupt_stored_names_and_details_are_skipped(pulls):
    with pulls.conn:
        pulls.conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (NAMES_KEY, "[1,2]"))
        pulls.conn.execute("UPDATE tg_member SET detail='not json' WHERE guid=102")
    assert stored_names(pulls.conn) == {}
    fester = by_context(hurt_report(pulls.conn, spells=SpellNames(book())))["631:4:0"]
    assert fester["damage_taken"]["total"] == 35000


def test_cli_hurt_is_read_only(pulls, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_spell_book", lambda root: book())
    path = tmp_path / "mind.db"
    before = hashlib.sha256(path.read_bytes()).digest()
    assert cli.training_main(["hurt", "--context", "631:4:0", "--last", "5", "--data-dir", str(tmp_path)]) == 0
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not (tmp_path / "mind.lock").exists()
    result = json.loads(capsys.readouterr().out)
    [fester] = result["contexts"]
    assert fester["deaths"]["by_spell"][1]["name"] == "Vile Gas" and fester["damage_taken"]["total"] == 36000
    assert cli.training_main(["hurt", "--kind", "offline", "--data-dir", str(tmp_path)]) == 0
    assert [c["context"] for c in json.loads(capsys.readouterr().out)["contexts"]] == ["631:5:0"]
    assert hashlib.sha256(path.read_bytes()).digest() == before


@pytest.mark.parametrize("args", [["--context", "631:99:0"], ["--context", "bad"], ["--last", "0"]])
def test_cli_hurt_refuses_bad_arguments(pulls, tmp_path, args, monkeypatch):
    monkeypatch.setattr(cli, "_spell_book", lambda root: None)
    if args[0] == "--last":
        with pytest.raises(SystemExit):
            cli.training_main(["hurt", *args, "--data-dir", str(tmp_path)])
    else:
        assert cli.training_main(["hurt", *args, "--data-dir", str(tmp_path)]) == 1


def test_cli_hurt_never_migrates(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(cli, "_spell_book", lambda root: None)
    assert cli.training_main(["hurt", "--data-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "mind.db").exists()
    conn = sqlite3.connect(tmp_path / "mind.db")
    conn.execute("PRAGMA user_version=7")
    conn.close()
    before = (tmp_path / "mind.db").read_bytes()
    assert cli.training_main(["hurt", "--data-dir", str(tmp_path)]) == 1
    assert (tmp_path / "mind.db").read_bytes() == before
