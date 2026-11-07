"""`selfies status` and `selfies try` over scratch data, the fake worker and a FakeLLM: no
model, no GPU, no live data. Every person here is invented."""
import json
import sqlite3
from dataclasses import asdict, replace

import pytest

from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.config import load_config
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.selfies import cli, files
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import CLASSES, RACES
from leeroy_mind.selfies.store import pick as pick_photo
from nightly.conftest import scripted
from nightly.test_night_scenes import SCENE_REPLY
from selfies.fakes import (PHOTO, fake_command, mind_app, planned_snap, ready, ready_snap,
                         tagged_jpeg)


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / "mind-data"
    root.mkdir()
    app = mind_app(root)
    try:
        p = load_persona(app.store, 102)
        save_persona(app.store, replace(p, photo=None), source="seed", ts=1000.0,
                     race=RACES[1], class_name=CLASSES[1])
    finally:
        app.store.close()
    monkeypatch.setattr(cli, "worker_command", lambda cfg: fake_command(tmp_path)[0])
    monkeypatch.setattr(cli, "worker_env", lambda cfg: fake_command(tmp_path)[1])
    return root


def llm(root, replies=None):
    cfg = load_config(root / "mind.toml")
    return FakeLLM(script=scripted({"photo": replies if replies is not None else [asdict(PHOTO)]}),
                   models={cfg.models.live, cfg.models.embedding, cfg.models.reflection})


def test_status_reads_without_migrating_an_older_database(tmp_path, capsys):
    root = tmp_path / "old"
    root.mkdir()
    (root / "mind.toml").write_text("", encoding="utf-8")
    conn = sqlite3.connect(root / "mind.db")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    before = (root / "mind.db").read_bytes()
    assert cli.selfies_main(["--data-dir", str(root), "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queue"]["available"] is False and out["stages"] == {}
    assert (root / "mind.db").read_bytes() == before


def test_status_prints_counts_and_guids_never_names(root, capsys):
    assert cli.selfies_main(["--data-dir", str(root), "status"]) == 0
    text = capsys.readouterr().out
    out = json.loads(text)
    assert out["queue"]["available"] is True and out["queue"]["counts"]["no_kind"] == 1
    assert "name" not in text.lower().replace("no_persona", "")


def test_try_refuses_while_the_mind_holds_its_lock(root, capsys):
    with acquire_instance_lock(root):
        code = cli.selfies_main(["--data-dir", str(root), "try"], make_llm=lambda cfg: llm(root))
    assert code == 1
    assert "mind service is running" in capsys.readouterr().out
    assert not (root / "scratch" / "selfies-try").exists()


def test_try_renders_into_scratch_and_writes_nothing_live(root, capsys, monkeypatch):
    seen = {}
    real = cli.render_candidates

    async def spy(*args, **kw):
        seen["leases"] = kw.get("leases")
        return await real(*args, **kw)
    monkeypatch.setattr(cli, "render_candidates", spy)
    live = (root / "mind.db").read_bytes()
    code = cli.selfies_main(["--data-dir", str(root), "try", "--guid", "102"],
                            make_llm=lambda cfg: llm(root))
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["members"] == 1 and out["sets"] == 1 and out["flagged"] == 0
    assert out["peak_gib"] == 10.5 and out["seconds_per_image"] > 0
    folder = root / "scratch" / "selfies-try" / "102"
    assert sorted(p.name for p in folder.glob("c?.jpg")) == ["c0.jpg", "c1.jpg", "c2.jpg", "c3.jpg"]
    assert out["path"] == str(root / "scratch" / "selfies-try")
    assert (root / "mind.db").read_bytes() == live
    assert seen["leases"]() == frozenset()


def test_try_defaults_to_two_members_with_a_photo_kind_or_one_made(root, capsys):
    code = cli.selfies_main(["--data-dir", str(root), "try"], make_llm=lambda cfg: llm(root))
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["members"] == 2 and out["sets"] == 2


def test_try_exits_1_when_a_set_does_not_finish(root, capsys):
    code = cli.selfies_main(["--data-dir", str(root), "try", "--guid", "102"],
                            make_llm=lambda cfg: llm(root, replies=[{"kind": "bad"}] * 6))
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["sets"] == 0 and out["members"] == 1


def test_try_refuses_a_guid_without_looks(root, capsys):
    code = cli.selfies_main(["--data-dir", str(root), "try", "--guid", "104"],
                            make_llm=lambda cfg: llm(root))
    assert code == 1 and "104" in capsys.readouterr().err


def test_try_retries_a_vram_refusal_once_on_the_same_worker(root, capsys, monkeypatch, tmp_path):
    starts = tmp_path / "starts.log"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_VRAM="1", FAKE_SELFIE_STARTS=str(starts))
    monkeypatch.setattr(cli, "worker_command", lambda cfg: cmd)
    monkeypatch.setattr(cli, "worker_env", lambda cfg: env)
    code = cli.selfies_main(["--data-dir", str(root), "try", "--guid", "101"],
                            make_llm=lambda cfg: llm(root))
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["sets"] == 1
    assert [n.split()[0] for n in starts.read_text().splitlines()] == ["spawn", "retry"]


