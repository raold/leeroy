"""The validated journal is decoded again only when its stored rows changed, and is never stale.

A cached read must equal a fresh decode of the same connection after any write: the store's own
reducers, raw SQL, a rollback or savepoint rollback after a read inside the transaction, and
another connection's commit. A held body never reads as free; a bad row still fails closed.
"""
import json
from pathlib import Path
import sqlite3
import time

import pytest

from leeroy_mind.guild.protocol import BotDo, Job
from leeroy_mind.protocol import Ack
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_policy import classify, document
from leeroy_mind.realm.action_store import ActionSpec, ActionStore, ActionUnavailable

from realm.journal import MIGRATION, journal_conn, realistic_journal

TOKEN = "a" * 32 + "-" + "b" * 16
DAYS = Path(__file__).parents[2] / "leeroy_mind/storage/migrations/0064_day_plans.sql"


def fresh(store):
    """What an uncached reader of the same connection sees right now."""
    other = ActionStore(store.conn)
    try:
        holds = other.holds()
    except ActionUnavailable:
        holds = ActionUnavailable
    return other._records(), holds


def same(store):
    records, holds = fresh(store)
    assert store.records() == records
    if holds is ActionUnavailable:
        with pytest.raises(ActionUnavailable):
            store.holds()
    else:
        assert store.holds() == holds
    return holds


def prepare(store, cmd, now, owner="realm_day", rank=4):
    c = classify(cmd)
    return store.prepare(ActionSpec(cmd.id, None, "root", c.policy, owner, rank, c.kind,
                                    c.payload, c.reserved_manifest, 50, 1), now=now)


def dispatch(store, record, now, started_ms=0):
    return store.begin_dispatch(record.command_id, payload=record.payload, process_id="process",
                                connection_epoch=7, now=now, started_ms=started_ms)


def held(store):
    return {g for h in store.holds() for g in h.guids}


@pytest.fixture
def store():
    conn = journal_conn()
    yield ActionStore(conn)
    conn.close()


def test_every_write_path_reads_as_a_fresh_decode(store):
    steps = []

    def step(name, write):
        store.holds(); store.records()  # warm: the write must invalidate what was read
        write()
        steps.append((name, same(store)))

    move = p.RealmMove("move", 2, "home")
    step("prepare", lambda: prepare(store, move, 1))
    step("dispatch", lambda: dispatch(store, store.get("move"), 2))
    step("ack", lambda: store.observe_ack(Ack(0, "move", True, ""), now=3))
    step("job", lambda: store.observe_realm_job(p.RealmJob(0, "move", "realm_move", "done", "", 2, 0), now=4))
    unknown = BotDo("unknown", (3, 4), "nc", "-bg,-lfg")
    step("prepare unknown", lambda: dispatch(store, prepare(store, unknown, 5), 6, started_ms=6000))
    step("send error", lambda: store.observe_send_error("unknown", "Timeout", now=7))
    step("abandon", lambda: store.abandon("unknown", reason="reviewed", now=8))
    restart = BotDo("restart", (5, 6), "nc", "-bg,-lfg")
    step("prepare restart", lambda: dispatch(store, prepare(store, restart, 9), 10, started_ms=10000))
    step("restart ack", lambda: store.observe_ack(Ack(0, "restart", True, ""), now=10.5))
    step("restart release", lambda: store.restart_release(started_ms=20000, guild=50, human=1, now=11))
    step("predispatch", lambda: store.predispatch_failed(prepare(store, p.RealmMove("lost", 7, "home"), 12).command_id,
                                                         reason="NotConnected", now=13))
    bot = BotDo("defaults", (8, 9), "nc", "-bg,-lfg")
    step("prepare defaults", lambda: dispatch(store, prepare(store, bot, 14), 15))
    step("bot job", lambda: store.observe_bot_job(Job(0, "defaults", "bot_do", "done", "", "", (8, 9), (),
                                                      0, 0, 0, 0, 0, 0), now=16))
    step("defaults ack", lambda: store.observe_ack(Ack(0, "defaults", True, ""), now=17))
    wg = p.WgJoin("wg", (4, 3, 2))
    step("wg", lambda: dispatch(store, prepare(store, wg, 18), 19))
    step("wg ack", lambda: store.observe_ack(Ack(0, "wg", True, ""), now=20))
    step("wg job", lambda: store.observe_realm_job(p.RealmJob(0, "wg", "wg_join", "done", "", 0, 2, TOKEN), now=21))
    step("query", lambda: dispatch(store, store.prepare_query("wg", p.GetRealmPvp("query", origin_id="wg"), now=40), 40))
    step("status", lambda: store.observe_pvp(p.RealmPvp(0, "query", TOKEN, "wg", "wintergrasp", 2**64 - 1, 1, 50, 1,
                                                        (2, 3), "active", False, "", ""), now=41))
    step("query ack", lambda: store.observe_ack(Ack(0, "query", True, ""), now=42))
    step("cleanup", lambda: dispatch(store, store.prepare_cleanup("wg", p.LeavePvp("cleanup", (2, 3), TOKEN), now=43), 43))
    inflight = p.RealmMove("inflight", 10, "home")
    step("prepare inflight", lambda: dispatch(store, prepare(store, inflight, 44), 45))
    step("recover", lambda: store.recover(process_id="restarted", now=46))
    step("prune", lambda: store.prune(keep=0, now=10_000))
    # The script really moved holds both ways: a reused stale read would have failed above.
    shapes = [sorted(g for h in holds for g in h.guids) for _, holds in steps]
    assert [2] in shapes and [] in shapes and [3, 4] in shapes and [5, 6] in shapes
    assert held(store) == {2, 3, 4, 10}  # the WG root and the move recovery left unknown


