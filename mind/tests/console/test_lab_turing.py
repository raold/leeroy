"""The Lab's Turing panel: read only, numbers and fixed labels, against invented rows."""
import sqlite3
import time
from pathlib import Path

from leeroy_mind.console import labview
from leeroy_mind.turing import store as tstore


def data_path(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()[2]).parent


def stored(store, night, auc, *, now):
    result = {"days": 14, "families": {"private": {
        "status": "ok", "human_lines": 120, "member_lines": 300, "sample": 120, "groups": 30,
        "auc": auc, "low": auc - .03, "high": auc + .02,
        "features": [{"key": "burst", "human": .09, "member": .67, "separation": .29, "higher": "members"}]}}}
    with store.transaction():
        tstore.put_stylo(store.conn, night, result, now=now)
        tstore.put_judgement(store.conn, night, "w1", "private", ["c:1"], {
            "status": "ok", "speakers": 2, "correct": 1, "his": 1, "his_correct": 0,
            "tells": [{"code": "timing", "judged": "bot", "truth": "bot", "phrase": None}]}, now=now)


def test_the_panel_reads_the_trend_and_the_tells(store):
    now = time.time()
    day = lambda n: time.strftime("%Y-%m-%d", time.localtime(now - n * 86400))
    stored(store, day(2), 0.95, now=now)
    stored(store, day(1), 0.9, now=now)
    stored(store, day(30), 0.99, now=now)  # outside the 14 days
    view = labview.read(data_path(store), now=now)["turing"]
    assert view["available"] and view["days"] == 14
    fam = view["families"][0]
    assert fam["family"] == "private" and fam["label"] == "Whispers and DMs"
    assert [p["night"] for p in fam["points"]] == [day(2), day(1)]
    assert [p["auc"] for p in fam["points"]] == [0.95, 0.9]
    assert fam["latest"]["features"][0]["words"] == "lines inside a burst of two or more: him 9%, members 67%"
    assert fam["judge"]["accuracy"] == 0.5 and fam["judge"]["his_accuracy"] == 0.0
    assert fam["tells"] == [{"code": "timing", "label": "reply speed and timing", "count": 2, "phrases": []}]


def test_absent_and_older_databases_read_not_set_up_without_writing(tmp_path):
    assert labview.read(tmp_path, now=1000)["turing"] == {"available": False, "days": 14, "families": []}
    assert not (tmp_path / "mind.db").exists()
    conn = sqlite3.connect(tmp_path / "mind.db")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    before = (tmp_path / "mind.db").read_bytes()
    assert labview.read(tmp_path, now=1000)["turing"]["available"] is False
    assert (tmp_path / "mind.db").read_bytes() == before


def test_a_migrated_database_with_no_nights_is_available_and_empty(store):
    view = labview.read(data_path(store), now=time.time())["turing"]
    assert view == {"available": True, "days": 14, "families": []}
