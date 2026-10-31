import json

import pytest

from conftest import make_persona, seed_test_cast
from leeroy_mind.personas.card import card_race_class
from leeroy_mind.personas.io import (PersonaCache, export_personas, import_personas,
                                     load_persona, save_persona)
from leeroy_mind.personas.schema import PersonaError


def _edit(path, fn):
    d = json.loads(path.read_text(encoding="utf-8"))
    fn(d)
    path.write_text(json.dumps(d, indent=2), encoding="utf-8")


def test_export_import_round_trip(tmp_path, store):
    cast = seed_test_cast(store)
    paths = export_personas(store, tmp_path)
    assert sorted(p.name for p in paths) == [f"{g}.json" for g in range(101, 106)]
    assert paths[0].read_text(encoding="utf-8").startswith('{\n  "schema": 1,')
    report = import_personas(store, tmp_path, ts=2000.0)
    assert report.unchanged == [101, 102, 103, 104, 105]
    assert report.imported == [] and report.errors == {}
    assert set(store.persona_versions().values()) == {1}
    assert load_persona(store, 103) == cast[103]


def test_export_subset_and_missing(tmp_path, store):
    seed_test_cast(store)
    assert [p.name for p in export_personas(store, tmp_path / "out", [102, 999])] == ["102.json"]


def test_bad_file_reported_and_not_written(tmp_path, store):
    seed_test_cast(store)
    export_personas(store, tmp_path)
    _edit(tmp_path / "101.json", lambda d: d["identity"].update(age=16))
    _edit(tmp_path / "102.json", lambda d: d["real_life"].update(job="night porter"))
    report = import_personas(store, tmp_path, ts=2000.0)
    assert any("identity.age" in e for e in report.errors["101.json"])
    assert store.persona_versions()[101] == 1
    # atomic: the good edit is not written either while any file is bad
    assert store.persona_versions()[102] == 1 and report.imported == []


def test_unparseable_file_reported(tmp_path, store):
    seed_test_cast(store)
    (tmp_path / "101.json").write_text("{nope", encoding="utf-8")
    report = import_personas(store, tmp_path, ts=2000.0)
    assert "not valid JSON" in report.errors["101.json"][0]


def test_guid_or_name_mismatch_rejected(tmp_path, store):
    seed_test_cast(store)
    export_personas(store, tmp_path)
    _edit(tmp_path / "101.json", lambda d: d.update(guid=102))
    _edit(tmp_path / "103.json", lambda d: d.update(character_name="Imposter"))
    report = import_personas(store, tmp_path, ts=2000.0)
    assert any("guid" in e for e in report.errors["101.json"])
    assert any("character_name" in e and "Coros" in e for e in report.errors["103.json"])


def test_change_bumps_version_and_card(tmp_path, store):
    seed_test_cast(store)
    export_personas(store, tmp_path)
    _edit(tmp_path / "101.json", lambda d: d["real_life"].update(job="lighthouse keeper"))
    report = import_personas(store, tmp_path, ts=2000.0)
    assert report.imported == [101] and report.unchanged == [102, 103, 104, 105]
    assert store.persona_versions()[101] == 2 and store.persona_versions()[102] == 1
    assert "lighthouse keeper" in store.get_card(101)
    row = store.conn.execute("SELECT source, updated FROM persona_core WHERE guid=101").fetchone()
    assert (row["source"], row["updated"]) == ("owner", 2000.0)
    assert import_personas(store, tmp_path, ts=3000.0).unchanged == [101, 102, 103, 104, 105]