def test_a_rolled_back_delete_read_inside_its_transaction_never_frees_the_body():
    store, before = realistic_journal()
    assert held(store) == before and before
    holder = store.holds()[0].scope_id
    store.conn.execute("DELETE FROM realm_action WHERE command_id=?", (holder,))
    assert held(store) < before  # read inside the open transaction: the row is gone there
    store.conn.rollback()
    assert held(store) == before
    same(store)


def test_a_savepoint_rollback_restores_the_hold():
    store, before = realistic_journal()
    holder = store.holds()[0].scope_id
    store.conn.execute("SAVEPOINT inner")
    store.conn.execute("DELETE FROM realm_action WHERE command_id=?", (holder,))
    assert held(store) < before
    store.conn.execute("ROLLBACK TO inner")
    store.conn.execute("RELEASE inner")
    assert held(store) == before
    same(store)


def test_a_store_write_that_rolls_back_leaves_no_stale_read(store, monkeypatch):
    dispatch(store, prepare(store, BotDo("held", (2, 3), "nc", "-bg,-lfg"), 1), 2)
    store.observe_send_error("held", "Timeout", now=3)
    assert held(store) == {2, 3}
    real = store._save

    def save_then_fail(r):
        real(r)
        store.holds()  # a read between the write and its rollback
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "_save", save_then_fail)
    with pytest.raises(RuntimeError):
        store.abandon("held", reason="reviewed", now=4)
    monkeypatch.undo()
    assert store.get("held").state == "unknown"
    assert held(store) == {2, 3}
    same(store)


def test_raw_sql_through_the_same_connection_is_seen(store):
    dispatch(store, prepare(store, p.RealmMove("move", 2, "home"), 1), 2)
    assert held(store) == {2}
    store.conn.execute("UPDATE realm_action SET reserved_manifest='[3]', payload=? WHERE command_id='move'",
                       (classify(p.RealmMove("move", 3, "home")).payload,))
    assert held(store) == {3}  # uncommitted, same connection
    store.conn.commit()
    assert held(store) == {3}
    same(store)


def test_another_connections_commit_is_seen_once_its_snapshot_is_visible(tmp_path):
    path = tmp_path / "mind.db"
    a = sqlite3.connect(path, autocommit=True)
    a.execute("PRAGMA journal_mode=WAL")
    a.executescript(MIGRATION.read_text(encoding="utf-8-sig"))
    a.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    a.autocommit = False
    b = sqlite3.connect(path, autocommit=False)
    try:
        store = ActionStore(a)
        dispatch(store, prepare(store, BotDo("held", (2, 3), "nc", "-bg,-lfg"), 1), 2)
        store.observe_send_error("held", "Timeout", now=3)
        assert held(store) == {2, 3}
        ActionStore(b).abandon("held", reason="reviewed", now=4)
        assert held(store) == {2, 3}  # a's open snapshot predates b's commit, fresh or cached
        same(store)
        a.commit()
        assert held(store) == set()
        same(store)
    finally:
        a.close(); b.close()


