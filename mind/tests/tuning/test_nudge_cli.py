import hashlib

from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.tuning.cli import tuning_main
from tuning.test_tuning_preview import _app
from tuning.test_nudges import ARGS
from conftest import run


def test_nudge_read_and_stopped_undo_revert(tmp_path, capsys):
    async def seed():
        app = _app(tmp_path)
        try:
            assert (await app.tuning.handle(ARGS))["ok"]
        finally:
            app.store.close()
    run(seed())
    db = tmp_path / "mind.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    args = ["--data-dir", str(tmp_path)]
    assert tuning_main([*args, "nudges"]) == 0
    assert "nudge #1 member 102" in capsys.readouterr().out
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    with acquire_instance_lock(tmp_path):
        assert tuning_main([*args, "undo", "1"]) == 1
    assert "running" in capsys.readouterr().err
    assert tuning_main([*args, "undo", "1"]) == 0
    assert tuning_main([*args, "revert", "1"]) == 0
    assert list((tmp_path / "snapshots").glob("mind-tuning-*.db"))
