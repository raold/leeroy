"""A US member's home state (owner, 2026-11-15): where her own words put her, else a draw by
population seeded by her guid; `personas homes` plans it read-only and saves it with --go as an
owner persona version with a "home" ledger row the persona ledger's undo takes back. Households
are shaped like the stored cast's; every person here is invented."""
import json
import sqlite3
from collections import Counter
from dataclasses import replace

import pytest

from conftest import make_persona
from dbtemplate import open_migrated
from leeroy_mind.__main__ import main
from leeroy_mind.clock import SystemClock
from leeroy_mind.nightly.growth import undo
from leeroy_mind.personas import homes
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import PersonaError
from leeroy_mind.personas.states import US_STATES
from leeroy_mind.protocol import Member
from leeroy_mind.storage.store import Store


def us(guid=201, name="Arkon", **real_life):
    p = make_persona(guid, name, region="US", accent="en-us")
    return replace(p, real_life=replace(p.real_life, **{"household": "Lives alone with a cat",
                                                         **real_life}))


@pytest.mark.parametrize("text,state,phrase", [
    ("Cinder-block house outside Flagstaff, AZ. One cat.", "Arizona", "Flagstaff, AZ"),
    ("Split-level in Cleveland TN, two kids (one college, one HS)", "Tennessee", "Cleveland TN"),
    ("Split-level in Columbus, one cat, no partner", "Ohio", "Columbus"),
    ("Alone in a one-bedroom apartment off I-70 in Kansas City", "Missouri", "Kansas City"),
    ("Lives with girlfriend and a corgi in Mobile, Alabama.", "Alabama", "Alabama"),
    ("lives with her husband in tucson, az", "Arizona", "tucson, az"),
    ("Alone in a Boise walk-up, one grey tabby", "Idaho", "Boise"),
    ("National Weather Service forecaster, Omaha office", "Nebraska", "Omaha"),
    ("Senior Civil Engineer, Virginia DOT", "Virginia", "Virginia"),
    ("Mechanic in Charleston, West Virginia", "West Virginia", "West Virginia"),
    ("One-cat apartment in St. Paul.", "Minnesota", "St. Paul"),
    ("Forensic DNA analyst at NC State Crime Lab", "North Carolina", "NC State"),
])
def test_a_place_her_words_name_is_her_state(text, state, phrase):
    m = homes.mentions(text)[0]
    assert (m.state, m.phrase, m.own) == (state, phrase, True)


@pytest.mark.parametrize("text", [
    "Logs in 7-10pm CT to fit around evening rounds",   # a time zone, not Connecticut
    "9-5 work, 8:30 CT raids, late-night reading",
    "Alone in a duplex off Peoria with a grey Maine Coon",  # a road and a cat
    "Lives alone; raids till late, OK with that",
    "Sleeps in a boulder of a bed, near a phoenix tattoo",  # common nouns
    "Lives with her husband and a cat named Ash.",
])
def test_no_place(text):
    assert homes.mentions(text) == []


def test_a_relatives_place_counts_only_without_one_of_her_own():
    kids = us(household="Small house, no partner. Two grown kids in Ohio and Texas.")
    assert [(m.state, m.own) for m in homes.mentions(kids.real_life.household)] == [
        ("Ohio", False), ("Texas", False)]
    assert homes.choose(kids).home == "Ohio"  # the first, as nothing of her own is named
    both = us(household="Divorced, kid in Anchorage.", rhythm="off-season in Juneau apartment")
    plan = homes.choose(both)
    assert (plan.home, plan.how, plan.field, plan.phrase) == ("Alaska", "text", "rhythm", "Juneau")
    mom = us(household="One-bedroom off U.S. 62, Midland TX",
             life_thread="Sunday phone call to mom in Akron")
    assert homes.choose(mom).home == "Texas"


def test_the_household_comes_first_then_the_job_then_the_interview():
    assert homes.choose(us(household="Lives alone", job="Bail bonds agent in Tulsa")).home == "Oklahoma"
    assert homes.choose(us(household="Condo in Austin", job="Loss adjuster in Tulsa")).home == "Texas"
    p = replace(us(), mock_interview="Q: Where are you? A: Dayton, OH, same as always.")
    plan = homes.choose(p)
    assert (plan.home, plan.field) == ("Ohio", "interview")


def test_otherwise_a_draw_by_population_the_same_for_the_same_guid():
    plan = homes.choose(us(guid=4242))
    assert plan.how == "draw" and plan.home in US_STATES and plan.field is None
    assert all(homes.draw(4242) == plan.home for _ in range(3))
    counts = Counter(homes.draw(g) for g in range(1, 4001))
    assert counts.most_common(1)[0][0] == "California"
    assert counts["California"] > counts["Texas"] > counts["Ohio"] > counts["Wyoming"]


def test_the_plan_gives_only_us_members_without_a_home_and_clear_takes_them_off():
    uk = make_persona(202, "Belra")
    done = replace(us(203, "Coros"), identity=replace(us(203, "Coros").identity, home="Utah"))
    plan = homes.plan_homes([us(201, "Arkon", household="Condo in Austin"), uk, done])
    assert [(h.guid, h.home, h.how) for h in plan] == [(201, "Texas", "text")]
    assert [(h.guid, h.before, h.home, h.how) for h in homes.plan_homes(
        [us(201, "Arkon"), uk, done], clear=True)] == [(203, "Utah", None, "clear")]


# -- writing it ------------------------------------------------------------------------------------

