"""Operator recovery of a blocked lab: stored native receipts only, never a bridge or a model."""

import asyncio
import hashlib
import json

import pytest

from leeroy_mind.app import acquire_instance_lock
from leeroy_mind.guild.loot import _get, _put
from leeroy_mind.training import cli, store
from leeroy_mind.training.body_holds import validate_lab_complete
from leeroy_mind.training.lab_recovery import LabClearRefused, clear_blocked_lab
from leeroy_mind.training.lab_runner import (ATTEMPT_PREFIX, CLEARED_PREFIX, CURSOR_KEY, NATIVE_PREFIX,
    NATIVE_START_KEY, OWNED_KEY, SUMMARY_PREFIX, LabRunner)
from leeroy_mind.training.protocol import DummyCleanup

from .test_tg_lab_runner import blocked, complete, lab, start  # noqa: F401  (fixture)


def meta(fake):
    return dict(fake.conn.execute("SELECT key,value FROM meta WHERE key GLOB 'tg:lab:*' ORDER BY key").fetchall())


def refused(fake, words):
    before = meta(fake)
    with pytest.raises(LabClearRefused, match=words) as error:
        clear_blocked_lab(fake.conn, fake.clock.now())
    assert meta(fake) == before
    return str(error.value)


def test_nothing_to_clear_without_a_blocked_lab(fake, lab):
    refused(fake, "no lab record")
    runner, _, _ = lab
    fake.acks["dummy_trial"].append((False, "busy"))
    asyncio.run(runner.tick())
    asyncio.run(runner.tick())
    assert _get(fake.conn, OWNED_KEY)["phase"] == "complete"
    refused(fake, "not blocked")


def test_a_cleanup_failed_trial_without_native_evidence_is_refused_in_words(fake, lab):
    runner, _, _ = lab
    ident = blocked(fake, runner)
    said = refused(fake, "no native evidence yet")
    assert ident in said and "101,102,103,104" in said and "cleanup_failed:cleanup_pending" in said
    assert "dummy_cleanup" in said and "worldserver restart" in said
    # A worldserver that started before the trial is the same process that ran it.
    with fake.conn:
        _put(fake.conn, NATIVE_START_KEY, dict(started_ms=runner._owned["current"]["sent_ms"] - 1, hello_ts=1, seen=1))
    refused(fake, "no native evidence yet")


def assert_cleared(fake, runner, ident, kind):
    owned = _get(fake.conn, OWNED_KEY)
    assert owned["phase"] == "complete" and owned["current"] is None
    assert owned["halt"] == "native_cleanup_unproved" and owned["recovered"]["evidence"]["kind"] == kind
    assert owned["attempts"] == len(owned["attempt_ids"]) == 1 and owned["gaps"] == [ident]
    attempt = _get(fake.conn, ATTEMPT_PREFIX + ident)
    assert attempt["restoration"] == "native_recovered" and attempt["recovered"]["trial"] == ident
    assert attempt["results"]  # Native's results stay as they came.
    summary = _get(fake.conn, SUMMARY_PREFIX + owned["night"])
    assert summary["reason"] == "native_cleanup_unproved" and summary["winner"] is None and summary["recovered"]
    assert _get(fake.conn, CURSOR_KEY) == owned["spec"]
    assert _get(fake.conn, CLEARED_PREFIX + owned["id"])["members"] == [101, 102, 103, 104]
    validate_lab_complete(fake.conn, owned)
    restarted = LabRunner(fake, fake.cfg, runner.ground, runner.applier, schedule=runner.schedule)
    assert not restarted.active and restarted.body_holds() == () and restarted.members() == set()
    assert fake.conn.execute("SELECT count(*) FROM tg_dummy").fetchone()[0] == 0  # Never a measurement.


@pytest.mark.parametrize("state", ["released", "restored"])
def test_natives_cleanup_receipt_clears_and_frees_the_members(fake, lab, state):
    runner, _, _ = lab
    ident = blocked(fake, runner)
    runner.feed(DummyCleanup(int(fake.clock.now() * 1000), ident, state, (101,) if state == "released" else (),
                             600000))
    result = clear_blocked_lab(fake.conn, fake.clock.now())
    assert result["evidence"]["kind"] == "dummy_cleanup" and result["evidence"]["state"] == state
    assert result["members_freed"] == [101, 102, 103, 104]
    assert _get(fake.conn, NATIVE_PREFIX + ident)["state"] == state  # The receipt itself is kept.
    assert_cleared(fake, runner, ident, "dummy_cleanup")


def test_a_worldserver_started_after_the_trial_clears_it(fake, lab):
    runner, _, _ = lab
    ident = blocked(fake, runner)
    terminal = runner._owned["current"]["terminal"]["ts"]
    with fake.conn:
        _put(fake.conn, NATIVE_START_KEY, dict(started_ms=terminal + 1, hello_ts=terminal + 2, seen=1))
    result = clear_blocked_lab(fake.conn, fake.clock.now())
    assert result["evidence"] == dict(kind="native_restart", trial=ident, started_ms=terminal + 1)
    assert_cleared(fake, runner, ident, "native_restart")


