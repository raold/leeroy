"""The direct-command ledger over a real Store: one exactly-once intent per ref."""
import asyncio
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from leeroy_mind.protocol import Ack
from leeroy_mind.realm.actions import ActionContext
from leeroy_mind.realm.direct import DirectLedger, DirectRefused, attach
from leeroy_mind.realm.protocol import Announce, CalendarPost, GuildReadmit, ItemCount, RealmJob, RealmMove, RealmSay

PROCESS = "direct-test-process"
LIVE = ActionContext(True, True, True, 3, PROCESS, 11, 50, 10)


def _say(text="wts frostweave, cheap"):
    return RealmSay(uuid.uuid4().hex, 201, "trade", text)


def _post():
    return CalendarPost(uuid.uuid4().hex, 201, "Raid night", event_type="raid", at=1800000000)


def _ack(cmd, ok=True, error=""):
    return Ack(1, cmd.id, ok, error)


def _job(cmd, kind="calendar_post", state="done", value=4242, reason=""):
    return RealmJob(1, cmd.id, kind, state, reason, 201, value)


@pytest.fixture
def ledger(store):
    return DirectLedger(store, process_id=PROCESS, clock=store.clock)


def _rows(store):
    return [tuple(r) for r in store.conn.execute(
        "SELECT command_id,owner,ref,state,reason,value FROM realm_direct ORDER BY rowid")]


def _dispatched(ledger, cmd, ref, *, owner="speech", expects_job=False):
    assert ledger.prepare(cmd, owner=owner, ref=ref, context=LIVE, expects_job=expects_job) == cmd.id
    ledger.before_send(cmd, context=LIVE)
    return cmd


def test_direct_prepare_once_per_ref(ledger, store):
    first, second = _say(), _say("wtb saronite")
    assert ledger.prepare(first, owner="speech", ref="line:1", context=LIVE, expects_job=False) == first.id
    assert ledger.prepare(second, owner="speech", ref="line:1", context=LIVE, expects_job=False) is None
    assert ledger.state("speech", "line:1") == ("prepared", None, None)
    # A ref belongs to its owner: another owner's same ref is its own intent.
    assert ledger.prepare(second, owner="mail", ref="line:1", context=LIVE, expects_job=False) == second.id
    assert [row[1:4] for row in _rows(store)] == [("speech", "line:1", "prepared"), ("mail", "line:1", "prepared")]
    assert ledger.owns_command(first) and ledger.owns_command(second)
    assert ledger.state("speech", "line:2") is None


def test_direct_prepare_refuses_what_it_cannot_own(ledger, store):
    for context in (ActionContext(False, True, True, 3, PROCESS, 11, 50, 10),
                    ActionContext(True, False, False, 3, PROCESS, 0, 0, 0),
                    ActionContext(True, True, True, 3, "another-process", 11, 50, 10)):
        assert ledger.prepare(_say(), owner="speech", ref="line:1", context=context, expects_job=False) is None
    for cmd in (RealmMove(uuid.uuid4().hex, 201, "uc_bank"),  # A body move stays on the queue.
                RealmSay("m1", 201, "trade", "short id"), RealmSay("A" * 32, 201, "trade", "upper hex"),
                GuildReadmit(uuid.uuid4().hex, 201, 1000, 2)):
        with pytest.raises(ValueError):
            ledger.prepare(cmd, owner="speech", ref="line:1", context=LIVE, expects_job=False)
    for owner, ref in (("night", "line:1"), ("speech", ""), ("speech", "line 1"), ("speech", "x" * 129)):
        with pytest.raises(ValueError):
            ledger.prepare(_say(), owner=owner, ref=ref, context=LIVE, expects_job=False)
    with pytest.raises(ValueError):
        ledger.prepare(_say(), owner="speech", ref="line:1", context=LIVE, expects_job=1)
    assert _rows(store) == []
    used = _say()
    ledger.prepare(used, owner="speech", ref="line:1", context=LIVE, expects_job=False)
    with pytest.raises(ValueError, match="identity"):  # A command id is one intent's, ever.
        ledger.prepare(used, owner="speech", ref="line:2", context=LIVE, expects_job=False)
    assert len(_rows(store)) == 1


def test_direct_prepare_joins_the_callers_unit(ledger, store):
    cmd = _say()
    with pytest.raises(RuntimeError, match="caller"):
        with store.transaction():
            assert ledger.prepare(cmd, owner="speech", ref="line:1", context=LIVE, expects_job=False) == cmd.id
            raise RuntimeError("caller failed")
    assert _rows(store) == []
    with store.transaction():
        ledger.prepare(cmd, owner="speech", ref="line:1", context=LIVE, expects_job=False)
    assert ledger.state("speech", "line:1") == ("prepared", None, None)


