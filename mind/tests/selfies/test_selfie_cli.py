"""`selfies status` and `selfies try` over scratch data, the fake worker and a FakeLLM: no
model, no GPU, no live data. Every person here is invented."""
import json
import sqlite3
from dataclasses import asdict, replace

import pytest

from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.config import load_config
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.selfies import cli
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.wow import CLASSES, RACES
from nightly.conftest import scripted
from selfies.fakes import PHOTO, fake_command, mind_app


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