def test_a_member_with_a_standing_setup_is_refused_even_with_evidence(fake, lab):
    runner, _, _ = lab
    ident = blocked(fake, runner)
    runner.feed(DummyCleanup(int(fake.clock.now() * 1000), ident, "released", (101,), 600000))
    store.put_setup(fake.conn, dict(guid=103, since=fake.clock.now(), toggles=["+damage"]))
    said = refused(fake, "standing setup")
    assert "103" in said


def test_a_malformed_record_is_refused_without_repair(fake, lab):
    runner, _, _ = lab
    blocked(fake, runner)
    owned = _get(fake.conn, OWNED_KEY)
    owned["guids"] = "PRIVATE_SENTINEL"
    with fake.conn:
        _put(fake.conn, OWNED_KEY, owned)
    assert "PRIVATE_SENTINEL" not in refused(fake, "malformed")


def test_cli_clears_only_with_the_mind_stopped_and_says_why_it_refuses(fake, lab, tmp_path, capsys):
    runner, _, _ = lab
    ident = blocked(fake, runner)
    path = tmp_path / "mind.db"
    args = ["lab-clear", "--data-dir", str(tmp_path)]
    digest = hashlib.sha256(path.read_bytes()).digest()
    assert cli.training_main(args) == 1
    out = capsys.readouterr().out
    assert out.startswith("training lab-clear: refused: no native evidence yet") and ident in out
    assert hashlib.sha256(path.read_bytes()).digest() == digest
    runner.feed(DummyCleanup(int(fake.clock.now() * 1000), ident, "released", (101,), 600000))
    with acquire_instance_lock(tmp_path):
        assert cli.training_main(args) == 1
    assert "mind.lock is held" in capsys.readouterr().out
    assert _get(fake.conn, OWNED_KEY)["phase"] == "blocked"
    assert cli.training_main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["cleared"] == runner._owned["id"] and result["evidence"]["kind"] == "dummy_cleanup"
    fake.conn.commit()  # End this connection's read snapshot to see the CLI's write.
    assert _get(fake.conn, OWNED_KEY)["phase"] == "complete"
    assert cli.training_main(["lab", "--data-dir", str(tmp_path)]) == 0
    shown = json.loads(capsys.readouterr().out)["owned"]
    assert shown["phase"] == "complete" and shown["recovered"]["evidence"]["trial"] == ident


def test_a_clean_terminal_contradicted_by_a_stored_result_is_not_evidence(fake, lab):
    runner, _, _ = lab
    async def scenario():
        await start(runner)
        await complete(fake, runner, changes={"dropped": "cleanup_failed:ownership_changed"})
    asyncio.run(scenario())
    assert runner._owned["halt"] == "native_cleanup_unproved"
    assert runner._owned["current"]["terminal"]["reason"] == ""  # The terminal alone looks clean.
    refused(fake, "no native evidence yet")


def test_a_clean_terminal_clears_and_keeps_the_earlier_restoration_state(fake, lab):
    runner, _, applier = lab
    applier.apply_standing.side_effect = RuntimeError("synthetic")
    async def scenario():
        await start(runner)
        await complete(fake, runner)
    asyncio.run(scenario())
    assert runner._owned["halt"] == "standing_restoration_uncertain"
    ident = runner._owned["current"]["id"]
    result = clear_blocked_lab(fake.conn, fake.clock.now())
    assert result["evidence"]["kind"] == "native_terminal"
    attempt = _get(fake.conn, ATTEMPT_PREFIX + ident)
    assert attempt["restoration_before"] == "pending" and attempt["restoration"] == "native_recovered"
    validate_lab_complete(fake.conn, _get(fake.conn, OWNED_KEY))


def test_a_record_the_body_holds_would_reject_never_commits(fake, lab):
    runner, _, _ = lab
    ident = blocked(fake, runner)
    runner.feed(DummyCleanup(int(fake.clock.now() * 1000), ident, "released", (101,), 600000))
    owned = _get(fake.conn, OWNED_KEY)
    owned["alive"] = [999]  # Not a member: completing it would make the body holds fail closed.
    with fake.conn:
        _put(fake.conn, OWNED_KEY, owned)
    said = refused(fake, "would not be a valid completed lab")
    assert "nothing was written" in said
    assert _get(fake.conn, OWNED_KEY)["phase"] == "blocked"


def test_a_block_with_no_trial_in_flight_clears_without_native_evidence(fake, lab):
    runner, _, _ = lab
    asyncio.run(runner.tick())  # Acquire, probe and prime; nothing is sent to a body yet.
    assert runner._owned["phase"] == "ready" and runner._owned["current"] is None
    restarted = LabRunner(fake, fake.cfg, runner.ground, runner.applier, schedule=runner.schedule)
    assert restarted._owned["phase"] == "blocked" and restarted._owned["halt"] == "process_restart"
    result = clear_blocked_lab(fake.conn, fake.clock.now())
    assert result["evidence"] is None and result["members_freed"] == [101, 102, 103, 104]
    validate_lab_complete(fake.conn, _get(fake.conn, OWNED_KEY))