def test_direct_barrier_commits_before_wire(ledger, store, clock):
    cmd = _say()
    ledger.prepare(cmd, owner="speech", ref="line:1", context=LIVE, expects_job=False)
    path = store.conn.execute("PRAGMA database_list").fetchone()["file"]

    def on_the_wire():  # What another connection, and so a restart, would read at the write.
        reader = sqlite3.connect(path)
        try:
            return reader.execute("SELECT state,dispatched_ms FROM realm_direct WHERE command_id=?",
                                  (cmd.id,)).fetchone()
        finally:
            reader.close()

    assert on_the_wire() == ("prepared", None)
    with pytest.raises(RuntimeError):  # The barrier commits its own unit, never inside another.
        with store.transaction():
            ledger.before_send(cmd, context=LIVE)
    ledger.before_send(cmd, context=LIVE)
    assert on_the_wire() == ("dispatched", int(clock.now() * 1000))
    with pytest.raises(DirectRefused, match="settled"):  # Never through the barrier twice.
        ledger.before_send(cmd, context=LIVE)
    with pytest.raises(DirectRefused, match="not_owned"):
        ledger.before_send(_say(), context=LIVE)
    changed = RealmSay(cmd.id, 201, "trade", "another text")  # Its id, not its payload.
    assert not ledger.owns_command(changed)
    with pytest.raises(DirectRefused, match="not_owned"):
        ledger.before_send(changed, context=LIVE)
    assert not ledger.owns_command(Announce(cmd.id, "a" * 32 + ":1", 1, 50, 10, 631, 9, 0, 0, 10, False,
                                            "Lord Marrowgar", first="week"))


@pytest.mark.parametrize("context", [
    ActionContext(False, True, True, 3, PROCESS, 11, 50, 10),
    ActionContext(True, True, True, 3, PROCESS, 11, 51, 10),
    ActionContext(True, True, True, 3, PROCESS, 11, 50, 11),
    ActionContext(True, False, False, 4, PROCESS, 0, 0, 0),
])
def test_direct_barrier_refuses_another_context_and_frees_the_ref(ledger, store, context):
    cmd = _say()
    ledger.prepare(cmd, owner="speech", ref="line:1", context=LIVE, expects_job=False)
    with pytest.raises(DirectRefused, match="context_unavailable"):
        ledger.before_send(cmd, context=context)
    assert ledger.state("speech", "line:1") == ("abandoned", "context_unavailable", None)
    retry = _say()
    assert ledger.prepare(retry, owner="speech", ref="line:1", context=LIVE, expects_job=False) == retry.id


def test_direct_ack_and_job_either_order(ledger, store):
    ack_first = _dispatched(ledger, _post(), "raid:tue", owner="calendar", expects_job=True)
    job_first = _dispatched(ledger, _post(), "raid:wed", owner="calendar", expects_job=True)
    assert ledger.on_ack(ack_first, _ack(ack_first))
    assert ledger.state("calendar", "raid:tue") == ("acked", None, None)  # Waiting for its job.
    assert not ledger.on_job(_job(ack_first, kind="mail_send"))  # Its own kind only.
    assert ledger.on_job(_job(ack_first, value=4242))
    assert ledger.on_job(_job(job_first, value=(1 << 64) - 1))
    assert ledger.state("calendar", "raid:wed") == ("acked", None, (1 << 64) - 1)
    assert not ledger.on_ack(job_first, _ack(job_first))  # Settled by its job already.
    assert not ledger.on_ack(job_first, _ack(job_first, ok=False, error="cap"))
    for cmd in (ack_first, job_first):
        assert not ledger.on_job(_job(cmd, value=1))  # The first result stands.
        assert not ledger.on_ack(cmd, _ack(cmd))
    assert ledger.state("calendar", "raid:tue") == ("acked", None, 4242)
    assert ledger.state("calendar", "raid:wed") == ("acked", None, (1 << 64) - 1)
    # The same ref is done: it is never prepared again.
    assert ledger.prepare(_post(), owner="calendar", ref="raid:tue", context=LIVE, expects_job=True) is None


