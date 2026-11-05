"""Migration 0016: the direct ledger, rule origins and the rebuilt line, mail and calendar."""
import sqlite3

import pytest

from leeroy_mind.storage import db

NEW = ("realm_direct", "realm_rule_origin", "realm_benchmark_seen")
REBUILT = ("realm_line", "realm_mail", "realm_calendar")
HEX = "0123456789abcdef" * 2


def _insert(conn, table, values):
    conn.execute(f"INSERT INTO {table}({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                 tuple(values.values()))


def _v15(tmp_path, monkeypatch):
    files = db._migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n <= 15])
        conn = db.open_db(tmp_path / "v15.db")
    with conn:
        _insert(conn, "realm_line", dict(id=3, ts=10.0, guid=201, guild=60, channel="trade", text="lf healer", dropped=None))
        _insert(conn, "realm_line", dict(id=7, ts=11.0, guid=201, guild=60, channel="trade", text="gz", dropped="flirt"))
        for id, state in ((2, "queued"), (4, "sent"), (5, "taken"), (9, "failed")):
            _insert(conn, "realm_mail", dict(id=id, ts=12.0, sender=101, kind="gift", subject="for you",
                                             body="", items='[{"item_id":1}]', item_guid=id * 10, state=state,
                                             taken=13.0 if state == "taken" else None))
        _insert(conn, "realm_calendar", dict(id=6, kind="raid", creator=101, at=14.0, title="ICC 25",
                                             ref="raid:1", updated=15.0))
        _insert(conn, "realm_fact", dict(id=1, kind="kill", text="A fact.", source="rule", public=1, ts=16.0))
        _insert(conn, "realm_knows", dict(fact=1, guild=60, since=16.0, how="own"))
    return conn, files


def _schema(conn, *, rebuilt):
    rows = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    return [tuple(r) for r in rows if (r[2] in REBUILT) == rebuilt and r[2] not in NEW]


def _rows(conn, table, columns="*"):
    return [tuple(r) for r in conn.execute(f"SELECT {columns} FROM {table} ORDER BY 1")]


def test_migration_0016_applies_and_keeps_rows(tmp_path, monkeypatch):
    conn, files = _v15(tmp_path, monkeypatch)
    try:
        others = _schema(conn, rebuilt=False)
        before = {table: _rows(conn, table) for table in REBUILT}
        with monkeypatch.context() as patch:  # 0016 itself; later migrations have tests of their own
            patch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n <= 16])
            assert db.latest_version() == 16
            assert db.migrate(conn) == 16
            assert db.migrate(conn) == 16
        assert _schema(conn, rebuilt=False) == others
        # Every old column of every old row, unchanged; the new ones filled as the copy says.
        assert _rows(conn, "realm_line", "id,ts,guid,guild,channel,text,dropped") == before["realm_line"]
        assert _rows(conn, "realm_line", "id,command_id,kind,state,reason") == [
            (3, None, None, "unknown", None), (7, None, None, "dropped", None)]
        mail = "id,ts,sender,kind,subject,body,items,item_guid,state,taken"
        assert [row[:8] + row[9:] for row in _rows(conn, "realm_mail", mail)] == [
            row[:8] + row[9:] for row in before["realm_mail"]]
        assert _rows(conn, "realm_mail", "id,state,command_id,reason") == [
            (2, "ready", None, None), (4, "sent", None, None), (5, "taken", None, None), (9, "failed", None, None)]
        assert _rows(conn, "realm_calendar", "id,kind,creator,at,title,ref,updated") == before["realm_calendar"]
        assert _rows(conn, "realm_calendar", "id,event_id,command_id,state") == [(6, None, None, "posted")]
        assert _rows(conn, "realm_fact") == [(1, "kill", "A fact.", "rule", "", 1, 16.0)]
        assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= set(NEW)
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE '%_v15'").fetchall()
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            assert all(key[2] not in {f"{t}_v15" for t in REBUILT}
                       for key in conn.execute(f'PRAGMA foreign_key_list("{table}")'))
        with conn:  # New rows continue after the kept ids, in the new shapes.
            _insert(conn, "realm_line", dict(ts=20.0, guid=201, guild=60, channel="trade", text="new",
                                             command_id=HEX, kind="rival", state="dispatched"))
            _insert(conn, "realm_mail", dict(ts=20.0, sender=101, kind="letter", subject="", body=""))
            _insert(conn, "realm_calendar", dict(kind="raid", creator=101, at=21.0, title="Naxx", updated=21.0,
                                                 event_id=77, command_id=HEX, state="posted"))
        assert conn.execute("SELECT max(id) FROM realm_line").fetchone()[0] == 8
        assert conn.execute("SELECT state FROM realm_mail WHERE id=10").fetchone()[0] == "awaiting_text"
    finally:
        conn.close()


