import hashlib
import asyncio
import json
import sqlite3

import pytest

from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.training import cli


@pytest.mark.parametrize("args", [["status"], ["gate"], ["report", "--week", "2026-W44"],
                                 ["voicecheck", "--night", "2026-11-02"]])
def test_read_only_commands_preserve_database(fake, tmp_path, args, capsys):
    path = tmp_path / "mind.db"
    before = hashlib.sha256(path.read_bytes()).digest()
    assert cli.training_main([*args, "--data-dir", str(tmp_path)]) == 0
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert not (tmp_path / "mind.lock").exists()
    assert isinstance(json.loads(capsys.readouterr().out), dict)


@pytest.mark.parametrize("args", [["gate", "--reset"], ["rescore"]])
def test_mutation_refuses_held_service_lock(fake, tmp_path, args):
    with acquire_instance_lock(tmp_path):
        assert cli.training_main([*args, "--data-dir", str(tmp_path)]) == 1


def test_reset_clears_experiment_evidence_and_parity_only(fake, tmp_path):
    with fake.conn:
        for key in ("tg_gate", "tg_gate:raid:1", "tg_gate:perf:gate:1", "keep"):
            fake.conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (key, "{}"))
        for fight in ("gate:1", "future-other"):
            fake.conn.execute("INSERT INTO tg_perf(ts,fight,context,kind,armed,mean_ms) VALUES (0,?,'631:0:0','offline',0,1)", (fight,))
    assert cli.training_main(["gate", "--reset", "--data-dir", str(tmp_path)]) == 0
    assert fake.conn.execute("SELECT key FROM meta WHERE key GLOB 'tg_gate*'").fetchall() == []
    assert [r[0] for r in fake.conn.execute("SELECT fight FROM tg_perf")] == ["future-other"]
    assert fake.conn.execute("SELECT value FROM meta WHERE key='keep'").fetchone() is not None


def test_lock_is_held_for_entire_rescore(fake, tmp_path, monkeypatch):
    from leeroy_mind.app import AlreadyRunning
    async def rescore(conn, root):
        with pytest.raises(AlreadyRunning):
            acquire_instance_lock(root)
        conn.execute("SELECT * FROM tg_pull")
        return 0
    monkeypatch.setattr(cli, "_rescore", rescore)
    assert cli.training_main(["rescore", "--data-dir", str(tmp_path)]) == 0
    with acquire_instance_lock(tmp_path):
        pass


def test_missing_or_old_database_never_created_or_migrated(tmp_path):
    assert cli.training_main(["status", "--data-dir", str(tmp_path)]) == 1
    assert not (tmp_path / "mind.db").exists()
    conn = sqlite3.connect(tmp_path / "mind.db")
    conn.execute("PRAGMA user_version=7")
    conn.close()
    before = (tmp_path / "mind.db").read_bytes()
    assert cli.training_main(["gate", "--reset", "--data-dir", str(tmp_path)]) == 1
    assert (tmp_path / "mind.db").read_bytes() == before


def test_main_dispatches_training(monkeypatch):
    from leeroy_mind.__main__ import main
    seen = []
    monkeypatch.setattr(cli, "training_main", lambda argv: seen.append(argv) or 7)
    assert main(["training", "status"]) == 7
    assert seen == [["status"]]


def _eval_store(conn, clock):
    from conftest import seed_test_cast
    from leeroy_mind.protocol import Member
    from leeroy_mind.storage.store import Store
    store = Store(conn, clock=clock)
    seed_test_cast(store)
    store.upsert_cast([Member(1, "Testmage", 10, 8, 0, 80, 0, False, 0, 0, -1)], clock.now())
    store.set_meta("human_guid", "1")
    return store


def test_fresh_eval_cli_discovers_training_from_evaluated_config_and_copy(fake, tmp_path, monkeypatch):
    from pathlib import Path
    from leeroy_mind import eval as weekly
    from leeroy_mind.nightly import cli as nightly_cli
    from leeroy_mind.training import service
    _eval_store(fake.conn, fake.clock)
    (tmp_path / "mind.toml").write_text("[training]\nenabled=true\n", encoding="utf-8")
    monkeypatch.setattr(weekly, "_sections", {})
    monkeypatch.setattr(nightly_cli, "SystemClock", lambda: fake.clock)
    original = service.report_section
    seen = []
    def section(ctx, week):
        seen.append(Path(ctx.store.conn.execute("PRAGMA database_list").fetchone()[2]))
        return original(ctx, week)
    monkeypatch.setattr(service, "report_section", section)
    before = fake.conn.execute("SELECT count(*) FROM meta").fetchone()[0]
    assert nightly_cli.eval_main(["--data-dir", str(tmp_path), "--week", "2026-W44", "--no-llm"],
        make_llm=lambda: pytest.fail("no-llm constructed a client")) == 0
    report = json.loads((tmp_path / "reports" / "2026-W44.json").read_text(encoding="utf-8"))
    assert "training" in report and "done_when" in report["training"]
    assert seen == [tmp_path / "scratch" / "mind-eval-2026-W44-trial.db"]
    assert fake.conn.execute("SELECT count(*) FROM meta").fetchone()[0] == before
    assert weekly._sections == {}  # Discovery is per evaluation, not global mutation.


def test_enabled_attachment_does_not_leak_training_into_disabled_schema7_eval(app_stub, tmp_path, monkeypatch):
    from leeroy_mind import eval as weekly
    from leeroy_mind.config import load_config
    from leeroy_mind.nightly import cli as nightly_cli
    from leeroy_mind.storage import db
    from leeroy_mind.training import service
    from .test_tg_service import enable
    monkeypatch.setattr(weekly, "_sections", {})
    service.attach_training(enable(app_stub))
    assert "training" in weekly._sections
    weekly.register_section("synthetic_extra", lambda ctx, week: {"status": "complete"})
    root = tmp_path / "disabled"
    root.mkdir()
    (root / "mind.toml").write_text("[training]\nenabled=false\n", encoding="utf-8")
    migrations = db._migration_files()
    with monkeypatch.context() as schema7:
        schema7.setattr(db, "_migration_files", lambda: [(version, path) for version, path in migrations if version <= 7])
        conn = db.open_db(root / "mind.db")
    try:
        store = _eval_store(conn, app_stub.clock)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='tg_pull'").fetchone() is None
        async def evaluate():
            async with nightly_cli._context(root, store, load_config(root / "mind.toml"), app_stub.clock,
                "2026-11-01", make_llm=lambda: pytest.fail("constructed a model"), use_llm=False) as ctx:
                return await weekly.run_weekly(ctx, "2026-W44", use_llm=False)
        path = asyncio.run(evaluate())
        report = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        assert "training" not in report
        assert report["synthetic_extra"] == {"status": "complete"}
        assert json.loads(store.get_meta("nightly:eval-result:2026-W44"))["complete"] is True
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    finally:
        conn.close()
