"""T10: `python -m leeroy_mind tuning status|history|undo|revert`. Reading never writes or
migrates; undo and revert hold the instance lock, so they run only with the mind stopped, and
take a snapshot first."""
import hashlib
from datetime import datetime, timezone

import pytest

from conftest import HUMAN, seed_test_cast
from leeroy_mind.__main__ import main
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.personas.io import load_persona
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from dbtemplate import open_migrated
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.tuning import changes as C

CLOCK = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "mind.toml").write_text("[talk]\nchattiness = 4\n", encoding="utf-8")
    s = Store(open_migrated(tmp_path / "mind.db"), clock=FakeClock(CLOCK))
    try:
        seed_test_cast(s)
        s.upsert_cast([Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0, level=80,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
        s.set_meta("human_guid", str(HUMAN))
        pic = C.picture(s, {g: load_persona(s, g) for g in s.persona_versions()})
        C.apply_sets(s, [C.Set("member", "102", "talkative", 8)], pic, source="console", now=2.0)
        C.apply_sets(s, [C.Set("world", "", "chattiness", 7)], pic, source="console", now=3.0)
    finally:
        s.close()
    return tmp_path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_status_and_history_read_without_writing(root, capsys):
    before = _digest(root / "mind.db")
    assert main(["tuning", "--data-dir", str(root), "status"]) == 0
    out = capsys.readouterr().out
    assert "version 2" in out and "chattiness 7 (mind.toml: 4)" in out
    assert "Belra (102): talkative 8" in out and "1 member" in out
    assert main(["tuning", "--data-dir", str(root), "history"]) == 0
    out = capsys.readouterr().out
    assert out.index("#2") < out.index("#1") and "world chattiness unset -> 7" in out
    assert _digest(root / "mind.db") == before
    assert not (root / "snapshots").exists()


def test_undo_and_revert_take_a_snapshot_and_write_one_change(root, capsys):
    assert main(["tuning", "--data-dir", str(root), "undo", "2"]) == 0
    assert "undo #2: saved as change #3" in capsys.readouterr().out
    assert list((root / "snapshots").glob("mind-tuning-*.db"))
    assert main(["tuning", "--data-dir", str(root), "revert", "0"]) == 0
    capsys.readouterr()
    s = Store(open_migrated(root / "mind.db"), clock=FakeClock(CLOCK))
    try:
        assert s.conn.execute("SELECT count(*) FROM tuning_value").fetchone()[0] == 0
        assert s.conn.execute("SELECT source FROM tuning_change WHERE id=4").fetchone()[0] == "cli"
    finally:
        s.close()


def test_with_the_mind_running_a_write_is_refused(root, capsys):
    with acquire_instance_lock(root):
        assert main(["tuning", "--data-dir", str(root), "undo", "2"]) == 1
    out = capsys.readouterr()
    assert "the mind is running" in (out.out + out.err)
    assert main(["tuning", "--data-dir", str(root), "history"]) == 0  # reading still works


def test_a_refused_undo_says_why(root, capsys):
    s = Store(open_migrated(root / "mind.db"), clock=FakeClock(CLOCK))
    try:
        pic = C.picture(s, {g: load_persona(s, g) for g in s.persona_versions()})
        C.apply_sets(s, [C.Set("member", "102", "talkative", 9)], pic, source="console", now=4.0)
    finally:
        s.close()
    assert main(["tuning", "--data-dir", str(root), "undo", "1"]) == 1
    assert "changed since by #3" in capsys.readouterr().err


def test_an_older_database_is_read_without_being_migrated(tmp_path, capsys, monkeypatch):
    from leeroy_mind.storage import db
    files = db._migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n <= 16])
        open_db(tmp_path / "mind.db").close()
    before = _digest(tmp_path / "mind.db")
    assert main(["tuning", "--data-dir", str(tmp_path), "status"]) == 0
    assert "no tuning tables" in capsys.readouterr().out
    assert main(["tuning", "--data-dir", str(tmp_path), "undo", "1"]) == 1
    assert "db migrate" in capsys.readouterr().err
    assert _digest(tmp_path / "mind.db") == before