@pytest.mark.parametrize("table,values", [
    ("realm_line", dict(ts=1.0, guid=1, guild=1, channel="trade", text="x", state="sent")),  # no kind
    ("realm_line", dict(ts=1.0, guid=1, guild=1, channel="trade", text="x", kind="rival", state="sent")),
    ("realm_line", dict(ts=1.0, guid=1, guild=1, channel="trade", text="x", kind="rival", state="dropped")),
    ("realm_line", dict(ts=1.0, guid=1, guild=1, channel="trade", text="x", kind="rival", state="dropped",
                        reason="flirt", dropped="flirt")),
    ("realm_line", dict(ts=1.0, guid=1, guild=1, channel="trade", text="x", kind="boast", state="dropped",
                        reason="flirt")),
    ("realm_line", dict(ts=1.0, guid=1, guild=1, channel="trade", text="x", kind="rival", state="refused")),
    ("realm_line", dict(ts=1.0, guid=1, guild=1, channel="trade", text="x", kind="rival", state="sent",
                        command_id="m1")),
    ("realm_mail", dict(ts=1.0, sender=1, kind="gift", subject="", body="", state="queued")),
    ("realm_mail", dict(ts=1.0, sender=1, kind="gift", subject="", body="", state="dispatched")),
    ("realm_mail", dict(ts=1.0, sender=1, kind="gift", subject="", body="", state="ready", command_id=HEX)),
    ("realm_mail", dict(ts=1.0, sender=1, kind="gift", subject="", body="", state="skipped")),
    ("realm_mail", dict(ts=1.0, sender=1, kind="gift", subject="", body="", state="skipped", reason="No Rights")),
    ("realm_calendar", dict(kind="raid", creator=1, at=1.0, title="x", updated=1.0, state="pending", event_id=5)),
    ("realm_calendar", dict(kind="raid", creator=1, at=1.0, title="x", updated=1.0, state="posted", event_id=0)),
    ("realm_calendar", dict(kind="raid", creator=1, at=1.0, title="x", updated=1.0, state="sent")),
    ("realm_benchmark_seen", dict(benchmark_id=0, guild=1, ts=1.0)),
    ("realm_benchmark_seen", dict(benchmark_id=1, guild=0, ts=1.0)),
])
def test_rebuilt_and_new_tables_enforce_their_shapes(store, table, values):
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        _insert(store.conn, table, values)


def _direct(**overrides):
    values = dict(command_id=HEX, owner="speech", ref="line:1", kind="realm_say", payload_sha256="a" * 64,
                  guild=50, human=10, process_id="p", expects_job=0, state="prepared", prepared_ms=1000)
    values.update(overrides)
    return values


def test_realm_direct_holds_one_live_intent_per_ref(store):
    conn = store.conn
    with conn:
        _insert(conn, "realm_direct", _direct())
        _insert(conn, "realm_direct", _direct(command_id="1" * 32, state="refused", reason="busy", settled_ms=1001))
        _insert(conn, "realm_direct", _direct(command_id="2" * 32, ref="line:2", state="unknown",
                                              reason="transport_unknown", dispatched_ms=1000, settled_ms=1002))
    for state in ("prepared", "dispatched", "acked", "unknown"):
        extra = dict(prepared=dict(), dispatched=dict(dispatched_ms=1000),
                     acked=dict(dispatched_ms=1000, settled_ms=1001),
                     unknown=dict(dispatched_ms=1000, settled_ms=1001, reason="transport_unknown"))[state]
        for ref in ("line:1", "line:2"):
            with pytest.raises(sqlite3.IntegrityError), conn:
                _insert(conn, "realm_direct", _direct(command_id="3" * 32, ref=ref, state=state, **extra))
    with conn:  # A refused or abandoned intent holds no ref.
        _insert(conn, "realm_direct", _direct(command_id="4" * 32, ref="line:3", state="abandoned",
                                              reason="unsent", settled_ms=1000))
        _insert(conn, "realm_direct", _direct(command_id="5" * 32, ref="line:3"))


