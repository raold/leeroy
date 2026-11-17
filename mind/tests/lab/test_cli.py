"""`lab report` and `lab run --dry-run` read only; the report renders stored numbers; the health
note and the console's panel."""
import asyncio
import json
import time

from lab.rig import digest, make_live
from lab.test_night import run_night
from leeroy_mind.lab import report
from leeroy_mind.lab.cli import lab_main


def test_report_on_a_new_database_and_the_dry_run_write_nothing(tmp_path, capsys):
    live = make_live(tmp_path)
    before = digest(live)
    assert lab_main(["--data-dir", str(live), "report"]) == 0
    assert "no runs yet" in capsys.readouterr().out
    assert lab_main(["--data-dir", str(live), "run", "--dry-run", "--pairs", "5"]) == 0
    out = capsys.readouterr().out
    assert "piece reply_human, 5 pairs" in out and "Nothing was started or written." in out
    assert "grader rubric, promoting a winner" in out   # the rig's table; the defaults only measure
    assert out.count("  pair ") == 5
    assert lab_main(["--data-dir", str(live), "run", "--dry-run", "--scripts", "flirt-1,meet-1",
                     "--members", "102", "--pairs", "2"]) == 0
    out = capsys.readouterr().out
    assert "flirt-1" in out and "meet-1" in out and "#102" in out and "#101" not in out
    assert lab_main(["--data-dir", str(live), "run", "--dry-run", "--scripts", "nope"]) == 2
    assert digest(live) == before


def test_the_report_health_and_panel_after_a_night(tmp_path, capsys):
    live = make_live(tmp_path)
    run_night(live, tmp_path)
    assert lab_main(["--data-dir", str(live), "report"]) == 0
    out = capsys.readouterr().out
    assert "piece reply_human" in out and "control: rubric 1.00 over 8 of 8 answers" in out
    assert "won" in out and "{+Plainly" in out
    assert lab_main(["--data-dir", str(live), "report", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    [run] = data["runs"]
    assert run["status"] == "done" and len(run["variants"]) == 2
    import sqlite3
    conn = sqlite3.connect(live / "mind.db")
    try:
        panel = report.panel(conn)
        assert panel["available"] and panel["tried"] == 2 and panel["promoted"] == 1
        assert panel["runs"][0]["rubric"] == 1.0 and panel["runs"][0]["kinds"]
        assert report.last_completed(conn) is not None
        from leeroy_mind.health import _conversation_lab
        ok = _conversation_lab(conn, live, time.time())
        assert ok.status == "ok" and "last night run finished" in ok.text
        stale = _conversation_lab(conn, live, time.time() + 5 * 86400)
        assert stale.status == "note" and "no night run finished in 3 nights" in stale.text
        from leeroy_mind.console.labview import _conversation, lab_view
        assert _conversation(conn)["promoted"] == 1
        conn.row_factory = sqlite3.Row
        view = lab_view(conn, now=time.time())  # a lab arm in the registry reads as any arm
        assert "prompt.reply_human" in view["families"] and "prompt.continue" not in view["families"]
        assert any(a["id"].startswith("lab.reply_human.") for a in view["arms"]["arms"])
        assert view["conversation"]["promoted"] == 1
    finally:
        conn.close()
    (tmp_path / "off").mkdir()
    off = make_live(tmp_path / "off", "[lab]\nenabled = false\n")
    import sqlite3 as s
    c2 = s.connect(off / "mind.db")
    try:
        from leeroy_mind.health import _conversation_lab
        assert _conversation_lab(c2, off, time.time()).text.startswith("conversation lab: off")
    finally:
        c2.close()