def _store(store):
    store.upsert_cast([Member(guid=201, name="Arkon", race=1, class_=1, gender=0, level=80,
                              rank=3, online=False, map=0, zone=0, spec_tab=-1)], 1000.0)
    save_persona(store, us(household="Lives with her husband in Irving, TX"), source="seed",
                 ts=1000.0, race="Human", class_name="Warrior")
    return store


def _ledger(store):
    with store.transaction():
        return [dict(r) for r in store.conn.execute(
            "SELECT id, kind, text, ref FROM persona_ledger WHERE guid=201 ORDER BY id")]


def test_set_home_saves_an_owner_version_with_a_ledger_row_and_undo_takes_it_back(store):
    _store(store)
    ledger = homes.set_home(store, 201, "Texas", now=2000.0)
    assert load_persona(store, 201).identity.home == "Texas"
    assert store.persona_versions()[201] == 2
    assert store.conn.execute("SELECT source FROM persona_core WHERE guid=201").fetchone()[0] == "owner"
    assert _ledger(store) == [{"id": ledger, "kind": "home", "text": "home not set -> Texas", "ref": None}]
    assert json.loads(store.get_meta(homes.RECEIPT.format(ledger=ledger))) == {
        "after": "Texas", "before": None, "source": "seed", "version": 2}
    assert homes.set_home(store, 201, "Texas", now=2001.0) is None  # nothing to do
    assert undo(store, ledger, now=3000.0)
    assert load_persona(store, 201).identity.home is None
    assert store.conn.execute("SELECT source FROM persona_core WHERE guid=201").fetchone()[0] == "seed"
    assert json.loads(store.get_persona_json(201))["identity"].get("home", "absent") == "absent"
    assert not undo(store, ledger, now=3001.0)


def test_a_later_change_wins_over_the_undo(store):
    _store(store)
    first = homes.set_home(store, 201, "Texas", now=2000.0)
    homes.set_home(store, 201, "Ohio", now=2100.0)
    assert not undo(store, first, now=3000.0)
    assert load_persona(store, 201).identity.home == "Ohio"


def test_the_schema_refuses_a_home_that_is_no_state(store):
    _store(store)
    with pytest.raises(PersonaError, match="identity.home: must be a US state's full name"):
        homes.set_home(store, 201, "Texsa", now=2000.0)
    assert store.persona_versions()[201] == 1 and _ledger(store) == []


# -- the CLI ---------------------------------------------------------------------------------------

def _data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    s = Store(open_migrated(d / "mind.db"), clock=SystemClock())
    s.upsert_cast([Member(guid=g, name=n, race=1, class_=1, gender=0, level=80, rank=3,
                          online=False, map=0, zone=0, spec_tab=-1)
                   for g, n in ((201, "Arkon"), (202, "Belra"), (203, "Coros"))], 1000.0)
    for p in (us(201, "Arkon", household="Alone in Tucson with a fat orange cat"),
              make_persona(202, "Belra"), us(203, "Coros")):
        save_persona(s, p, source="generated", ts=1000.0, race="Human", class_name="Warrior")
    s.close()
    return d


def _homes(d):
    conn = sqlite3.connect(d / "mind.db")
    try:
        return {g: json.loads(js)["identity"].get("home") for g, js in
                conn.execute("SELECT guid, json FROM persona_core").fetchall()}
    finally:
        conn.close()


def test_cli_plans_read_only_by_default(tmp_path, capsys):
    d = _data_dir(tmp_path)
    before = (d / "mind.db").read_bytes()
    assert main(["personas", "--data-dir", str(d), "homes"]) == 0
    out = capsys.readouterr().out
    assert "    201  Arkon         Arizona          household: Tucson" in out
    assert f"    203  Coros         {homes.draw(203):<15}  drawn by population" in out
    assert "202" not in out.split("would give")[0]
    assert ("would give 2 of 2 US members a home state: 1 from their own words (household 1), "
            "1 drawn; 0 had one already; 1 UK members keep UK") in out
    assert "nothing was written" in out and (d / "mind.db").read_bytes() == before


def test_cli_go_saves_after_a_snapshot_and_clear_takes_them_off(tmp_path, capsys):
    d = _data_dir(tmp_path)
    assert main(["personas", "--data-dir", str(d), "homes", "--go"]) == 0
    out = capsys.readouterr().out
    assert "snapshot " in out and "did give 2 of 2 US members a home state" in out
    assert len(list((d / "snapshots").glob("mind-pre-homes-*.db"))) == 1
    assert _homes(d) == {201: "Arizona", 202: None, 203: homes.draw(203)}
    assert main(["personas", "--data-dir", str(d), "homes", "--go"]) == 0
    assert "did give 0 of 2 US members a home state" in capsys.readouterr().out
    assert main(["personas", "--data-dir", str(d), "homes", "--clear", "--go"]) == 0
    assert "did take the home state off 2 members" in capsys.readouterr().out
    assert _homes(d) == {201: None, 202: None, 203: None}
    conn = sqlite3.connect(d / "mind.db")
    try:
        assert [r[0] for r in conn.execute("SELECT kind FROM persona_ledger ORDER BY id")] == [
            "home"] * 4
    finally:
        conn.close()


def test_cli_refuses_to_write_while_the_mind_runs(tmp_path, capsys):
    from leeroy_mind.app import acquire_instance_lock
    d = _data_dir(tmp_path)
    with acquire_instance_lock(d):
        assert main(["personas", "--data-dir", str(d), "homes", "--go"]) == 1
        assert "mind service is running" in capsys.readouterr().out
    assert _homes(d) == {201: None, 202: None, 203: None}
