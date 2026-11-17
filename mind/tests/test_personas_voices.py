"""Importing voice sheets (`personas voices`, personas/voices.py): a sheet file is read in the
lab's shape or the schema's, checked, and saved with --go as an owner persona version with a
"voice" ledger row the persona ledger's undo takes back; a dry run and a refusal print codes and
sizes, never a sheet's text. Every sheet here is invented (tests/sheets.py)."""
import json
import sqlite3

import pytest

from conftest import make_persona
from dbtemplate import open_migrated
from leeroy_mind.__main__ import main
from leeroy_mind.clock import SystemClock
from leeroy_mind.human import HumanIdentity
from leeroy_mind.nightly.growth import undo
from leeroy_mind.personas import voices
from leeroy_mind.personas.io import canonical_json, load_persona, save_persona
from leeroy_mind.personas.schema import PersonaError, speech_to_dict
from leeroy_mind.protocol import Member
from leeroy_mind.storage.store import Store
from sheets import GRUFF, BRIGHT, sheet

CAST = ((201, "Arkon"), (202, "Belra"), (203, "Coros"))
HOME = "lives with a flatmate"  # the test cast's household: what an import stamps the sheet with


def at_home(data=GRUFF, **over):
    """The sheet as an import stores it: stamped with her household."""
    return sheet(data, household=HOME, **over)
HUMAN = HumanIdentity(1, "Testmage", "Zan", "Quentin", True, "he")


def lab_file(guid, data=GRUFF, **top):
    """A sheet in the sheet lab's shape, with the keys the import does not read."""
    return {"guid": guid, "character_name": dict(CAST).get(guid, "Nobody"),
            "fixed_facts": {"age": 30},
            "voice": {"register": data["register"], "sentence_shape": data["shape"],
                      "says": data["says"], "never_says": data["never"],
                      "unprompted": data["brings_up"], "with_him": data["with_him"],
                      "relationship_stance": data["stance"], "humour": "dry",
                      "typing": "lower case, no full stops"},
            "texture_seed": {"casual": 1.0}, "samples": data["samples"],
            "prompt_block": "not read", "prompt_block_est_tokens": 1, **top}


def write(folder, guid, body, name=None):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or f"{guid}.json")
    path.write_bytes((body if isinstance(body, str) else json.dumps(body)).encode("utf-8"))
    return path


