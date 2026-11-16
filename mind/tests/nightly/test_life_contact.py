"""`nightly life-contact` (2026-11-15): offscreen memories that put him in a member's life off the
game are retired (archived, never deleted) and others that name him or touch her secret are
made private with him, each with a receipt; a dry run writes nothing, --go refuses while the
mind runs and snapshots first. Invented names only."""
import json

from nightly.conftest import IDENT
from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.config import load_config
from leeroy_mind.nightly.cli import nightly_main
from leeroy_mind.nightly.life_contact import RESCOPED_KEY, RETIRED_KEY, apply, plan, run

CONTACT = "Testmage's text lit up my screen: still at work?"
NAMED = "Kept thinking about the raid Testmage led."
SECRET = "Another thursday shift at the casino."
PLAIN = "The register drawer jammed again."


def seed(h):
    h.persona(101, secret="works a second job at the casino on thursdays")
    ids = {text: h.mem(101, text, source="offscreen", evidence=(f"beat:{n}",))
           for n, text in enumerate((CONTACT, NAMED, SECRET, PLAIN), 1)}
    ids["kept"] = h.mem(102, "Testmage's note was on my desk.", source="offscreen",
                        status="archived", evidence=("beat:9",))
    ids["chat"] = h.mem(102, CONTACT, source="chat")  # not a night's beat: left alone
    # the arc's weekend with him and an encounter are offscreen but no life beat: left alone
    ids["arc"] = h.mem(102, "Testmage drove me to the airport after our weekend.",
                       source="offscreen", evidence=("arc:weekend:1",))
    ids["encounter"] = h.mem(103, "Saw Testmage's raid call in guild chat.", source="offscreen",
                             evidence=("encounter:5",))
    return ids


def state(h, mid):
    h.store.conn.commit()  # a fresh snapshot: the command writes on its own connection
    return tuple(h.store.conn.execute("SELECT status, scope, scope_with FROM memory WHERE id=?",
                                      (mid,)).fetchone())


def test_plan_and_apply_with_receipts(h):
    ids = seed(h)
    ident = IDENT.__class__(1, "Testmage", "", "", True, "he")
    p = plan(h.store, ident)
    assert [m for m, _, _ in p.retire] == [ids[CONTACT]]
    assert sorted(m for m, _, _ in p.rescope) == sorted([ids[NAMED], ids[SECRET]])
    assert apply(h.store, p, human=1, now=123.0) == (1, 2)
    assert state(h, ids[CONTACT]) == ("archived", "open", 0)
    assert state(h, ids[NAMED]) == state(h, ids[SECRET]) == ("active", "private", 1)
    assert state(h, ids[PLAIN]) == ("active", "open", 0)
    assert state(h, ids["chat"])[0] == "active"
    assert state(h, ids["arc"]) == state(h, ids["encounter"]) == ("active", "open", 0)
    assert json.loads(h.store.get_meta(RETIRED_KEY.format(id=ids[CONTACT]))) == {
        "status": "active", "at": 123.0}
    assert json.loads(h.store.get_meta(RESCOPED_KEY.format(id=ids[NAMED])))["scope"] == "open"
    assert h.store.conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 8  # none deleted
    assert apply(h.store, plan(h.store, ident), human=1, now=124.0) == (0, 0)  # idempotent


def test_dry_run_writes_nothing_and_go_snapshots(h, tmp_path, capsys):
    ids = seed(h)
    cfg = load_config(tmp_path / "missing.toml")
    assert run(tmp_path, cfg) == 0
    out = capsys.readouterr().out
    assert "to retire 1, to make private 2" in out and "dry run" in out
    assert CONTACT not in out  # texts only with --show
    assert state(h, ids[CONTACT])[0] == "active"
    assert not (tmp_path / "snapshots").exists()
    assert run(tmp_path, cfg, go=True) == 0
    assert list((tmp_path / "snapshots").glob("mind-pre-life-contact-*.db"))
    assert state(h, ids[CONTACT])[0] == "archived"


def test_go_refuses_while_the_mind_runs(h, tmp_path, capsys):
    ids = seed(h)
    cfg = load_config(tmp_path / "missing.toml")
    with acquire_instance_lock(tmp_path):
        assert run(tmp_path, cfg, go=True) == 1
    assert "refused" in capsys.readouterr().out
    assert state(h, ids[CONTACT])[0] == "active"


def test_the_cli_runs_it(h, tmp_path, capsys):
    seed(h)
    (tmp_path / "mind.toml").write_text("", encoding="utf-8")
    assert nightly_main(["life-contact", "--data-dir", str(tmp_path), "--show"]) == 0
    out = capsys.readouterr().out
    assert CONTACT in out and "dry run" in out
