"""training/inspect.py gives the console what the CLI prints, from the same readers."""
import json

from leeroy_mind.training import cli, inspect
from .test_tg_report import add


def _out(capsys, *argv, tmp_path):
    assert cli.training_main([*argv, "--data-dir", str(tmp_path)]) == 0
    return json.loads(capsys.readouterr().out)


def test_inspect_gives_what_the_cli_printed(fake, tmp_path, capsys):
    add(fake, 1)
    with fake.conn:
        fake.conn.execute("INSERT INTO meta(key,value) VALUES('tg_gate',?)",
                          (json.dumps({"phase": "passed", "raids": 3, "verdict_ms": 0.4}),))
        fake.conn.execute("INSERT INTO tg_card(id,context,version,status,origin,directives,call,reason,created,changed)"
                          " VALUES(1,'631:0:0',1,'champion','stock','[]','hold','why',10,10)")
        fake.conn.execute("INSERT INTO tg_trial(id,scope,lever,metric,status,opened) VALUES(1,'631:0:0','x','raid','open',5)")
        fake.conn.execute("INSERT INTO tg_arm(id,trial,card,champion,status) VALUES(1,1,1,1,'live')")
    assert inspect.gate_state(fake.conn) == _out(capsys, "gate", tmp_path=tmp_path)
    assert inspect.trials_view(fake.conn, None) == _out(capsys, "trials", tmp_path=tmp_path)
    assert inspect.trials_view(fake.conn, "631:0:0") == _out(capsys, "trials", "--context", "631:0:0", tmp_path=tmp_path)
    assert inspect.cards_view(fake.conn, None) == _out(capsys, "cards", tmp_path=tmp_path)
    assert inspect.cards_view(fake.conn, "631:0:0")["cards"][0]["call"] == "hold"
    status = _out(capsys, "status", tmp_path=tmp_path)
    lines = [f"[{'x' if i['done'] else ' '}] {i['label']}" + (" (waiting for recording)" if i["waiting"] else "")
             for i in inspect.checklist(status["done_when"])]
    assert lines == status["checklist"]


def test_the_checklist_has_the_six_milestones_in_order():
    items = inspect.checklist({"four_weeks": True})
    assert [i["key"] for i in items] == [k for k, _ in inspect.CHECKLIST] and len(items) == 6
    assert items[0]["done"] is True and items[1]["done"] is False
    assert [i["waiting"] for i in items] == [False, False, False, False, True, True]
    gated = inspect.checklist({"gate_within_1ms": True, "voice_run": True})
    assert [i["waiting"] for i in gated] == [False] * 6
