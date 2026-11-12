import sqlite3

import pytest

from leeroy_mind.storage.store import Store


def test_sqlite_read_transaction_is_not_a_store_unit(store):
    assert store.conn.autocommit is False
    assert store.conn.in_transaction
    store.conn.execute("SELECT value FROM meta").fetchall()
    assert store.in_unit is False
    store.conn.commit()
    assert store.conn.in_transaction
    assert store.in_unit is False


def test_in_unit_tracks_nested_commit(store, tmp_path):
    observer = sqlite3.connect(tmp_path / "mind.db")
    try:
        assert store.in_unit is False
        with store.transaction():
            assert store.in_unit is True
            with store.transaction():
                assert store.in_unit is True
                store.set_meta("nested", "committed")
            assert store.in_unit is True
            assert observer.execute("SELECT value FROM meta WHERE key='nested'").fetchone() is None
        assert store.in_unit is False
        value = observer.execute("SELECT value FROM meta WHERE key='nested'").fetchone()[0]
        assert value == "committed"
    finally:
        observer.close()


def test_caught_inner_rollback_keeps_outer_unit_active(store):
    with store.transaction():
        store.set_meta("outer", "kept")
        with pytest.raises(RuntimeError), store.transaction():
            assert store.in_unit is True
            store.set_meta("inner", "undone")
            raise RuntimeError("inner failed")
        assert store.in_unit is True
        assert store.get_meta("inner") is None
        store.set_meta("after", "kept")
    assert store.in_unit is False
    assert (store.get_meta("outer"), store.get_meta("inner"), store.get_meta("after")) == (
        "kept", None, "kept")


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("nested", [False, True])
def test_rollback_cleans_up_unit_and_allows_reuse(store, error_type, nested):
    def fail():
        assert store.in_unit is True
        store.set_meta("failed", "undone")
        raise error_type("unit failed")

    with pytest.raises(error_type), store.transaction():
        assert store.in_unit is True
        if nested:
            with store.transaction():
                fail()
        else:
            fail()
    assert store.in_unit is False
    assert store.get_meta("failed") is None
    with store.transaction():
        assert store.in_unit is True
        store.set_meta("reused", "committed")
    assert store.in_unit is False
    assert store.get_meta("reused") == "committed"


def test_unit_ownership_is_local_to_each_store(store, clock):
    peer = Store(store.conn, clock=clock)
    assert store.in_unit is False and peer.in_unit is False
    with store.transaction():
        assert store.in_unit is True and peer.in_unit is False
    with peer.transaction():
        assert peer.in_unit is True and store.in_unit is False
    assert store.in_unit is False and peer.in_unit is False


def test_in_unit_is_read_only_and_has_no_sql_or_commit_side_effect(store):
    statements = []
    store.conn.set_trace_callback(statements.append)
    try:
        assert store.in_unit is False
        with pytest.raises(AttributeError):
            store.in_unit = True
        assert store.in_unit is False
        assert statements == []
        with pytest.raises(RuntimeError), store.transaction():
            store.set_meta("uncommitted", "undone")
            statements.clear()
            assert store.in_unit is True
            with pytest.raises(AttributeError):
                store.in_unit = False
            assert store.in_unit is True
            assert statements == []
            raise RuntimeError("roll back the unit")
        assert store.in_unit is False
        assert store.get_meta("uncommitted") is None
    finally:
        store.conn.set_trace_callback(None)


def test_a_read_left_open_does_not_fail_the_next_unit(store, tmp_path):
    """A read outside a unit leaves the connection's transaction open on a WAL snapshot. Once
    another connection (the signals writer) commits, a write in that stale snapshot fails at
    once with "database is locked" (SQLITE_BUSY_SNAPSHOT; the busy timeout cannot help), as
    tests/signals/test_integration.py did in CI. A unit starts from a fresh snapshot."""
    other = sqlite3.connect(tmp_path / "mind.db", timeout=2.0, isolation_level=None)
    try:
        store.conn.execute("SELECT value FROM meta").fetchall()  # left open
        other.execute("BEGIN IMMEDIATE")
        other.execute("INSERT INTO meta(key, value) VALUES ('other', 'x')")
        other.commit()
        with store.transaction():
            store.set_meta("mine", "y")
        assert other.execute("SELECT value FROM meta WHERE key='mine'").fetchone()[0] == "y"
    finally:
        other.close()