def test_direct_failed_job_is_refused_and_a_command_without_one_settles_on_its_ack(ledger):
    failed = _dispatched(ledger, _post(), "raid:thu", owner="calendar", expects_job=True)
    assert ledger.on_ack(failed, _ack(failed))
    assert ledger.on_job(_job(failed, state="failed", value=0, reason="calendar_full"))
    assert ledger.state("calendar", "raid:thu") == ("refused", "calendar_full", None)
    early = _dispatched(ledger, _post(), "raid:fri", owner="calendar", expects_job=True)
    assert ledger.on_job(_job(early, state="failed", value=0, reason="Not A Reason"))
    assert ledger.state("calendar", "raid:fri") == ("refused", "job_failed", None)
    said = _dispatched(ledger, _say(), "line:1")
    assert not ledger.on_job(RealmJob(1, said.id, "realm_say", "done", "", 201, 0))  # Expects none.
    assert ledger.on_ack(said, _ack(said))
    assert ledger.state("speech", "line:1") == ("acked", None, None)
    assert not ledger.on_job(RealmJob(1, said.id, "realm_say", "done", "", 201, 0))
    owned = RealmJob(1, early.id, "calendar_post", "done", "", 201, 1, pvp_owner="a" * 32 + "-" + "b" * 16)
    assert not ledger.on_job(owned)
    counted = _dispatched(ledger, ItemCount(uuid.uuid4().hex, 201, (3358,)), "count:201", owner="step",
                          expects_job=True)
    assert ledger.on_job(RealmJob(1, counted.id, "item_count", "done", "", 201, 17))
    assert ledger.state("step", "count:201") == ("acked", None, 17)


def test_direct_dispatched_restart_is_unknown_never_resent(store, clock):
    before = DirectLedger(store, process_id=PROCESS, clock=clock)
    cmd = _dispatched(before, _say(), "line:1")
    waiting = _dispatched(before, _post(), "raid:tue", owner="calendar", expects_job=True)
    assert before.on_ack(waiting, _ack(waiting))
    clock._t += 60
    after = DirectLedger(store, process_id="next-process", clock=clock)
    assert after.recover(process_id="next-process") == (0, 1)
    assert after.state("speech", "line:1") == ("unknown", "recovered_dispatched", None)
    context = ActionContext(True, True, True, 1, "next-process", 1, 50, 10)
    assert after.prepare(_say(), owner="speech", ref="line:1", context=context, expects_job=False) is None
    for ledger in (before, after):  # Unknown is final: no barrier, ack, job or error moves it.
        with pytest.raises(DirectRefused, match="settled"):
            ledger.before_send(cmd, context=LIVE)
        assert not ledger.on_ack(cmd, _ack(cmd))
        assert not ledger.on_send_error(cmd, "NotConnected")
        assert not ledger.abandon_unsent(cmd)
    assert after.prune(keep=0) == 0
    assert after.state("speech", "line:1") == ("unknown", "recovered_dispatched", None)
    # An acked command still waiting for its job keeps waiting: the job can still come.
    assert after.on_job(_job(waiting, value=31))
    assert after.state("calendar", "raid:tue") == ("acked", None, 31)
    assert after.recover(process_id="next-process") == (0, 0)


def test_direct_prepared_restart_abandoned_allows_retry(store, clock):
    before = DirectLedger(store, process_id=PROCESS, clock=clock)
    cmd = _say()
    before.prepare(cmd, owner="speech", ref="line:1", context=LIVE, expects_job=False)
    after = DirectLedger(store, process_id="next-process", clock=clock)
    context = ActionContext(True, True, True, 1, "next-process", 1, 50, 10)
    mine = _say("wtb titansteel")
    after.prepare(mine, owner="speech", ref="line:2", context=context, expects_job=False)
    assert after.recover(process_id="next-process") == (1, 0)  # Never its own process's rows.
    assert after.state("speech", "line:1") == ("abandoned", "recovered_prepared", None)
    assert after.state("speech", "line:2") == ("prepared", None, None)
    with pytest.raises(DirectRefused, match="settled"):  # The old payload is never replayed.
        before.before_send(cmd, context=LIVE)
    retry = _say()
    assert after.prepare(retry, owner="speech", ref="line:1", context=context, expects_job=False) == retry.id
    after.before_send(retry, context=context)
    assert after.state("speech", "line:1") == ("dispatched", None, None)


def test_direct_refused_allows_new_attempt(ledger):
    refused = _dispatched(ledger, _say(), "line:1")
    assert ledger.on_ack(refused, _ack(refused, ok=False, error="not_in_capital"))
    assert ledger.state("speech", "line:1") == ("refused", "not_in_capital", None)
    odd = _dispatched(ledger, _say(), "line:1")
    assert ledger.on_ack(odd, _ack(odd, ok=False, error="Not In Capital!"))
    assert ledger.state("speech", "line:1") == ("refused", "native_refused", None)
    unwritten = _dispatched(ledger, _say(), "line:1")
    assert ledger.on_send_error(unwritten, "NotConnected")
    assert ledger.state("speech", "line:1") == ("refused", "pretransport_refused", None)
    sent = _dispatched(ledger, _say(), "line:1")
    assert ledger.on_ack(sent, _ack(sent))
    assert ledger.state("speech", "line:1") == ("acked", None, None)
    assert ledger.prepare(_say(), owner="speech", ref="line:1", context=LIVE, expects_job=False) is None
    lost = _dispatched(ledger, _say(), "line:2")
    assert ledger.on_send_error(lost, "AckTimeout")  # It may have been written: never again.
    assert ledger.state("speech", "line:2") == ("unknown", "transport_unknown", None)
    assert ledger.prepare(_say(), owner="speech", ref="line:2", context=LIVE, expects_job=False) is None