def test_a_bad_row_fails_closed_with_a_warm_cache():
    store, before = realistic_journal()
    assert held(store) == before
    row = store.conn.execute("SELECT command_id, evidence FROM realm_action ORDER BY seq LIMIT 1").fetchone()
    store.conn.execute("UPDATE realm_action SET evidence=? WHERE command_id=?", (row[1] + " ", row[0]))
    store.conn.commit()
    with pytest.raises(ActionUnavailable):
        store.holds()
    with pytest.raises(ActionUnavailable):
        store.records()
    store.conn.execute("UPDATE realm_action SET evidence=? WHERE command_id=?", row[::-1])
    store.conn.commit()
    assert held(store) == before
    store.conn.execute("ALTER TABLE realm_action RENAME TO gone")
    with pytest.raises(ActionUnavailable):
        store.holds()


def test_a_broken_parent_link_fails_closed_with_a_warm_cache():
    store, before = realistic_journal()
    store.holds()
    store.conn.execute("UPDATE realm_action SET parent_id='missing', role='cleanup' "
                       "WHERE seq=(SELECT max(seq) FROM realm_action)")
    with pytest.raises(ActionUnavailable):
        store.holds()
    store.conn.rollback()
    assert held(store) == before


def day_store():
    conn = journal_conn()
    conn.executescript(DAYS.read_text(encoding="utf-8-sig"))
    store = ActionStore(conn)
    token, boot = "c" * 32, 5_000
    with conn:
        conn.execute("INSERT INTO day_plan(id,night,member,starts,ends,activity,status,action_ref) "
                     "VALUES (1,'2026-11-22',2,0,100,'city','running','day:1')")
        conn.execute("INSERT INTO day_permit(plan,begin_id,token,boot,serial,guild,human,process,epoch,state) "
                     "VALUES (1,?,?,?,'9',50,1,'process','7','open')", ("d" * 32, token, boot))
        conn.execute("INSERT INTO day_step(id,plan,kind,member) VALUES ('step',1,'move',2)")
    move = p.RealmMove("step", 2, "home", day_permit=token)
    dispatch(store, prepare(store, move, 1, owner="day:1"), 2, started_ms=boot)
    with conn:
        conn.execute("UPDATE day_permit SET release_ids='[\"step\"]' WHERE plan=1")
        assert store.day_released(1, 3) is False  # still in flight: the seal waits, the hold stays
    assert "day_release" in json.loads(store.get("step").evidence)
    return store


def test_a_day_release_proof_is_proved_again_on_every_read():
    store = day_store()
    assert held(store) == {2}
    store.conn.execute("UPDATE day_permit SET serial='10' WHERE plan=1")  # only the day tables change
    with pytest.raises(ActionUnavailable):
        store.holds()
    with pytest.raises(ActionUnavailable):
        store.records()
    store.conn.rollback()
    assert held(store) == {2}
    same(store)


def test_the_journal_is_decoded_once_until_a_row_changes(monkeypatch):
    store, before = realistic_journal()
    rows = store.conn.execute("SELECT count(*) FROM realm_action").fetchone()[0]
    decoded = []
    real = store._decode_row
    monkeypatch.setattr(store, "_decode_row", lambda names, row: decoded.append(1) or real(names, row))
    for _ in range(20):
        assert held(store) == before
        store.records()
    assert len(decoded) == rows
    store.conn.execute("UPDATE realm_action SET updated=updated+1 WHERE seq=1")
    decoded.clear()
    held(store); held(store)
    assert len(decoded) == rows


def test_a_cached_read_of_the_live_sized_journal_is_cheap():
    store, before = realistic_journal()
    held(store)
    start = time.perf_counter()
    for _ in range(150):
        assert held(store) == before
    assert time.perf_counter() - start < 1.5  # 150 fresh decodes take several seconds