@pytest.mark.parametrize("overrides", [
    dict(command_id="m1"), dict(command_id="A" * 32), dict(owner="night"), dict(ref="line 1"), dict(ref=""),
    dict(kind="RealmSay"), dict(payload_sha256="x"), dict(guild=0), dict(human=1 << 32), dict(expects_job=2),
    dict(state="sent"), dict(reason="x"), dict(value="1"), dict(dispatched_ms=1000),
    dict(state="dispatched"), dict(state="dispatched", dispatched_ms=999),
    dict(state="dispatched", dispatched_ms=1000, value="5", expects_job=1),
    dict(state="acked", dispatched_ms=1000),
    dict(state="acked", dispatched_ms=1000, settled_ms=1001, expects_job=1),
    dict(state="acked", dispatched_ms=1000, expects_job=1, value="7"),
    dict(state="acked", dispatched_ms=1000, settled_ms=1001, expects_job=1, value="07"),
    dict(state="acked", dispatched_ms=1000, settled_ms=1001, expects_job=1, value="18446744073709551616"),
    dict(state="acked", dispatched_ms=1000, settled_ms=999, reason=None),
    dict(state="refused", settled_ms=1001), dict(state="refused", reason="busy"),
    dict(state="unknown", reason="lost", settled_ms=1001),
    dict(state="abandoned", reason="unsent", dispatched_ms=1000, settled_ms=1001),
])
def test_realm_direct_states_keep_their_shapes(store, overrides):
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        _insert(store.conn, "realm_direct", _direct(**overrides))


def test_realm_direct_accepts_every_settled_shape(store):
    shapes = [dict(state="dispatched", dispatched_ms=1000),
              dict(state="acked", dispatched_ms=1000, settled_ms=1000),
              dict(state="acked", dispatched_ms=1000, expects_job=1),
              dict(state="acked", dispatched_ms=1000, settled_ms=1001, expects_job=1, value="18446744073709551615"),
              dict(state="refused", reason="pretransport_refused", dispatched_ms=1000, settled_ms=1001),
              dict(state="unknown", reason="recovered_dispatched", dispatched_ms=1000, settled_ms=1001),
              dict(state="abandoned", reason="recovered_prepared", settled_ms=1000)]
    with store.conn:
        for n, shape in enumerate(shapes):
            _insert(store.conn, "realm_direct", _direct(command_id=f"{n:032x}", ref=f"line:{n}", **shape))


@pytest.mark.parametrize("overrides", [
    dict(producer_key="rule:night_beat_v1:k"), dict(producer_key="event:race_rule_kill_v1:k"),
    dict(producer_key="rule:race_rule_kill_v1:"), dict(producer_key="rule:race_rule_kill_v1:a b"),
    dict(policy="verified_raid_kill_v1", producer_key="rule:verified_raid_kill_v1:k"),
    dict(inputs_json='{"b":1, "a":2}'), dict(inputs_json="[]"), dict(known_json="[]"),
    dict(known_json='{"a":1}'), dict(text_sha256="x"), dict(revision=0), dict(state="retired"), dict(fact=99),
])
def test_rule_origin_schema(store, overrides):
    with store.conn:
        _insert(store.conn, "realm_fact", dict(id=1, kind="kill", text="A fact.", source="rule", ts=1.0))
    values = dict(fact=1, producer_key="rule:race_rule_kill_v1:k", policy="race_rule_kill_v1", inputs_json="{}",
                  known_json='[[50,"own"]]', text_sha256="a" * 64, revision=1, state="active")
    with pytest.raises(sqlite3.IntegrityError), store.conn:
        _insert(store.conn, "realm_rule_origin", {**values, **overrides})
    with store.conn:
        _insert(store.conn, "realm_rule_origin", values)


def test_failed_0016_rolls_back_the_rebuild(tmp_path, monkeypatch):
    conn, files = _v15(tmp_path, monkeypatch)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        schema = [tuple(r) for r in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY 1,2")]
        rows = {table: _rows(conn, table) for table in REBUILT}
        migration = next(path for number, path in files if number == 16)
        broken = tmp_path / migration.name
        broken.write_text(migration.read_text(encoding="utf-8") + "\nCREATE TABLE realm_source(x);",
                          encoding="utf-8")
        monkeypatch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n < 16] + [(16, broken)])
        with pytest.raises(sqlite3.OperationalError):
            db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version == 15
        assert [tuple(r) for r in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY 1,2")] == schema
        assert {table: _rows(conn, table) for table in REBUILT} == rows
    finally:
        conn.close()