def test_try_drops_what_loaded_beside_its_worker_once_the_worker_is_gone(root, capsys, monkeypatch):
    # final review C1: mod-ollama-chat runs while the mind is stopped, so a try can leave Orion
    # split onto the CPU for the mind it hands back to
    from leeroy_mind.nightly.gpu import GpuArbiter
    from leeroy_mind.selfies.worker import SelfieWorker
    seen, make_room, stop = [], GpuArbiter.make_room, SelfieWorker.stop

    async def room(self):
        seen.append("room")
        await make_room(self)

    async def stopped(self):
        await stop(self)
        seen.append("stopped")
    monkeypatch.setattr(GpuArbiter, "make_room", room)
    monkeypatch.setattr(SelfieWorker, "stop", stopped)
    code = cli.selfies_main(["--data-dir", str(root), "try", "--guid", "101"],
                            make_llm=lambda cfg: llm(root))
    assert code == 0 and json.loads(capsys.readouterr().out)["sets"] == 1
    assert seen == ["room", "stopped", "room"]


def snap_llm(root, scenes=None):
    cfg = load_config(root / "mind.toml")
    return FakeLLM(script=scripted({"scene": scenes if scenes is not None else [SCENE_REPLY] * 4}),
                   models={cfg.models.live, cfg.models.embedding, cfg.models.reflection})


@pytest.fixture
def calls():
    return []


@pytest.fixture
def snaproot(tmp_path, monkeypatch, calls):
    """101 and 102 with a chosen photo (a planned and a ready snap among them), 103 without; the
    fake worker in identity mode."""
    root = tmp_path / "mind-data"
    root.mkdir()

    def prepare(store):
        for guid in (101, 102):
            ready(store, root, guid, 1, now=1.0)
            pick = pick_photo(store, guid, 1, 0, source="console", now=2.0)
            hist = files.history(root, guid, pick)
            hist.parent.mkdir(parents=True, exist_ok=True)
            hist.write_bytes(tagged_jpeg(f"history/{guid}"))
        planned_snap(store, 101)
        ready_snap(store, root, 102)
    mind_app(root, prepare=prepare).store.close()
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_IDENTITY="1",
                            FAKE_SELFIE_LOG=str(tmp_path / "log"))
    monkeypatch.setattr(cli, "worker_command",
                        lambda cfg, identity=False: calls.append(identity) or cmd)
    monkeypatch.setattr(cli, "worker_env", lambda cfg: env)
    return root


def test_status_counts_snaps_without_migrating(snaproot, capsys):
    before = (snaproot / "mind.db").read_bytes()
    assert cli.selfies_main(["--data-dir", str(snaproot), "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["snaps"] == {"planned": 1, "ready": 1, "sent_week": 0, "expired_week": 0}
    assert (snaproot / "mind.db").read_bytes() == before


def test_try_snaps_renders_beside_the_reference_into_scratch_and_writes_nothing_live(
        snaproot, capsys, tmp_path, calls):
    live = (snaproot / "mind.db").read_bytes()
    picks = {101: 1, 102: 2}
    hist = {g: files.history(snaproot, g, picks[g]).read_bytes() for g in (101, 102)}
    code = cli.selfies_main(["--data-dir", str(snaproot), "try", "--snaps"],
                            make_llm=lambda cfg: snap_llm(snaproot))
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["rendered"] == 2 and out["members"] == 2
    assert out["no_photo"] == 1 and out["flagged"] == 0 and out["seconds_per_image"] > 0
    base = snaproot / "scratch" / "selfies-try" / "snaps"
    assert out["path"] == str(base)
    for guid in (101, 102):
        assert (base / str(guid) / "snap.jpg").is_file()
        assert (base / str(guid) / "ref.jpg").read_bytes() == hist[guid]
        assert files.history(snaproot, guid, picks[guid]).read_bytes() == hist[guid]
    assert not (base / "guild.jpg").exists()
    assert calls == [True]
    assert " identity " in (tmp_path / "log").read_text()
    assert (snaproot / "mind.db").read_bytes() == live
    assert not list((snaproot / "selfies" / "101").glob("snaps*"))


def test_try_snaps_guild_makes_one_grid(snaproot, capsys):
    code = cli.selfies_main(["--data-dir", str(snaproot), "try", "--snaps", "--guild"],
                            make_llm=lambda cfg: snap_llm(snaproot))
    assert code == 0 and json.loads(capsys.readouterr().out)["rendered"] == 2
    assert (snaproot / "scratch" / "selfies-try" / "snaps" / "guild.jpg").is_file()


def test_try_snaps_skips_members_without_a_chosen_photo(snaproot, capsys):
    code = cli.selfies_main(["--data-dir", str(snaproot), "try", "--snaps", "--guid", "103"],
                            make_llm=lambda cfg: snap_llm(snaproot))
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["members"] == 0 and out["no_photo"] == 1 and out["rendered"] == 0
    assert not list((snaproot / "scratch" / "selfies-try" / "snaps").glob("*/snap.jpg"))


def test_try_snaps_exits_1_when_a_scene_is_declined(snaproot, capsys):
    code = cli.selfies_main(["--data-dir", str(snaproot), "try", "--snaps"],
                            make_llm=lambda cfg: snap_llm(snaproot, [{**SCENE_REPLY, "photo": False}] * 2))
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["rendered"] == 0 and out["members"] == 2


def test_try_snaps_refuses_while_the_mind_holds_its_lock(snaproot, capsys):
    with acquire_instance_lock(snaproot):
        code = cli.selfies_main(["--data-dir", str(snaproot), "try", "--snaps"],
                                make_llm=lambda cfg: snap_llm(snaproot))
    assert code == 1 and "mind service is running" in capsys.readouterr().out
    assert not (snaproot / "scratch" / "selfies-try").exists()