def test_direct_abandon_unsent_only_closes_a_prepared_intent(ledger):
    cmd = _say()
    ledger.prepare(cmd, owner="speech", ref="line:1", context=LIVE, expects_job=False)
    assert ledger.abandon_unsent(cmd)
    assert not ledger.abandon_unsent(cmd)
    assert ledger.state("speech", "line:1") == ("abandoned", "unsent", None)
    sent = _dispatched(ledger, _say(), "line:1")
    assert not ledger.abandon_unsent(sent)  # Past the barrier, only its settlement closes it.
    assert ledger.state("speech", "line:1") == ("dispatched", None, None)


def test_direct_prune_keeps_live(ledger, store):
    for n in range(3):
        cmd = _dispatched(ledger, _say(), f"line:{n}")
        ledger.on_ack(cmd, _ack(cmd, ok=False, error="busy"))
    abandoned = _say()
    ledger.prepare(abandoned, owner="speech", ref="line:3", context=LIVE, expects_job=False)
    ledger.abandon_unsent(abandoned)
    ledger.prepare(_say(), owner="speech", ref="live:prepared", context=LIVE, expects_job=False)
    _dispatched(ledger, _say(), "live:dispatched")
    waiting = _dispatched(ledger, _post(), "live:waiting", owner="calendar", expects_job=True)
    ledger.on_ack(waiting, _ack(waiting))
    done = _dispatched(ledger, _say(), "live:acked")
    ledger.on_ack(done, _ack(done))
    lost = _dispatched(ledger, _say(), "live:unknown")
    ledger.on_send_error(lost, "ConnectionLost")
    live = [row for row in _rows(store) if row[2].startswith("live:")]
    assert ledger.prune(keep=1) == 3
    assert [row[2] for row in _rows(store) if not row[2].startswith("live:")] == ["line:3"]
    assert ledger.prune(keep=1) == 0
    assert ledger.prune(keep=0) == 1
    assert _rows(store) == live
    for keep in (-1, 65537, True, 1.0):
        with pytest.raises(ValueError):
            ledger.prune(keep=keep)


def test_direct_prune_keeps_a_calendar_posts_refusals_for_its_desk(ledger, store, clock):
    # The calendar desk reads a post's refusal reason to tell a busy one from a real one.
    posts = []
    for n in range(2):
        post = _dispatched(ledger, _post(), f"raid:2026-11-1{n}", owner="calendar", expects_job=True)
        ledger.on_ack(post, _ack(post, ok=False, error="busy:training_cleanup"))
        posts.append(post)
    for n in range(3):
        cmd = _dispatched(ledger, _say(), f"line:{n}")
        ledger.on_ack(cmd, _ack(cmd, ok=False, error="busy"))
    assert ledger.prune(keep=0) == 3  # Every other refusal goes; the posts' stay.
    assert [row[2:5] for row in _rows(store)] == [
        ("raid:2026-11-10", "refused", "busy_training_cleanup"),
        ("raid:2026-11-11", "refused", "busy_training_cleanup")]
    clock._t += 30 * 86400 - 1
    assert ledger.prune(keep=0) == 0
    clock._t += 2
    assert ledger.prune(keep=1) == 1  # A month on, they are ordinary refusals again.
    assert [row[0] for row in _rows(store)] == [posts[1].id]
    live = _dispatched(ledger, _post(), "raid:2026-12-01", owner="calendar", expects_job=True)
    clock._t += 31 * 86400
    assert ledger.prune(keep=0) == 1 and [row[0] for row in _rows(store)] == [live.id]  # Live: never.


def test_direct_attach_is_a_part_without_io(store, clock):
    realm = SimpleNamespace(app=SimpleNamespace(store=store, clock=clock), _process_id=PROCESS)
    changes = store.conn.total_changes
    part = attach(realm)
    assert type(part) is DirectLedger and part.store is store
    assert store.conn.total_changes == changes
    for n in range(3):
        cmd = _dispatched(part, _say(), f"line:{n}")
        part.on_ack(cmd, _ack(cmd, ok=False, error="busy"))

    async def ticks():
        await part.on_event(object())
        await part.tick()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(part, "prune", lambda keep=512: calls.append(keep) or 0)
        calls = []
        asyncio.run(ticks())
        asyncio.run(ticks())  # Within the hour: no second prune.
        clock._t += 3600
        asyncio.run(ticks())
    assert calls == [512, 512]
    assert len(_rows(store)) == 3
