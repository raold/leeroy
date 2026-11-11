"""Owner choices, recoverability and pending faces; invented cast and fake worker only."""
from dataclasses import asdict, replace
import json
import sqlite3

import pytest

from conftest import run
from leeroy_mind.nightly.enrich import _bare
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import ProfilePhoto
from leeroy_mind.selfies import cli, files, kind, scenes, variety
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.store import current, pick, queued_sets
from leeroy_mind.selfies.style import PLAIN
from leeroy_mind.selfies.worker import WorkerError
from nightly.conftest import IDENT
from nightly.test_night_enrich import APPEARANCE
from nightly.test_night_photo_kind import KIND
from selfies.fakes import ready
from selfies.test_variety import factory, source, vec


def generated_kind(store, guid):
    p = load_persona(store,guid)
    kind._record(store,guid,PLAIN,p.photo)
    return p


def test_hand_set_kind_survives_looks_reroll(source):
    root,store,clock = source
    p = load_persona(store,102)
    hand = replace(p,photo=replace(p.photo,outfit="a blue raincoat"))
    save_persona(store,hand,source="owner",ts=clock.now(),race="Night Elf",class_name="Druid")
    store.set_meta(kind.ADOPTED,"1")
    result = run(variety.run(root,store,SelfieCfg(),IDENT,clock,top=1,go=True,make_worker=factory(root)))
    assert result["selected"][0]["guid"] == 102
    changed = load_persona(store,102)
    assert changed.appearance != hand.appearance and changed.photo == hand.photo
    assert kind.chosen_style(store,changed) is None


def test_plain_kind_waits_for_existing_night_pass(source):
    root,store,clock = source
    generated_kind(store,102)
    run(variety.run(root,store,SelfieCfg(),IDENT,clock,top=1,go=True,make_worker=factory(root)))
    assert load_persona(store,102).photo is None
    assert store.get_meta(kind.STYLE_KEY.format(guid=102)) is None
    assert not queued_sets(store)


def test_go_snapshots_before_writing_and_records_prior_fields(source,capsys):
    root,store,clock = source
    p = generated_kind(store,102)
    assert cli.selfies_main(["variety","--go","--top","1","--data-dir",str(root)],make_worker=factory(root)) == 0
    capsys.readouterr()
    snaps = list((root/"snapshots").glob("mind-pre-variety-*.db"))
    assert len(snaps) == 1
    with sqlite3.connect(snaps[0]) as conn:
        assert json.loads(conn.execute("SELECT json FROM persona_core WHERE guid=102").fetchone()[0])["appearance"] == json.loads(json.dumps(asdict(p.appearance)))
    receipt = json.loads(store.get_meta("selfies:variety-prior:102"))
    assert receipt["appearance"] == json.loads(json.dumps(asdict(p.appearance)))
    assert receipt["photo"] == asdict(p.photo)


def test_personas_undo_restores_old_looks_and_kind(source,capsys):
    from leeroy_mind.__main__ import main
    root,store,clock = source
    p = generated_kind(store,102)
    run(variety.run(root,store,SelfieCfg(),IDENT,clock,top=1,go=True,make_worker=factory(root)))
    row = store.conn.execute("SELECT id FROM persona_ledger WHERE guid=102 AND kind='variety'").fetchone()
    assert row is not None
    assert main(["personas","--data-dir",str(root),"undo",str(row[0])]) == 0
    capsys.readouterr()
    store.conn.commit()  # release the reader's snapshot before reading the CLI write
    restored = load_persona(store,102)
    assert restored.appearance == p.appearance and restored.photo == p.photo


def test_existing_looks_without_disclosures_do_not_join_enrich_pass(h):
    p = h.persona(101,appearance=APPEARANCE)
    assert not p.disclosures
    (h.settings.data_dir/"mind.toml").write_text("[looks]\ndiversity=true")
    assert not _bare(h.ctx(),101)


def test_reroll_blocks_scene_candidates_until_a_new_pick(h):
    p = h.persona(101,appearance=APPEARANCE)
    save_persona(h.store,replace(p,photo=ProfilePhoto(**KIND)),source="generated",ts=h.T,
                 race="Human",class_name="Warrior")
    root = h.settings.data_dir
    ready(h.store,root,101,1,now=h.T)
    first = pick(h.store,101,1,0,source="cli",now=h.T)
    h.store.set_meta(variety.PENDING+"101",str(first))
    ctx = h.ctx()
    assert not scenes._eligible(ctx,101,h.T)
    pick(h.store,101,1,1,source="cli",now=h.T+1)
    assert scenes._eligible(ctx,101,h.T+1)


def test_bad_photo_is_skipped_without_losing_other_clusters(source):
    root,store,clock = source
    class Worker:
        async def start(self): pass
        async def stop(self): pass
        async def embed(self,path):
            if "101" in path.parts:
                raise WorkerError("encoder_failed")
            return vec(0)
    result = run(variety.run(root,store,SelfieCfg(),IDENT,clock,make_worker=lambda *a,**kw:Worker()))
    assert result["embedded"] == 2
    assert result["skipped"] == [{"guid":101,"code":"encoder_failed"}]
    assert [r["guid"] for r in result["selected"]] == [103]


