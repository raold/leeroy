"""Dry-run/go on a synthetic database and real subprocess fake worker only."""
import asyncio
from dataclasses import replace
import json
import logging

import pytest

from conftest import HUMAN, run, seed_test_cast
from dbtemplate import open_migrated
from engine_rig import engine_clock
from nightly.conftest import IDENT
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.selfies import cli, files, similarity, variety
from leeroy_mind.selfies.store import current, photos, pick, queued_sets
from leeroy_mind.selfies.worker import SelfieWorker, WorkerError
from leeroy_mind.storage.store import Store
from selfies.fakes import fake_command, ready, renderable_cast


def vec(index):
    return [float(i==index) for i in range(1024)]


@pytest.fixture
def source(tmp_path):
    root = tmp_path/"data"
    root.mkdir()
    (root/"mind.toml").write_text("",encoding="utf-8")
    clock = engine_clock()
    store = Store(open_migrated(root/"mind.db"),clock=clock)
    seed_test_cast(store)
    renderable_cast(store,101,102,103)
    store.upsert_cast([Member(1,"Testmage",1,8,0,80,0,False,0,0,-1)],clock.now())
    store.set_meta("human_guid","1")
    for g in (101,102,103):
        ready(store,root,g,1)
        pick(store,g,1,0,source="cli",now=clock.now())
        files.sync_chosen(root,g,current(store,g))
    yield root,store,clock
    store.close()


def factory(root,**env):
    command,environ = fake_command(root,**env)
    return lambda *a,**kw: SelfieWorker(command,env=environ)


def snapshot(store,root):
    return {"personas":dict(store.persona_versions()),
            "data":{g:store.get_persona_json(g) for g in store.persona_versions()},
            "photos":photos(store),
            "files":{str(p.relative_to(root)):p.read_bytes() for p in (root/"selfies").rglob("*.jpg")}}


def test_synthetic_vectors_form_clusters_keep_one_and_respect_owner():
    vectors = {101:vec(0),102:vec(0),103:vec(1),104:vec(1),105:vec(2)}
    chosen,kept,pairs = similarity.choose(vectors,{102:"owner"},30)
    assert [g for _,_,g,_ in chosen] == [101,104]
    assert kept == [102,103] and pairs == [(0,101,102),(0,103,104)]
    assert len(similarity.choose(vectors,{},1)[0]) == 1
    assert not similarity.choose({101:vec(0),102:vec(1)}, {},30)[0]


@pytest.mark.parametrize("bad", [[],[0.0]*1024,[float("nan")]*1024,[float("inf")]*1024,[True]*1024])
def test_bad_vectors_cannot_select_members(bad):
    with pytest.raises(ValueError,match="embedding_invalid"):
        similarity.clusters({101:bad})


def test_dry_run_writes_neither_personas_embeddings_picks_nor_files(source,capsys,caplog):
    root,store,clock = source
    before = snapshot(store,root)
    with caplog.at_level(logging.INFO):
        code = cli.selfies_main(["variety","--dry-run","--data-dir",str(root)],make_worker=factory(root))
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert code == 0 and report["dry_run"] and report["embedded"] == 3
    assert [r["guid"] for r in report["selected"]] == [102,103]
    assert snapshot(store,root) == before and not queued_sets(store)
    assert store.conn.execute("SELECT count(*) FROM selfie_embedding").fetchone()[0] == 0
    assert not any(s in output.out+caplog.text for s in ("Arkon","Belra","Coros","Quillon","Testmage"))


def test_go_rerolls_and_queues_keeps_chosen_and_gallery_and_lust_build(source,capsys):
    root,store,clock = source
    p = load_persona(store,102)
    p = replace(p,identity=replace(p.identity,player_gender="female"),romance=replace(p.romance,stance="lust"))
    save_persona(store,p,source="generated",ts=clock.now(),race="Night Elf",class_name="Druid")
    before = snapshot(store,root)
    assert cli.selfies_main(["variety","--go","--top","1","--data-dir",str(root)],make_worker=factory(root)) == 0
    report = json.loads(capsys.readouterr().out)
    assert [r["guid"] for r in report["selected"]] == [102]
    changed = load_persona(store,102)
    assert changed.appearance.build in {"curvy","voluptuous"}
    assert replace(changed,appearance=p.appearance,photo=p.photo) == p
    assert changed.appearance != p.appearance
    assert store.conn.execute("SELECT source FROM persona_core WHERE guid=102").fetchone()[0] == "variety"
    assert len(queued_sets(store)) == 1 and queued_sets(store)[0].guid == 102
    after = snapshot(store,root)
    assert after["photos"] == before["photos"] and after["files"] == before["files"]
    assert store.conn.execute("SELECT count(*) FROM selfie_embedding").fetchone()[0] == 3
    assert store.conn.execute("SELECT decided FROM selfie_set WHERE guid=102 AND set_no=1").fetchone()[0] == "picked"


def test_owner_protection_wins_and_cache_reuses_current_pick_only(source,capsys):
    root,store,clock = source
    p = load_persona(store,102)
    owned = replace(p,appearance=replace(p.appearance,hair_colour="black"))
    save_persona(store,owned,source="owner",ts=clock.now(),race="Night Elf",class_name="Druid")
    for g,pickrow in photos(store).items():
        similarity.save(store,g,pickrow.pick,vec(0),clock.now())
    def forbidden(*a,**kw):
        pytest.fail("all current picks already embedded")
    assert cli.selfies_main(["variety","--go","--data-dir",str(root)],make_worker=forbidden) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["kept"] == [102] and report["protected"] == [{"guid":102,"code":"owner"}]
    assert load_persona(store,102) == owned
    assert similarity.cached(store.conn,102,photos(store)[102].pick+999) is None


def test_rerun_keeps_pending_looks_until_a_new_pick(source,capsys):
    root,store,clock = source
    args = ["variety","--go","--data-dir",str(root)]
    assert cli.selfies_main(args,make_worker=factory(root)) == 0
    capsys.readouterr()
    before = snapshot(store,root)
    assert cli.selfies_main(args,make_worker=lambda *a,**kw:pytest.fail("cache must suffice")) == 0
    report = json.loads(capsys.readouterr().out)
    assert not report["selected"]
    assert snapshot(store,root) == before
    assert report["skipped"] == [{"guid":102,"code":"awaiting_new_pick"},{"guid":103,"code":"awaiting_new_pick"}]


def test_running_mind_refuses_before_worker_or_write(source,capsys):
    root,store,clock = source
    before = snapshot(store,root)
    with acquire_instance_lock(root):
        assert cli.selfies_main(["variety","--go","--data-dir",str(root)],make_worker=lambda *a,**k:pytest.fail()) == 1
    assert "mind must be stopped" in capsys.readouterr().err
    assert snapshot(store,root) == before


def test_worker_rejects_malformed_embed_and_ends_process_tree(source):
    root,store,clock = source
    async def main():
        worker = factory(root,FAKE_SELFIE_BAD_EMBED="1")()
        await worker.start()
        with pytest.raises(WorkerError,match="embedding_invalid"):
            await worker.embed(files.chosen(root,101))
        assert not worker.running
        await worker.stop()
    run(main())


def test_worker_cancellation_ends_embedding_process(source):
    root,store,clock = source
    async def main():
        worker = factory(root,FAKE_SELFIE_HANG="1")()
        await worker.start()
        task = asyncio.create_task(worker.embed(files.chosen(root,101)))
        await asyncio.sleep(.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not worker.running
        await worker.stop()
    run(main())