def test_a_member_dial_is_saved_loaded_exported_and_imported(tmp_path, store):
    """Fix wave G4: romance.dial rides the persona through every path the owner uses; a file
    exported before the field has no key and is the same persona (no version bump)."""
    seed_test_cast(store)
    mine = make_persona(102, "Belra", dial=10)
    assert save_persona(store, mine, source="owner", ts=1500.0, race="Night Elf",
                        class_name="Druid") == 2
    def dials():
        return {g: load_persona(store, g).romance.dial for g in (101, 102, 103)}
    assert dials() == {101: None, 102: 10, 103: None}
    export_personas(store, tmp_path)
    assert [json.loads((tmp_path / f"{g}.json").read_text(encoding="utf-8"))["romance"]["dial"]
            for g in (101, 102, 103)] == [None, 10, None]
    _edit(tmp_path / "103.json", lambda d: d["romance"].update(dial=7))
    report = import_personas(store, tmp_path, ts=2000.0)
    assert report.imported == [103] and report.unchanged == [101, 102, 104, 105]
    assert dials() == {101: None, 102: 10, 103: 7}
    _edit(tmp_path / "101.json", lambda d: d["romance"].pop("dial"))  # from before the field
    assert import_personas(store, tmp_path, ts=3000.0).unchanged == [101, 102, 103, 104, 105]
    _edit(tmp_path / "104.json", lambda d: d["romance"].update(dial=11))
    bad = import_personas(store, tmp_path, ts=4000.0)
    assert bad.imported == [] and "romance.dial: must be at most 10" in bad.errors["104.json"]
    assert store.persona_versions()[104] == 1


def test_dry_run_writes_nothing(tmp_path, store):
    seed_test_cast(store)
    export_personas(store, tmp_path)
    _edit(tmp_path / "101.json", lambda d: d["real_life"].update(job="lighthouse keeper"))
    assert import_personas(store, tmp_path, ts=2000.0, dry_run=True).imported == [101]
    assert store.persona_versions()[101] == 1


def test_unknown_cast_keeps_previous_race_and_class(tmp_path, store):
    seed_test_cast(store)
    store.conn.execute("DELETE FROM cast_member")
    store.conn.commit()
    export_personas(store, tmp_path)
    _edit(tmp_path / "101.json", lambda d: d["real_life"].update(job="lighthouse keeper"))
    assert import_personas(store, tmp_path, ts=2000.0).imported == [101]
    card = store.get_card(101)
    assert "lighthouse keeper" in card and card_race_class(card) == ("Orc", "Warrior")
    # the name is still held to the stored persona's
    _edit(tmp_path / "101.json", lambda d: d.update(character_name="Imposter"))
    assert "101.json" in import_personas(store, tmp_path, ts=3000.0).errors


def test_import_is_one_transaction(tmp_path, store, monkeypatch):
    seed_test_cast(store)
    export_personas(store, tmp_path)
    for g in (101, 102):
        _edit(tmp_path / f"{g}.json", lambda d: d["real_life"].update(job="lighthouse keeper"))
    import leeroy_mind.personas.io as pio
    real, calls = pio.save_persona, []

    def flaky(*a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*a, **kw)

    monkeypatch.setattr(pio, "save_persona", flaky)
    with pytest.raises(RuntimeError):
        import_personas(store, tmp_path, ts=2000.0)
    assert store.persona_versions()[101] == 1


def test_save_persona_validates_and_skips_identical(store):
    p = make_persona(101, "Arkon")
    assert save_persona(store, p, source="seed", ts=1.0, race="Orc", class_name="Warrior") == 1
    assert save_persona(store, p, source="seed", ts=2.0, race="Orc", class_name="Warrior") == 1
    with pytest.raises(PersonaError, match="identity.age"):
        save_persona(store, make_persona(101, "Arkon", age=17), source="seed", ts=3.0,
                     race="Orc", class_name="Warrior")


def test_persona_cache_invalidate(store):
    seed_test_cast(store)
    cache = PersonaCache(store)
    assert cache.get(101).real_life.job == "warehouse supervisor"
    assert cache.get(999) is None and sorted(cache.all()) == [101, 102, 103, 104, 105]
    save_persona(store, make_persona(101, "Arkon", job="lighthouse keeper"), source="owner",
                 ts=5.0, race="Orc", class_name="Warrior")
    assert cache.get(101).real_life.job == "warehouse supervisor"
    cache.invalidate(101)
    assert cache.get(101).real_life.job == "lighthouse keeper"
    cache.invalidate()
    assert cache.all()[102].character_name == "Belra"