def _members(store):
    store.upsert_cast([Member(guid=g, name=n, race=1, class_=1, gender=0, level=80, rank=3,
                              online=False, map=0, zone=0, spec_tab=-1) for g, n in CAST]
                      + [Member(guid=1, name="Testmage", race=1, class_=8, gender=0, level=80,
                                rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1000.0)
    for g, n in CAST:
        save_persona(store, make_persona(g, n, player_gender="female" if g == 202 else "male"),
                     source="generated", ts=1000.0, race="Human", class_name="Warrior")
    return store


def _ledger(store, guid=201):
    with store.transaction():
        return [dict(r) for r in store.conn.execute(
            "SELECT id, kind, text, ref FROM persona_ledger WHERE guid=? ORDER BY id", (guid,))]


def _source(store, guid=201):
    return store.conn.execute("SELECT source FROM persona_core WHERE guid=?", (guid,)).fetchone()[0]


# -- reading a file ----------------------------------------------------------------------------------

def test_a_file_in_the_labs_shape_or_the_schemas_reads_as_the_same_sheet(tmp_path):
    lab = write(tmp_path, 201, lab_file(201))
    own = write(tmp_path, 202, {"guid": 202, "speech": GRUFF})
    assert voices.read_sheet(lab) == (201, "Arkon", sheet())
    assert voices.read_sheet(own) == (202, None, sheet())
    bom = write(tmp_path, 203, "﻿" + json.dumps(lab_file(203)))
    assert voices.read_sheet(bom)[2] == sheet()


@pytest.mark.parametrize("body,code", [
    ("{not json", "not_json"), ("[1, 2]", "bad_shape"), ({"guid": 201}, "bad_shape"),
    ({"guid": 201, "voice": "dry"}, "bad_shape"),
    ({"guid": 201, "speech": {**GRUFF, "humour": "dry"}}, "bad_shape"),
    ({"guid": 201, "speech": {**GRUFF, "says": "noted"}}, "bad_shape"),
    ({"guid": 201, "voice": {"register": "dry"}, "samples": {}}, "bad_shape"),
    ("x" * (voices.MAX_FILE_BYTES + 1), "unreadable"),
])
def test_what_a_file_is_refused_for(tmp_path, body, code):
    with pytest.raises(voices.SheetError) as e:
        voices.read_sheet(write(tmp_path, 201, body))
    assert e.value.code == code
    assert code in voices.REFUSALS


def test_a_missing_file_is_unreadable(tmp_path):
    with pytest.raises(voices.SheetError) as e:
        voices.read_sheet(tmp_path / "201.json")
    assert e.value.code == "unreadable"


# -- the plan ----------------------------------------------------------------------------------------

def test_the_plan_names_each_files_outcome_and_sizes_and_skips_files_that_are_no_sheets(store, tmp_path):
    _members(store)
    write(tmp_path, 201, lab_file(201))
    write(tmp_path, 202, lab_file(202, BRIGHT))
    write(tmp_path, 0, {"plan": "the cast"}, name="cast_plan.json")
    plans = voices.plan(store, tmp_path)
    assert [(h.guid, h.name, h.outcome, h.codes, h.samples) for h in plans] == [
        (201, "Arkon", "import", (), 5), (202, "Belra", "import", (), 5)]
    assert all(0 < h.tokens <= 120 for h in plans) and plans[0].speech == at_home()
    assert "noted" not in repr(plans[0])  # a plan's repr never holds her words
    assert [h.guid for h in voices.plan(store, tmp_path, guids={202})] == [202]
    voices.set_voice(store, 201, sheet(), now=2000.0)
    assert voices.plan(store, tmp_path, guids={201})[0].outcome == "unchanged"


@pytest.mark.parametrize("body,codes", [
    (lab_file(999), ("guid_mismatch",)),
    (lab_file(201, character_name="Someone"), ("name_mismatch",)),
    ({"guid": 201, "speech": {**GRUFF, "says": ["a", "b", "c"]}}, ("invalid",)),
    ({"guid": 201, "speech": {**GRUFF, "samples": {"tease": "you first"}}}, ("invalid",)),
    ({"guid": 201, "speech": {**GRUFF, "samples": {**GRUFF["samples"], "flirt": "come here"}}},
     ("invalid",)),  # a man, and flirt beside warm
    ({"guid": 201, "speech": {**GRUFF, "brings_up": ["her crush on Belra"]}}, ("member_romance",)),
    ({"guid": 201, "speech": {**GRUFF, "samples": {**GRUFF["samples"],
                                                     "tease": "ask Coros, he pulled early"}}},
     ("member_name",)),
    ({"guid": 201, "speech": {**GRUFF, "samples": {**GRUFF["samples"],
                                                     "tired": "awesome. brain is soup"}}},
     ("never_in_sample",)),
    ({"guid": 201, "speech": {**GRUFF, "stance": "single since 2019"}}, ("later_era",)),
    ({"guid": 201, "speech": {**GRUFF, "says": ["noted", "sound as", "on my tally",
                                                 "you absolute menace"]}}, ("says_slop",)),
])
def test_what_a_sheet_is_refused_for_by_code(store, tmp_path, body, codes):
    _members(store)
    write(tmp_path, 201, body)
    (h,) = voices.plan(store, tmp_path, human=HUMAN)
    assert (h.outcome, h.codes) == ("refused", codes) and h.speech is None
    assert set(codes) <= set(voices.REFUSALS)
    if codes == ("invalid",):  # the schema's own messages: paths and limits, never her words
        assert h.details and all(d.startswith("speech") for d in h.details)


def test_a_sheet_for_nobody_in_the_cast_or_with_his_real_name_is_refused(store, tmp_path):
    _members(store)
    write(tmp_path, 777, lab_file(777))
    assert voices.plan(store, tmp_path)[0].codes == ("no_persona",)
    write(tmp_path, 1, lab_file(1))  # he has no persona: never a sheet for him
    assert voices.plan(store, tmp_path, guids={1})[0].codes == ("no_persona",)
    write(tmp_path, 201, {"guid": 201, "speech": {**GRUFF, "with_him": "calls him Quentin"}})
    plans = {h.guid: h for h in voices.plan(store, tmp_path, human=HUMAN)}
    assert plans[201].codes == ("real_name",)
    assert "Quentin" not in " ".join(plans[201].details)


# -- writing it --------------------------------------------------------------------------------------

def test_set_voice_saves_an_owner_version_with_a_ledger_row_and_undo_takes_it_back(store):
    _members(store)
    before = store.get_persona_json(201)
    ledger = voices.set_voice(store, 201, sheet(), now=2000.0)
    assert load_persona(store, 201).speech == at_home()
    assert store.persona_versions()[201] == 2 and _source(store) == "owner"
    assert _ledger(store) == [{"id": ledger, "kind": "voice", "text": "voice sheet set",
                               "ref": None}]
    receipt = json.loads(store.get_meta(voices.RECEIPT.format(ledger=ledger)))
    assert receipt == {"after": speech_to_dict(at_home()), "before": None, "source": "generated",
                       "version": 2}
    assert voices.set_voice(store, 201, sheet(), now=2001.0) is None  # nothing to do
    assert undo(store, ledger, now=3000.0)
    after = load_persona(store, 201)
    assert after.speech is None and _source(store) == "generated"
    # byte for byte the persona it was: no "speech" key is left behind
    assert canonical_json(after) == canonical_json(load_persona_json(before))
    assert json.loads(store.get_persona_json(201)).get("speech", "absent") == "absent"
    assert not undo(store, ledger, now=3001.0)
    assert [r["kind"] for r in _ledger(store)] == ["voice", "undo"]


def load_persona_json(js):
    from leeroy_mind.personas.schema import persona_from_dict
    return persona_from_dict(json.loads(js))


def test_a_replaced_sheet_undoes_to_the_one_before_and_a_later_change_wins(store):
    _members(store)
    first = voices.set_voice(store, 201, sheet(), now=2000.0)
    other = at_home(register="soft-spoken and careful")
    second = voices.set_voice(store, 201, other, now=2100.0)
    assert _ledger(store)[-1]["text"] == "voice sheet replaced"
    assert not undo(store, first, now=3000.0)          # the second change moved it: undo that first
    assert load_persona(store, 201).speech == other
    assert undo(store, second, now=3001.0) and load_persona(store, 201).speech == at_home()
    assert undo(store, first, now=3002.0) and load_persona(store, 201).speech is None


def test_clearing_gives_back_the_source_she_had_before_her_first_sheet(store):
    _members(store)
    voices.set_voice(store, 201, sheet(), now=2000.0)
    voices.set_voice(store, 201, sheet(register="soft-spoken and careful"), now=2100.0)
    cleared = voices.set_voice(store, 201, None, now=2200.0)
    assert load_persona(store, 201).speech is None and _source(store) == "generated"
    assert _ledger(store)[-1] == {"id": cleared, "kind": "voice", "text": "voice sheet removed",
                                  "ref": None}
    assert undo(store, cleared, now=3000.0)
    assert load_persona(store, 201).speech.register == "soft-spoken and careful"


def test_the_schema_refuses_a_bad_sheet_at_the_write_too(store):
    _members(store)
    with pytest.raises(PersonaError, match="speech.samples: needs an answer sample"):
        voices.set_voice(store, 201, sheet(samples={"tease": "you first"}), now=2000.0)
    assert store.persona_versions()[201] == 1 and _ledger(store) == []
    with pytest.raises(LookupError):
        voices.set_voice(store, 999, sheet(), now=2000.0)


def test_the_ledger_takes_the_voice_kind_on_a_migrated_database(store):
    from leeroy_mind.nightly.store import ledger_add
    assert ledger_add(store, guid=201, kind="voice", text="voice sheet set", evidence=[], now=1.0)
    with pytest.raises(sqlite3.IntegrityError):
        ledger_add(store, guid=201, kind="tone", text="x", evidence=[], now=1.0)


# -- the CLI -----------------------------------------------------------------------------------------

def _data_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "mind.toml").write_bytes(b"")  # `personas undo` reads it
    s = Store(open_migrated(d / "mind.db"), clock=SystemClock())
    _members(s)
    s.close()
    return d


def _sheets(d):
    conn = sqlite3.connect(d / "mind.db")
    try:
        return {g: json.loads(js).get("speech") for g, js in
                conn.execute("SELECT guid, json FROM persona_core").fetchall()}
    finally:
        conn.close()


def _folder(tmp_path):
    folder = tmp_path / "sheets"
    write(folder, 201, lab_file(201))
    write(folder, 202, lab_file(202, BRIGHT))
    write(folder, 203, {"guid": 203, "speech": {**GRUFF, "stance": "single since 2019"}})
    return folder


def _no_text(out):
    for data in (GRUFF, BRIGHT):
        for value in (data["register"], data["shape"], data["with_him"], data["stance"],
                      *data["says"], *data["brings_up"], *data["samples"].values()):
            assert value not in out


def test_cli_plans_read_only_by_default_and_prints_sizes_and_codes_never_text(tmp_path, capsys):
    d, folder = _data_dir(tmp_path), _folder(tmp_path)
    before = (d / "mind.db").read_bytes()
    assert main(["personas", "--data-dir", str(d), "voices", "--dir", str(folder)]) == 1
    out = capsys.readouterr().out
    assert "    201  Arkon         would import  [block " in out and "/120 tokens, 5 samples]" in out
    assert "    203  Coros         refused: later_era" in out
    assert "         later_era: slang or a year from after 2009" in out
    assert "would import 2 voice sheets; 0 unchanged; 1 refused" in out
    assert "nothing was written" in out and (d / "mind.db").read_bytes() == before
    _no_text(out)
    assert main(["personas", "--data-dir", str(d), "voices", "--dir", str(folder), "--dry-run",
                 "--guid", "201,202"]) == 0
    assert "would import 2 voice sheets; 0 unchanged; 0 refused" in capsys.readouterr().out


def test_cli_go_saves_after_a_snapshot_then_undo_and_clear(tmp_path, capsys):
    d, folder = _data_dir(tmp_path), _folder(tmp_path)
    assert main(["personas", "--data-dir", str(d), "voices", "--dir", str(folder), "--go"]) == 1
    out = capsys.readouterr().out
    assert "snapshot " in out and "imported (ledger #1)" in out and "imported (ledger #2)" in out
    assert "did import 2 voice sheets; 0 unchanged; 1 refused" in out
    _no_text(out)
    assert len(list((d / "snapshots").glob("mind-pre-voices-*.db"))) == 1
    got = _sheets(d)
    assert got[201] == {**GRUFF, "household": HOME} and got[203] is None
    assert got[202] == {**BRIGHT, "household": HOME}
    # again: nothing changes
    assert main(["personas", "--data-dir", str(d), "voices", "--dir", str(folder), "--go",
                 "--guid", "201,202"]) == 0
    assert "did import 0 voice sheets; 2 unchanged; 0 refused" in capsys.readouterr().out
    # `personas undo <ledger id>` restores the prior version
    assert main(["personas", "--data-dir", str(d), "undo", "1"]) == 0
    capsys.readouterr()
    assert _sheets(d)[201] is None and _sheets(d)[202] == {**BRIGHT, "household": HOME}
    # --clear takes the rest off: the rollback step before older code
    assert main(["personas", "--data-dir", str(d), "voices", "--clear"]) == 0
    assert "would take the voice sheet off 1 members" in capsys.readouterr().out
    assert main(["personas", "--data-dir", str(d), "voices", "--clear", "--guid", "202",
                 "--go"]) == 0
    assert "cleared (ledger #" in capsys.readouterr().out
    assert _sheets(d) == {201: None, 202: None, 203: None}
    conn = sqlite3.connect(d / "mind.db")
    try:  # each back to the source it had before, and stored as before the field existed
        assert set(r[0] for r in conn.execute("SELECT source FROM persona_core")) == {"generated"}
        assert all("speech" not in json.loads(r[0]) for r in conn.execute(
            "SELECT json FROM persona_core"))
    finally:
        conn.close()


def test_cli_says_what_it_needs_and_names_a_guid_without_a_file(tmp_path, capsys):
    d, folder = _data_dir(tmp_path), _folder(tmp_path)
    assert main(["personas", "--data-dir", str(d), "voices"]) == 1
    assert "--dir DIR" in capsys.readouterr().err
    assert main(["personas", "--data-dir", str(d), "voices", "--dir", str(tmp_path / "none")]) == 1
    assert "no folder" in capsys.readouterr().err
    assert main(["personas", "--data-dir", str(d), "voices", "--dir", str(folder), "--go",
                 "--dry-run"]) == 1
    assert "not both" in capsys.readouterr().err
    assert main(["personas", "--data-dir", str(d), "voices", "--dir", str(folder), "--guid",
                 "201,555"]) == 1
    assert "    555                no sheet file" in capsys.readouterr().out


def test_cli_refuses_to_write_while_the_mind_runs(tmp_path, capsys):
    from leeroy_mind.app import acquire_instance_lock
    d, folder = _data_dir(tmp_path), _folder(tmp_path)
    with acquire_instance_lock(d):
        assert main(["personas", "--data-dir", str(d), "voices", "--dir", str(folder),
                     "--go"]) == 1
        assert "mind service is running" in capsys.readouterr().out
    assert _sheets(d) == {201: None, 202: None, 203: None}


# -- the rest of the persona's readers ---------------------------------------------------------------

def test_an_export_carries_the_sheet_and_an_import_of_it_changes_nothing(store, tmp_path):
    from leeroy_mind.personas.io import export_personas, import_personas
    _members(store)
    voices.set_voice(store, 201, sheet(), now=2000.0)
    (path,) = export_personas(store, tmp_path / "out", guids=[201])
    assert json.loads(path.read_text(encoding="utf-8"))["speech"] == {**GRUFF, "household": HOME}
    report = import_personas(store, tmp_path / "out", ts=3000.0, human=HUMAN)
    assert (report.imported, report.unchanged, report.errors) == ([], [201], {})


def test_the_nights_core_judge_reads_no_sheet(store):
    from leeroy_mind.nightly.growth import core_text
    _members(store)
    plain = core_text(load_persona(store, 201))
    voices.set_voice(store, 201, sheet(), now=2000.0)
    assert core_text(load_persona(store, 201)) == plain
