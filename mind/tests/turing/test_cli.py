"""`turing report` and `turing run`: read only, on invented data in a scratch data dir."""
import json
import sqlite3
from pathlib import Path

from leeroy_mind.__main__ import main
from leeroy_mind.protocol import Chat
from leeroy_mind.turing import store as tstore

T0 = 1_800_000_000.0


def data_dir(store) -> Path:
    return Path(store.conn.execute("PRAGMA database_list").fetchone()[2]).parent


def whispers(store, now):
    store.set_meta("human_guid", "1")
    t = now - 5 * 86400
    for i in range(40):
        m = 101 + i % 3
        t += 3000
        store.insert_chat_line(Chat(0, "whisper", 1, "Testmage", m, "ok sure", 0, 0),
                               session_id=None, origin="human", ts=t)
        store.insert_chat_line(Chat(0, "whisper", m, f"M{m}", 1, "That's lovely, I'm glad.", 0, 0),
                               session_id=None, origin="mind", ts=t + 15)


def test_run_measures_now_and_writes_nothing(store, capsys, monkeypatch):
    import time
    now = time.time()
    whispers(store, now)
    root = data_dir(store)
    before = store.conn.execute("SELECT count(*) FROM meta").fetchone()[0]
    assert main(["turing", "run", "--data-dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Whispers and DMs: 40 of your lines, 40 of theirs" in out
    assert "contractions with an apostrophe (i'm, don't): him 0%, members 100%" in out
    assert "Discord channels: 0 of your lines, 0 of theirs" in out and "not measured" in out
    assert main(["turing", "run", "--json", "--days", "7", "--data-dir", str(root)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["days"] == 7 and data["families"]["private"]["status"] == "ok"
    assert store.conn.execute("SELECT count(*) FROM meta").fetchone()[0] == before
    assert not tstore.available(store.conn) or not store.conn.execute(
        "SELECT count(*) FROM turing_night").fetchone()[0]


def test_report_reads_stored_nights(store, capsys):
    import time
    now = time.time()
    night = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    result = {"days": 14, "families": {
        "private": {"status": "ok", "human_lines": 700, "member_lines": 1100, "sample": 700,
                    "groups": 160, "auc": 0.95, "low": 0.94, "high": 0.96,
                    "features": [{"key": "apostrophe", "human": 0.06, "member": 0.5,
                                  "separation": 0.2, "higher": "members"}]},
        "game": {"status": "too_few", "human_lines": 3, "member_lines": 80, "sample": 0, "groups": 0,
                 "auc": None, "low": None, "high": None, "features": []}}}
    with store.transaction():
        tstore.put_stylo(store.conn, night, result, now=now)
        tstore.put_judgement(store.conn, night, "w1", "game", ["c:1"], {
            "status": "ok", "speakers": 3, "correct": 2, "his": 1, "his_correct": 1,
            "tells": [{"code": "polish", "judged": "bot", "truth": "bot", "phrase": "too tidy"},
                      {"code": "polish", "judged": "bot", "truth": "bot", "phrase": None},
                      {"code": "natural", "judged": "human", "truth": "human", "phrase": "terse"}]},
            now=now)
    assert main(["turing", "report", "--data-dir", str(data_dir(store))]) == 0
    out = capsys.readouterr().out
    assert f"{night}  AUC 0.95 (approx. 0.94-0.96)" in out
    assert "contractions with an apostrophe (i'm, don't): him 6%, members 50%" in out
    assert "not measured (too_few: 3 of your lines, 80 of theirs)" in out
    assert ("Blind judge: 75% balanced accuracy in 1 windows: members called AI 50% of 2 times, "
            "you called human 100% of 1 times (raw 67%, which an always-AI judge would score 67%).") in out
    assert 'writing too polished or literary (2) "too tidy"' in out
    assert main(["turing", "report", "--json", "--data-dir", str(data_dir(store))]) == 0
    data = json.loads(capsys.readouterr().out)
    game = next(f for f in data["families"] if f["family"] == "game")
    assert game["judge"]["accuracy"] == 2 / 3 and game["judge"]["balanced"] == 0.75
    assert game["points"][0]["judge"] == 0.75 and game["tells"][0]["code"] == "polish"


def test_an_older_database_reads_not_set_up_and_is_left_alone(tmp_path, capsys):
    db = tmp_path / "mind.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    before = db.read_bytes()
    assert main(["turing", "report", "--data-dir", str(tmp_path)]) == 0
    assert "Not set up yet" in capsys.readouterr().out
    assert main(["turing", "run", "--data-dir", str(tmp_path)]) == 1  # no human_guid stored
    out = capsys.readouterr()
    assert out.out == "" and out.err == "turing run failed: ValueError\n"
    assert db.read_bytes() == before


def test_a_missing_data_dir_is_refused(tmp_path, capsys):
    assert main(["turing", "report", "--data-dir", str(tmp_path / "nothing")]) == 2
    assert "no mind.db" in capsys.readouterr().err