def test_cli_export_import_card(tmp_path, capsys):
    from leeroy_mind.__main__ import main
    from leeroy_mind.storage.db import open_db
    from leeroy_mind.storage.store import Store
    from leeroy_mind.testing.fake_clock import FakeClock
    from datetime import datetime, timezone
    s = Store(open_db(tmp_path / "mind.db"), clock=FakeClock(datetime(2026, 10, 29, tzinfo=timezone.utc)))
    seed_test_cast(s)
    s.close()
    base = ["personas", "--data-dir", str(tmp_path)]
    assert main([*base, "export", "--guid", "101"]) == 0
    assert (tmp_path / "personas" / "101.json").exists()
    assert main([*base, "import", "--dry-run"]) == 0
    assert main([*base, "card", "101"]) == 0
    out = capsys.readouterr().out
    assert "You play Arkon" in out and "tokens]" in out
    assert main([*base, "card", "999"]) == 1


# -- carried minors ----------------------------------------------------------------------------

def _break(store, guid, raw):
    store.conn.execute("UPDATE persona_core SET json = ? WHERE guid = ?", (raw, guid))
    store.conn.commit()


def test_a_persona_the_code_cannot_read_is_skipped_and_logged_once(store, caplog):
    import logging
    seed_test_cast(store)
    bad = json.loads(store.get_persona_json(102))
    bad["romance"]["stance"] = "smitten"  # a newer schema, say, after a rollback
    _break(store, 102, json.dumps(bad))
    _break(store, 103, "{nope")
    cache = PersonaCache(store)
    with caplog.at_level(logging.WARNING):
        assert cache.get(102) is None and cache.get(102) is None and cache.get(103) is None
        assert sorted(cache.all()) == [101, 104, 105]
    warned = [r.getMessage() for r in caplog.records if r.getMessage().startswith(
        "persona_unreadable")]
    assert len(warned) == 2 and "guid 102" in warned[0] and "guid 103" in warned[1]
    save_persona(store, make_persona(102, "Belra"), source="owner", ts=5.0, race="Orc",
                 class_name="Warrior")
    cache.invalidate(102)
    assert cache.get(102).character_name == "Belra"


def test_import_reads_a_file_saved_with_a_byte_order_mark(tmp_path, store):
    seed_test_cast(store)
    export_personas(store, tmp_path)
    path = tmp_path / "101.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    d["real_life"]["job"] = "lighthouse keeper"
    path.write_text(json.dumps(d), encoding="utf-8-sig")
    report = import_personas(store, tmp_path, ts=2000.0)
    assert report.errors == {} and report.imported == [101]


def test_import_from_a_folder_without_persona_files_is_an_error(tmp_path, store):
    seed_test_cast(store)
    where = tmp_path / "nothing-here"
    report = import_personas(store, where, ts=2000.0)
    assert report.errors == {str(where): ["no persona files (<guid>.json) in this folder"]}
    assert report.imported == [] and set(store.persona_versions().values()) == {1}


def test_the_same_persona_with_a_new_race_or_class_gets_a_new_card(store):
    p = make_persona(101, "Arkon")
    assert save_persona(store, p, source="seed", ts=1.0, race="Orc", class_name="Warrior") == 1
    assert save_persona(store, p, source="seed", ts=2.0, race="Troll", class_name="Warrior") == 2
    assert card_race_class(store.get_card(101)) == ("Troll", "Warrior")
    assert save_persona(store, p, source="seed", ts=3.0, race="Troll", class_name="Warrior") == 2


def test_import_screens_the_real_name_and_member_romance_when_it_knows_the_human(tmp_path, store):
    from leeroy_mind.human import HumanIdentity
    seed_test_cast(store)
    export_personas(store, tmp_path)
    _edit(tmp_path / "101.json",
          lambda d: d["sample_lines"].append({"text": "Pat said hi", "approved": True}))
    _edit(tmp_path / "102.json", lambda d: d.update(secret="has a crush on coros"))
    h = HumanIdentity(1, "Testmage", "Testy", "Pat", True)
    report = import_personas(store, tmp_path, ts=2000.0, human=h)
    assert any("real name" in e for e in report.errors["101.json"])
    assert "Pat" not in " ".join(report.errors["101.json"])
    assert any("romance" in e for e in report.errors["102.json"])
    assert report.imported == [] and set(store.persona_versions().values()) == {1}