def test_protected_members_are_listed_inside_clusters(source):
    root,store,clock = source
    p = load_persona(store,102)
    save_persona(store,replace(p,appearance=replace(p.appearance,hair_colour="black")),
                 source="owner",ts=clock.now(),race="Night Elf",class_name="Druid")
    result = run(variety.run(root,store,SelfieCfg(),IDENT,clock,make_worker=factory(root)))
    assert result["clusters"][0]["protected"] == [{"guid":102,"code":"owner"}]


def test_release_only_named_protected_members_in_current_preview(source,capsys):
    root,store,clock = source
    p = load_persona(store,102)
    p = replace(p,identity=replace(p.identity,player_gender="female"),
                romance=replace(p.romance,stance="lust"),
                appearance=replace(p.appearance,hair_colour="black"))
    save_persona(store,p,source="owner",ts=clock.now(),race="Night Elf",class_name="Druid")
    args = ["variety","--data-dir",str(root)]
    assert cli.selfies_main(args,make_worker=factory(root)) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["clusters"][0]["protected"] == [{"guid":102,"code":"owner"}]
    before = store.persona_versions()
    assert cli.selfies_main(args+["--release","103","--go"],make_worker=factory(root)) == 1
    assert "release_not_listed" in capsys.readouterr().err
    assert store.persona_versions() == before
    assert cli.selfies_main(args+["--release","102","--go"],make_worker=factory(root)) == 0
    report = json.loads(capsys.readouterr().out)
    assert 102 in {r["guid"] for r in report["selected"]}
    changed = load_persona(store,102)
    assert changed.appearance != p.appearance
    assert changed.appearance.build in {"curvy","voluptuous"}
    assert json.loads(store.get_meta("selfies:variety-prior:102"))["released"]


def test_snapshot_failure_aborts_before_worker_or_write(source,capsys,monkeypatch):
    from leeroy_mind import __main__ as main
    root,store,clock = source
    before = store.persona_versions()
    def broken(*args):
        raise OSError("private path")
    monkeypatch.setattr(main,"_snapshot_file",broken)
    assert cli.selfies_main(["variety","--go","--data-dir",str(root)],
                            make_worker=lambda *a,**kw:pytest.fail("must stop before worker")) == 1
    assert capsys.readouterr().err == "selfies variety failed: filesystem_error\n"
    assert store.persona_versions() == before


def test_kind_receipt_uses_the_photo_as_stored(source,monkeypatch):
    root,store,clock = source
    p = generated_kind(store,102)
    p = replace(p,identity=replace(p.identity,player_gender="female"),romance=replace(p.romance,stance="lust"))
    save_persona(store,p,source="generated",ts=clock.now(),race="Night Elf",class_name="Druid")
    def cleaned(store,p,**kwargs):
        return save_persona(store,replace(p,photo=replace(p.photo,setting=p.photo.setting+" ")),**kwargs)
    monkeypatch.setattr(variety,"save_persona",cleaned)
    run(variety.run(root,store,SelfieCfg(),IDENT,clock,top=1,go=True,make_worker=factory(root)))
    assert kind.chosen_style(store,load_persona(store,102)) == "lust"


def test_cleared_plain_kind_rechooses_and_queues_with_the_night(h):
    root = h.settings.data_dir
    for g in (101,102):
        p = h.persona(g,appearance=APPEARANCE)
        save_persona(h.store,replace(p,photo=ProfilePhoto(**KIND)),source="generated",ts=h.T,
                     race="Human",class_name="Warrior")
        generated_kind(h.store,g)
        ready(h.store,root,g,1,now=h.T)
        pick(h.store,g,1,0,source="cli",now=h.T)
        files.sync_chosen(root,g,current(h.store,g))
    run(variety.run(root,h.store,SelfieCfg(),IDENT,h.clock,top=1,go=True,make_worker=factory(root)))
    assert load_persona(h.store,102).photo is None and not queued_sets(h.store)
    ctx = h.ctx({"photo":[KIND]})
    run(kind.stage_photo(ctx))
    assert load_persona(h.store,102).photo == ProfilePhoto(**KIND)
    assert [(s.guid,s.set_no) for s in queued_sets(h.store)] == [(102,2)]


def test_undo_does_not_overwrite_a_later_owner_appearance(source):
    from leeroy_mind.nightly.growth import undo
    root,store,clock = source
    generated_kind(store,102)
    run(variety.run(root,store,SelfieCfg(),IDENT,clock,top=1,go=True,make_worker=factory(root)))
    row = store.conn.execute("SELECT id FROM persona_ledger WHERE kind='variety'").fetchone()
    p = load_persona(store,102)
    hand = replace(p,appearance=replace(p.appearance,hair_colour="copper"))
    save_persona(store,hand,source="owner",ts=clock.now(),race="Night Elf",class_name="Druid")
    assert not undo(store,row[0],now=clock.now())
    assert load_persona(store,102) == hand
