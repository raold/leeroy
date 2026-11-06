"""Track S6: `python -m leeroy_mind.arc --data-dir D end <guid>`, ending a romance by hand. It
holds the instance lock, so it runs only with the mind stopped, refuses a database older than
the code and takes a snapshot first."""
import hashlib
import runpy
import sys
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from conftest import HUMAN, make_persona, seed_test_cast
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.arc.__main__ import main
from leeroy_mind.arc.state import Arc, get_arc, put_arc
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock

CLOCK = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc)
T = CLOCK.timestamp()


def _store(root):
    return Store(open_db(root / "mind.db"), clock=FakeClock(CLOCK))


@pytest.fixture
def root(tmp_path):
    """A stopped mind's data dir: the test cast, the human, and Arkon (101) home from her
    weekend with him and away again on a later visit."""
    s = _store(tmp_path)
    try:
        seed_test_cast(s)
        s.upsert_cast([Member(guid=HUMAN, name="Testmage", race=5, class_=8, gender=0, level=80,
                              rank=0, online=False, map=0, zone=0, spec_tab=-1)], 1.0)
        s.set_meta("human_guid", str(HUMAN))
        save_persona(s, make_persona(101, "Arkon", stance="lust", player_gender="female"),
                     source="seed", ts=2.0, race="Orc", class_name="Warrior")
        rel = s.get_relationship(101, HUMAN)
        s.put_relationship(replace(rel, attraction=90, romance_stage="after"))
        put_arc(s, Arc(101, HUMAN, plan={"kind": "event", "target": T, "worries": [],
                                         "cover": ""},
                       plan_state="set", plan_at=T - 86400, weekends=1, last_meetup=T - 9e5,
                       updated=T))
        with s.transaction():
            s.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at, returned_at) "
                           "VALUES (101, 'visit', ?, ?, NULL)", (T - 3600, T + 86400))
    finally:
        s.close()
    return tmp_path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _end(root, guid):
    return main(["--data-dir", str(root), "end", str(guid)])


def test_the_cli_refuses_while_the_mind_runs_and_ends_it_when_stopped(root, capsys):
    before = _digest(root / "mind.db")
    with acquire_instance_lock(root):
        assert _end(root, 101) == 1
    out = capsys.readouterr()
    assert "the mind is running" in out.err
    assert _digest(root / "mind.db") == before and not (root / "snapshots").exists()
    assert _end(root, 101) == 0
    out = capsys.readouterr().out
    assert "101: after -> ex" in out
    assert "Arkon" not in out and "Belra" not in out
    assert list((root / "snapshots").glob("mind-arc-*.db"))
    s = _store(root)
    try:
        assert s.get_relationship(101, HUMAN).romance_stage == "ex"
        arc = get_arc(s, 101, HUMAN)
        assert (arc.ended, arc.plan_state, arc.plan) == ("owner", "none", {})
        assert s.conn.execute("SELECT count(*) FROM away WHERE returned_at IS NULL"
                              ).fetchone()[0] == 0
        assert [r[0] for r in s.conn.execute(
            "SELECT reason FROM rel_delta WHERE a = 101")] == ["after->ex"]
    finally:
        s.close()
    assert _end(root, 101) == 1                          # already history: nothing to end
    assert "already" in capsys.readouterr().err
    assert len(list((root / "snapshots").glob("mind-arc-*.db"))) == 1


def test_a_member_with_no_romance_is_refused_before_any_snapshot(root, capsys):
    before = _digest(root / "mind.db")
    assert _end(root, 102) == 1                          # stage none
    assert "no romance" in capsys.readouterr().err
    assert _end(root, 999) == 1                          # no persona
    assert "no persona" in capsys.readouterr().err
    assert _digest(root / "mind.db") == before and not (root / "snapshots").exists()


def test_an_older_database_is_refused(tmp_path, capsys, monkeypatch):
    from leeroy_mind.storage import db
    files = db._migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, "_migration_files", lambda: files[:-1])    # the newest one missing
        open_db(tmp_path / "mind.db").close()
    before = _digest(tmp_path / "mind.db")
    assert _end(tmp_path, 101) == 1
    assert "db migrate" in capsys.readouterr().err
    assert _digest(tmp_path / "mind.db") == before


def test_no_database_is_refused(tmp_path, capsys):
    assert _end(tmp_path, 101) == 1
    assert "no mind.db" in capsys.readouterr().err


def test_python_m_runs_it(root, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["leeroy_mind.arc", "--data-dir", str(root), "end", "101"])
    monkeypatch.delitem(sys.modules, "leeroy_mind.arc.__main__")   # imported above, for main
    with pytest.raises(SystemExit) as done:
        runpy.run_module("leeroy_mind.arc", run_name="__main__", alter_sys=True)
    assert done.value.code == 0
    assert "101: after -> ex" in capsys.readouterr().out
