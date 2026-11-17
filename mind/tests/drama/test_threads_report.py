"""The romantic-threads recorder's read side: the report, the CLI (`drama threads report` and
`replay`), health's note, the Social tab's view and the migration. Invented members only."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from drama.test_pulls import (NIGHT, OWN, T, cast, raid, record, scene, seed_shadow, shadow, sql)
from leeroy_mind import health
from leeroy_mind.__main__ import main
from leeroy_mind.console import socialview
from leeroy_mind.drama import pulls, report
from leeroy_mind.drama.cli import drama_main
from leeroy_mind.storage import db as db_mod
from test_migrations import migration_number

DAY = 86400.0


def data_dir(h) -> Path:
    return Path(h.store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def night_run(h, night, start, *, status="done"):
    sql(h, "INSERT INTO night_run(night, window_start, window_end, status, started) "
           "VALUES (?,?,?,?,?)", (night, start, start + DAY, status, start + DAY))


def recorded(h, monkeypatch):
    """Two recorded nights: raids, a warm scene, one would-be thread and one held back."""
    monkeypatch.setattr(pulls, "OPEN_P", 1.0)
    cast(h, taken=(104,))
    raid(h, 1, (101, 102, 103, 104))
    scene(h, 101, 102, [(101, 102, 2)])
    seed_shadow(h, 105, 104, 60.0)
    seed_shadow(h, 103, 104, 50.0)
    h.rel(105, 104, affinity=30.0)
    h.rel(103, 104, affinity=5.0)
    record(h)
    raid(h, 2, (101, 102), t=T + DAY)
    record(h, "2026-10-30")


# -- the report ----------------------------------------------------------------------------------

def test_the_report_counts_events_pulls_and_would_be_threads(h, monkeypatch):
    recorded(h, monkeypatch)
    data = report.threads_report(h.store.conn, nights=14)
    assert data["set_up"] is True and data["in_effect"] is False
    assert [n["night"] for n in data["nights"]] == [NIGHT, "2026-10-30"]
    assert data["nights"][0]["kinds"] == {"raided": 8, "warm_scene": 2}
    assert data["nights"][1]["kinds"] == {"raided": 2}
    assert data["kinds"] == {"raided": 10, "warm_scene": 2} and data["events"] == 12
    assert data["scopes"] == {"pair": 2, "public": 10}
    assert data["skipped"] == {"same_sex": 4}
    assert [(t["a"], t["b"], t["state"], t["shape"], t["taken"]) for t in data["open"]] == [
        (105, 104, "open", "triangle", True)]
    assert [(t["a"], t["b"], t["why"]) for t in data["held"]] == [(103, 104, "affinity")]
    assert data["waiting"] == {"affinity": 2}                  # one candidate, two nights
    assert data["top"][0]["a"] == 105 and data["top"][0]["pull"] == pytest.approx(60 * pulls.DECAY ** 2, abs=0.05)
    assert data["rules"]["threads_max"] == 3 and data["rules"]["taken_max"] == 1
    assert report.threads_report(h.store.conn, nights=1)["nights"][0]["night"] == "2026-10-30"


def test_the_report_text_is_labelled_and_holds_no_name(h, monkeypatch):
    recorded(h, monkeypatch)
    text = report.render(report.threads_report(h.store.conn))
    assert text.startswith("Romantic threads: recording only. Nothing below is in effect")
    for part in ("Events by kind, per night (last 2):", "raided", "warm_scene", "105 -> 104",
                 "Would be open now:", "triangle", "affinity", "left out: same_sex 4",
                 "caps 3 live, 1 with a taken member"):
        assert part in text, part
    for name in ("Arkon", "Belra", "Coros", "Dunna", "Ebbo", "Testmage", "Quillon"):
        assert name not in text


def test_an_older_database_and_an_unrecorded_one_say_so(h):
    old = sqlite3.connect(":memory:")
    data = report.threads_report(old)
    assert data["set_up"] is False and data["nights"] == [] and data["top"] == []
    assert "Not set up yet" in report.render(data)
    assert "No night recorded yet" in report.render(report.threads_report(h.store.conn))


# -- the CLI ---------------------------------------------------------------------------------------

def test_the_report_command_reads_only(h, monkeypatch, capsys):
    recorded(h, monkeypatch)
    root = data_dir(h)
    before = hashes(h)
    assert main(["drama", "threads", "report", "--data-dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert "recording only" in out and "105 -> 104" in out
    assert drama_main(["threads", "report", "--nights", "1", "--json", "--data-dir", str(root)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [n["night"] for n in data["nights"]] == ["2026-10-30"] and data["in_effect"] is False
    assert hashes(h) == before
    assert drama_main(["threads", "report", "--data-dir", str(root / "nowhere")]) == 2
    with pytest.raises(SystemExit):
        drama_main(["threads", "report", "--nights", "0", "--data-dir", str(root)])


def hashes(h):
    out = {}
    for (name,) in h.store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        digest = hashlib.sha256()
        for row in sorted(repr(tuple(r)) for r in h.store.conn.execute(f'SELECT * FROM "{name}"')):
            digest.update(row.encode())
        out[name] = digest.hexdigest()
    h.store.conn.commit()
    return out


def stored_nights(h):
    cast(h)
    start = T - 15 * 3600
    raid(h, 1, (101, 102, 103, 104), t=T)
    scene(h, 101, 102, [(101, 102, 2)])
    raid(h, 2, (101, 102), t=T + DAY)
    raid(h, 3, (103, 104), t=T + 2 * DAY)
    night_run(h, "2026-10-28", start - DAY)
    night_run(h, NIGHT, start)
    night_run(h, "2026-10-30", start + DAY)
    night_run(h, "2026-10-31", start + 2 * DAY, status="failed")
    return start


def test_replay_runs_the_stored_nights_on_a_copy_and_never_writes_the_live_database(h, capsys):
    stored_nights(h)
    seed_shadow(h, 105, 104, 12.0)       # a live row the replay must neither read nor change
    root = data_dir(h)
    before = hashes(h)
    assert drama_main(["threads", "replay", "--from", NIGHT, "--json", "--data-dir", str(root)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["replayed"] == [NIGHT, "2026-10-30"] and data["copy"] is None
    assert [n["kinds"] for n in data["nights"]] == [{"raided": 8, "warm_scene": 2}, {"raided": 2}]
    assert all(t["a"] != 105 for t in data["top"])    # the copy's shadow started empty
    assert "today's cast" in data["limits"]
    assert hashes(h) == before
    assert [h.count(t) for t in OWN] == [0, 1, 0] and shadow(h, 105, 104)["pull"] == 12.0
    assert list((root / "scratch").glob("*.db*")) == []


def test_replay_prints_the_report_and_can_keep_its_copy(h, capsys):
    stored_nights(h)
    root = data_dir(h)
    assert main(["drama", "threads", "replay", "--from", "2026-10-01", "--keep",
                 "--data-dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Replayed 3 stored nights from 2026-10-01 on a scratch copy")
    assert "The live database was not written." in out and "Events by kind, per night (last 3):" in out
    [copy] = (root / "scratch").glob("mind-threads-replay-*.db")
    conn = sqlite3.connect(copy)
    try:
        assert conn.execute("SELECT count(*) FROM pull_night").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM pull_event").fetchone()[0] == 12
    finally:
        conn.close()
    assert h.count("pull_event") == 0
    with pytest.raises(SystemExit):
        drama_main(["threads", "replay", "--from", "yesterday", "--data-dir", str(root)])


# -- health ---------------------------------------------------------------------------------------------

def check(h, text):
    root = data_dir(h)
    (root / "mind.toml").write_bytes(text)
    conn = sqlite3.connect(f"{(root / 'mind.db').as_uri()}?mode=ro", uri=True)
    try:
        return health._drama_threads(conn, root)
    finally:
        conn.close()


RECORD = b'[drama]\nthreads = "record"\n'


def test_health_is_quiet_while_off_and_while_recording_finds_events(h):
    off = check(h, b"")
    assert off.status == health.OK and off.text.startswith("romantic threads: off")
    cast(h)
    young = check(h, RECORD)
    assert young.status == health.OK and "0 nights so far" in young.text
    for i, night in enumerate(("2026-10-29", "2026-10-30", "2026-10-31")):
        raid(h, i + 1, (101, 102), t=T + i * DAY)
        record(h, night)
    ok = check(h, RECORD)
    assert ok.status == health.OK and ok.text == (
        "romantic threads: recording only; newest night 2026-10-31, 2 events")


def test_health_notes_three_nights_without_an_event(h):
    cast(h)
    raid(h, 1, (101, 102))
    for night in ("2026-10-29", "2026-10-30", "2026-10-31"):
        record(h, night)
    assert check(h, RECORD).status == health.OK      # the first of the three held events
    record(h, "2026-11-01")
    note = check(h, RECORD)
    assert note.status == health.NOTE and note.text == (
        "romantic threads: recording is on but the last 3 nights recorded no events")
    assert check(h, b"").status == health.OK         # off: no note


def test_health_notes_finished_nights_the_recorder_missed(h):
    cast(h)
    for i in range(3):
        night_run(h, f"2026-10-2{i + 7}", T + i * DAY)
    note = check(h, RECORD)
    assert note.status == health.NOTE and "last 3 finished nights were not recorded" in note.text


def test_health_reads_an_older_database_as_not_set_up(h, tmp_path):
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "mind.toml").write_bytes(RECORD)
    conn = sqlite3.connect(":memory:")
    got = health._drama_threads(conn, tmp_path / "old")
    assert got.status == health.NOTE and "not set up" in got.text


# -- the Social tab -----------------------------------------------------------------------------------------

def test_the_social_view_carries_the_recorders_numbers_as_not_in_effect(h, monkeypatch):
    recorded(h, monkeypatch)
    view = socialview.social_view(h.store.conn, now=T)
    threads = view["threads"]
    assert view["missing"] == [] and threads["set_up"] is True and threads["in_effect"] is False
    assert threads["events"] == 12 and len(threads["nights"]) == 2
    assert [(t["a"], t["b"]) for t in threads["open"]] == [(105, 104)]
    assert view["romances"] == []      # a would-be thread is no romance and no relationship row
    json.dumps(view)


def test_the_social_view_names_the_tables_an_older_database_lacks(h):
    sql(h, "DROP TABLE pull_night")
    view = socialview.social_view(h.store.conn, now=T)
    assert "pull_night" in view["missing"] and view["threads"]["set_up"] is False


def test_the_page_has_the_panel_and_says_it_is_recording():
    root = Path(socialview.__file__).parent
    page = (root / "page.html").read_text(encoding="utf-8")
    assert '<h3>Threads (recording)</h3></header><div id="social-threads">' in page
    script = (root / "static" / "js" / "social.mjs").read_text(encoding="utf-8")
    assert 'section("social-threads"' in script and "THREADS_NOTE" in script
    model = (root / "static" / "js" / "socialmodel.mjs").read_text(encoding="utf-8")
    assert "Recording only, not in effect" in model


# -- the migration ----------------------------------------------------------------------------------------------

def test_the_migration_adds_the_three_tables(h):
    conn = h.store.conn
    number = migration_number("pull_threads")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db_mod.latest_version() >= number
    cols = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in OWN}
    assert cols["pull_event"] == ["id", "night", "a", "b", "kind", "weight", "factor", "amount",
                                  "scope", "ref", "ts"]
    assert cols["pull_shadow"] == ["a", "b", "pull", "public_pull", "events", "first_night",
                                   "last_event_night", "updated_night", "thread_state",
                                   "thread_night", "thread_why", "shape", "ceiling", "taken"]
    assert cols["pull_night"] == ["night", "ts", "summary"]
    # nothing else changed: the tables the recorder must never write keep their columns
    assert "pull" not in {r[1] for r in conn.execute("PRAGMA table_info(relationship)")}


@pytest.mark.parametrize("statement", [
    "INSERT INTO pull_event(night,a,b,kind,weight,factor,amount,scope,ref,ts) VALUES ('n',1,1,'raided',1,1,1,'public','raid:1',0)",
    "INSERT INTO pull_event(night,a,b,kind,weight,factor,amount,scope,ref,ts) VALUES ('n',1,2,'raided',1,1,1,'guild','raid:1',0)",
    "INSERT INTO pull_event(night,a,b,kind,weight,factor,amount,scope,ref,ts) VALUES ('n',1,2,'raided',1,1.5,1,'public','raid:1',0)",
    "INSERT INTO pull_shadow(a,b,pull,first_night,last_event_night,updated_night) VALUES (1,2,101,'n','n','n')",
    "INSERT INTO pull_shadow(a,b,pull,first_night,last_event_night,updated_night,thread_state) VALUES (1,2,5,'n','n','n','involved')",
    "INSERT INTO pull_night(night,ts,summary) VALUES ('n',0,'[]')",
])
def test_the_tables_refuse_rows_outside_their_rules(h, statement):
    with pytest.raises(sqlite3.IntegrityError):
        sql(h, statement)


def test_one_ledger_row_per_fact(h):
    row = ("INSERT INTO pull_event(night,a,b,kind,weight,factor,amount,scope,ref,ts) "
           "VALUES ('n',1,2,'raided',1,1,1,'public','raid:1',0)")
    sql(h, row)
    with pytest.raises(sqlite3.IntegrityError):
        sql(h, row)
