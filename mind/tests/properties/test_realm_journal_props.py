"""The Realm action journal's cache (realm/action_store.py, #436) over generated histories:
prepares, dispatches, acks, native jobs, transport errors, releases, restarts, prunes and raw
SQL that commits or rolls back, with cached reads in between."""
import pytest

pytest.importorskip("hypothesis", reason='Hypothesis comes with the test extra: pip install -e ".[test]"')

from hypothesis import given, settings, strategies as st

from leeroy_mind.guild.protocol import BotDo, Job
from leeroy_mind.protocol import Ack
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_policy import classify
from leeroy_mind.realm.action_store import TERMINAL, ActionCapacity, ActionSpec, ActionStore, ActionUnavailable

from realm.journal import journal_conn

IDS = ("a", "b", "c", "d")
GUIDS = st.integers(2, 6)

ops = st.lists(st.one_of(
    st.tuples(st.just("move"), st.sampled_from(IDS), GUIDS, st.booleans()),
    st.tuples(st.just("defaults"), st.sampled_from(IDS),
              st.lists(GUIDS, min_size=1, max_size=3, unique=True).map(sorted).map(tuple), st.booleans()),
    st.tuples(st.just("dispatch"), st.sampled_from(IDS)),
    st.tuples(st.just("ack"), st.sampled_from(IDS), st.booleans()),
    st.tuples(st.just("job"), st.sampled_from(IDS), st.sampled_from(("done", "failed"))),
    st.tuples(st.just("send_error"), st.sampled_from(IDS), st.sampled_from(("Timeout", "NotConnected"))),
    st.tuples(st.just("predispatch"), st.sampled_from(IDS)),
    st.tuples(st.just("abandon"), st.sampled_from(IDS)),
    st.tuples(st.just("recover")),
    st.tuples(st.just("restart")),
    st.tuples(st.just("prune")),
    st.tuples(st.just("sql"), st.sampled_from(("touch", "delete", "corrupt")), st.sampled_from(IDS),
              st.sampled_from(("commit", "rollback", "savepoint"))),
    st.tuples(st.just("read")),
), max_size=25)


def fresh(store):
    """What an uncached reader of the same connection sees right now."""
    other = ActionStore(store.conn)
    try:
        return other._records(), other.holds()
    except ActionUnavailable:
        return ActionUnavailable, ActionUnavailable


def cached(store):
    try:
        return store.records(), store.holds()
    except ActionUnavailable:
        return ActionUnavailable, ActionUnavailable


def held(holds):
    return frozenset() if holds is ActionUnavailable else frozenset(g for h in holds for g in h.guids)


def prepare(store, cmd, now):
    c = classify(cmd)
    return store.prepare(ActionSpec(cmd.id, None, "root", c.policy, "realm_day", 4, c.kind, c.payload,
                                    c.reserved_manifest, 50, 1), now=now)


def apply(store, op, now):
    kind, *args = op
    if kind == "move":
        ident, guid, send = args
        r = prepare(store, p.RealmMove(ident, guid, "home"), now)
        if send:
            store.begin_dispatch(ident, payload=r.payload, process_id="process", connection_epoch=7, now=now,
                                 started_ms=int(now * 1000))
    elif kind == "defaults":
        ident, guids, send = args
        r = prepare(store, BotDo(ident, guids, "nc", "-bg,-lfg"), now)
        if send:
            store.begin_dispatch(ident, payload=r.payload, process_id="process", connection_epoch=7, now=now,
                                 started_ms=int(now * 1000))
    elif kind == "dispatch":
        r = store.get(args[0])
        if r is not None:
            store.begin_dispatch(r.command_id, payload=r.payload, process_id="process", connection_epoch=7,
                                 now=now, started_ms=int(now * 1000))
    elif kind == "ack":
        ident, ok = args
        store.observe_ack(Ack(0, ident, ok, "" if ok else "busy:combat"), now=now)
    elif kind == "job":
        ident, state = args
        r = store.get(ident)
        if r is None:
            return
        if r.kind == "realm_move":
            reason = "" if state == "done" else "busy:combat"
            store.observe_realm_job(p.RealmJob(0, ident, "realm_move", state, reason,
                                               r.reserved_manifest[0] if state == "done" else 0, 0), now=now)
        else:
            guids = r.reserved_manifest if state == "done" else ()
            store.observe_bot_job(Job(0, ident, r.kind, state, "" if state == "done" else "busy", "", guids, (),
                                      0, 0, 0, 0, 0, 0), now=now)
    elif kind == "send_error":
        store.observe_send_error(args[0], args[1], now=now)
    elif kind == "predispatch":
        store.predispatch_failed(args[0], reason="NotConnected", now=now)
    elif kind == "abandon":
        store.abandon(args[0], reason="reviewed", now=now)
    elif kind == "recover":
        store.recover(process_id="restarted", now=now)
    elif kind == "restart":
        store.restart_release(started_ms=int(now * 1000), guild=50, human=1, now=now)
    elif kind == "prune":
        store.prune(keep=1, now=now)
    elif kind == "sql":
        change, ident, end = args
        conn = store.conn
        if end == "savepoint":
            conn.execute("SAVEPOINT inner")
        if change == "touch":
            conn.execute("UPDATE realm_action SET updated=updated+1 WHERE command_id=?", (ident,))
        elif change == "delete":
            conn.execute("DELETE FROM realm_action WHERE command_id=? AND parent_id IS NULL", (ident,))
        else:  # evidence that is no longer canonical: the whole journal fails closed
            conn.execute("UPDATE realm_action SET evidence=evidence||' ' WHERE command_id=?", (ident,))
        cached(store)  # a read inside the open transaction, before it ends
        if end == "commit":
            conn.commit()
        elif end == "rollback":
            conn.rollback()
        else:
            conn.execute("ROLLBACK TO inner")
            conn.execute("RELEASE inner")
    else:
        cached(store)


@settings(max_examples=max(10, settings.default.max_examples // 2), deadline=None)
@given(ops)
def test_a_cached_journal_read_is_always_a_fresh_decode(script):
    """After any history of journal writes (the store's own reducers, refused calls, raw SQL that
    commits, rolls back or rolls back to a savepoint after a read inside its transaction):
    - records() and holds() equal what an uncached store decodes from the same connection, or
      both fail closed (ActionUnavailable) on a row that no longer validates;
    - a write rolled back, wholly or to a savepoint, leaves the holds exactly as they were before
      it: a held body never reads as free;
    - every root that is still open and has entered the transport holds its whole manifest."""
    conn = journal_conn()
    store = ActionStore(conn)
    try:
        now = 1_000.0
        for op in script:
            now += 7.0
            before = fresh(store)
            try:
                apply(store, op, now)
            except (ValueError, ActionCapacity, ActionUnavailable):
                pass  # a refused call; its own transaction rolled back
            after = fresh(store)
            assert cached(store) == after
            if op[0] == "sql" and op[3] != "commit":
                assert held(after[1]) == held(before[1]) and after == before
            if after[0] is not ActionUnavailable:
                for r in after[0]:
                    if r.role == "root" and r.state not in TERMINAL and r.transport_state != "not_entered":
                        assert set(r.reserved_manifest) <= held(after[1])
    finally:
        conn.close()
