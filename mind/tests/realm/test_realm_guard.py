"""His kick, the raid lead's one question, his exact answer and the one readmission it allows.

Unit tests drive the KickGuard over actual Store rows and real row bindings. Service tests run
the real MindApp, bridge reader and speech pipeline; only the byte stream is a test boundary.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from conftest import TEST_CAST, make_persona, run as run_loop
from engine_rig import HUMAN as RIG_HUMAN, engine_clock, make_rig
from leeroy_mind.bridge import AckTimeout
from leeroy_mind.ingress import RowReceipt, _wire_origin, chat_row_binding, game_row_binding
from leeroy_mind.personas.io import PersonaCache, save_persona
from leeroy_mind.protocol import Ack, Chat
from leeroy_mind.realm import protocol
from leeroy_mind.realm.guard import (FALLBACK, QUESTION, RETRY_MS, WINDOW_MS, Admission, GuardRefused,
                                     KickGuard, readmit_rank)
from leeroy_mind.realm.ports import LivePorts
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.storage import db
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.wow import CLASSES, RACES
from realm.fakes import RealmApp
from test_realm_foundation_schema import _episode, _insert, _source
from test_realm_live_proof import CONTEXT, canonical, insert, roster  # noqa: F401 (fixture)
from test_realm_source_admission import application, reached, run as run_native

HUMAN, LEAD, TARGET, OTHER = 10, 102, 103, 104
T0 = datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc).timestamp()  # The conftest clock.
KICK_MS = int(T0 * 1000) - 60_000  # He kicked Coros a minute ago.


class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t


class Ports:
    """The two reads the guard takes from the Realm's ports, over the same Store."""

    def __init__(self, store):
        self.store = store
        self.personas = PersonaCache(store)

    def cast(self):
        return [m for m in self.store.cast().values()
                if m.guid != HUMAN and m.level >= 80 and self.persona(m.guid) is not None]

    def persona(self, guid):
        return self.personas.get(guid)


def persona(store, guid, **over):
    name, race, cls = TEST_CAST[guid]
    save_persona(store, make_persona(guid, name, **over), source="seed", ts=1000.0,
                 race=RACES[race], class_name=CLASSES[cls])


def raid_lead(store, guid=LEAD, **over):
    persona(store, guid, officer_title="Raid Lead", guild_role="officer", **over)


def kick_event(store, *, guid=TARGET, ts=KICK_MS, rank=4, received=T0, backlog=False, row_payload=None):
    data = dict(ts=ts, guid=guid, action="his_kick", rank=rank, reason="")
    wire = _wire_origin(canonical(dict(v=1, type="guard", **data)), received_at=received, transport_epoch=7)
    stored = data if row_payload is None else row_payload
    with store.transaction():
        row_id = insert(store, "game_event", ts=received, type="guard", guid=guid, payload=canonical(stored),
                        session_id=None)
    receipt = RowReceipt("game_event", row_id, game_row_binding(row_id, "guard", stored), wire, backlog)
    return protocol.Guard(**data), receipt


def chat_event(store, text, *, ts, from_guid=HUMAN, to_guid=LEAD, received, channel="whisper",
               via_bridge=False, backlog=False, origin="human"):
    ev = Chat(ts, channel, from_guid, "Testmaster", to_guid, text, 0, 0, via_bridge)
    body = dict(v=1, type="chat", ts=ts, channel=channel, from_guid=from_guid, from_name="Testmaster",
                to_guid=to_guid, text=text, map=0, zone=0, via_bridge=via_bridge)
    wire = _wire_origin(canonical(body), received_at=received, transport_epoch=7)
    line_id = store.insert_chat_line(ev, session_id=None, origin=origin)
    row = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (line_id,)).fetchone()
    binding = chat_row_binding(line_id, **{key: row[key] for key in (
        "channel", "from_guid", "from_name", "to_guid", "text", "map", "zone", "origin")})
    return ev, RowReceipt("chat_line", line_id, binding, wire, backlog)


def mind_line(store, *, speaker=LEAD, to_guid=HUMAN, channel="whisper", text="saw coros got kicked"):
    return store.insert_chat_line(Chat(0, channel, speaker, "Belra", to_guid, text, 0, 0),
                                  session_id=None, origin="mind")


def his_line(store, text, *, to_guid=LEAD, channel="whisper"):
    return store.insert_chat_line(Chat(0, channel, HUMAN, "Testmaster", to_guid, text, 0, 0),
                                  session_id=None, origin="human")


class Rig:
    """The guard over the roster store, with a clock it moves by hand."""

    def __init__(self, store, *, process_id=CONTEXT.process_id):
        self.store = store
        self.clock = Clock(T0)
        self.ports = Ports(store)
        self.guard = KickGuard(store, self.ports, process_id=process_id, clock=self.clock)
        self.n = 0

    def kick(self, context=CONTEXT, **kw):
        ev, receipt = kick_event(self.store, **kw)
        return self.guard.retain(ev, receipt, context)

    def ask(self, *, ready=lambda guid: True, context=CONTEXT):
        return self.guard.reserve(context, ready=ready)

    def deliver(self, intent, *, ack_ts=None):
        assert self.guard.before_transport(intent, context=CONTEXT)
        line = mind_line(self.store, speaker=intent.lead_guid, text=intent.text)
        ack = Ack(int(self.clock.t * 1000) if ack_ts is None else ack_ts, "m1", True, "")
        assert self.guard.on_recorded(intent, line, ack)
        assert self.guard.settle_question(intent) == "delivered"
        return line

    def asked(self, **kw):
        assert self.kick(**kw)
        intent = self.ask()
        self.deliver(intent)
        return intent

    def answer(self, text="yes", *, context=CONTEXT, after=1.0, **kw):
        self.n += 1
        self.clock.t += after
        at = kw.pop("received", self.clock.t)
        ev, receipt = chat_event(self.store, text, ts=int(at * 1000) + self.n, received=at, **kw)
        return self.guard.answer(ev, receipt, context)

    def row(self, guid=TARGET):
        return dict(self.store.conn.execute("SELECT * FROM realm_kick_episode WHERE guid=?", (guid,)).fetchone())

    def states(self, guid=TARGET):
        row = self.row(guid)
        return row["ask_state"], row["consent_state"], row["admission_state"], row["admission_reason"]


@pytest.fixture
def rig(roster):
    raid_lead(roster)
    return Rig(roster)


def count(store, table, where="1"):
    return store.conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]


# -- his kick ---------------------------------------------------------------------------------

def test_his_kick_retains_one_episode_bound_to_its_own_row(rig):
    episode = rig.kick()
    row = rig.row()
    assert episode == row["episode_id"] and episode.startswith("kick:") and len(episode) == 69
    assert (row["guid"], row["guild"], row["human"], row["kick_ms"], row["old_rank"]) == (TARGET, 50, HUMAN,
                                                                                         KICK_MS, 4)
    assert (row["ask_state"], row["consent_state"], row["admission_state"]) == ("unasked", "none", "none")
    assert row["ask_expires_ms"] == KICK_MS + WINDOW_MS == KICK_MS + 72 * 3600 * 1000
    source = rig.store.conn.execute("SELECT * FROM realm_source WHERE id=?", (row["source"],)).fetchone()
    assert (source["event_type"], source["chat_line_id"], source["server_ms"]) == ("guard", None, KICK_MS)
    assert source["game_event_id"] == rig.store.conn.execute(
        "SELECT max(id) FROM game_event WHERE type='guard'").fetchone()[0]
    # The same envelope again (a replay, another row): its episode stands, nothing is added.
    assert rig.kick() == episode
    assert count(rig.store, "realm_kick_episode") == 1 and count(rig.store, "realm_source") == 1


@pytest.mark.parametrize("change", [
    dict(row_payload=dict(ts=KICK_MS, guid=OTHER, action="his_kick", rank=4, reason="")),  # Another row.
    dict(guid=HUMAN),  # Never his own guid.
])
def test_a_kick_without_its_exact_row_or_of_him_is_never_retained(rig, change):
    assert rig.kick(**change) is None
    assert count(rig.store, "realm_kick_episode") == 0 and count(rig.store, "realm_source") == 0


@pytest.mark.parametrize("context", [replace(CONTEXT, enabled=False), replace(CONTEXT, process_id="other"),
                                     replace(CONTEXT, presence_known=False)])
def test_a_kick_outside_a_live_realm_is_never_retained(rig, context):
    assert rig.kick(context=context) is None
    assert count(rig.store, "realm_kick_episode") == 0


def test_other_guard_steps_are_not_kicks(rig):
    ev, receipt = kick_event(rig.store)
    for action in ("readded", "gone", "blocked", "failed"):
        assert rig.guard.retain(replace(ev, action=action), receipt, CONTEXT) is None
    assert count(rig.store, "realm_kick_episode") == 0


# -- the question -----------------------------------------------------------------------------

def test_his_kick_gets_one_question_from_the_standing_raid_lead(rig):
    rig.kick()
    intent = rig.ask()
    assert (intent.lead_guid, intent.target_guid, intent.human_guid, intent.guild_id) == (LEAD, TARGET, HUMAN, 50)
    assert intent.text == QUESTION.format(name="Coros") == "saw Coros got kicked, want them back? (yes/no)"
    row = rig.row()
    assert (row["ask_state"], row["lead"], row["ask_id"]) == ("reserved", LEAD, intent.ask_id)
    assert (row["ask_process_id"], row["ask_generation"], row["ask_connection_epoch"]) == (
        CONTEXT.process_id, CONTEXT.generation, str(CONTEXT.connection_epoch))
    # Reserved before any send, and never reserved twice: a second tick finds nothing due.
    assert rig.ask() is None
    rig.deliver(intent)
    assert rig.ask() is None and rig.kick() is not None and rig.ask() is None
    assert rig.states() == ("delivered", "none", "none", None)


@pytest.mark.parametrize("setup", ["none", "two", "healer", "former", "unready"])
def test_nobody_asks_without_one_ready_unambiguous_lead(roster, setup):
    if setup != "none":
        raid_lead(roster, **({"raid_role": "healer"} if setup == "healer" else {}))
    if setup == "two":
        raid_lead(roster, OTHER)
    if setup == "former":
        with roster.transaction():
            insert(roster, "realm_kick", guid=LEAD, ts=1.0, rank=3)
    r = Rig(roster)
    assert r.kick()
    assert r.ask(ready=lambda guid: setup != "unready") is None
    assert r.states() == ("unasked", "none", "none", None)


def test_the_lead_never_asks_about_himself(rig):
    rig.kick(guid=LEAD)
    assert rig.ask() is None
    assert rig.states(LEAD) == ("unasked", "none", "none", None)


def test_one_open_question_per_lead(rig):
    first = rig.asked()
    rig.kick(guid=OTHER, ts=KICK_MS + 1)
    assert rig.ask() is None  # He could not tell which member his answer is about.
    assert rig.answer("no") == "no"
    second = rig.ask()
    assert second.target_guid == OTHER and second.ask_id != first.ask_id


def test_the_barrier_admits_one_dispatch_of_its_own_context_only(rig):
    rig.kick()
    intent = rig.ask()
    assert not rig.guard.before_transport(intent, context=replace(CONTEXT, generation=4))
    assert rig.states() == ("refused", "none", "none", None)
    assert rig.row()["dispatched_ms"] is None
    assert not rig.guard.before_transport(intent, context=CONTEXT)  # Refused is final for this ask.
    rig.clock.t += RETRY_MS / 1000
    again = rig.ask()
    assert again.ask_id != intent.ask_id
    assert rig.guard.before_transport(again, context=CONTEXT)
    assert rig.row()["dispatched_ms"] == int(rig.clock.t * 1000)
    # Exactly one dispatch: a second line of the turn, or a retry, never passes.
    assert not rig.guard.before_transport(again, context=CONTEXT)
    assert rig.ask() is None


def test_a_refused_question_is_asked_again_only_after_its_backoff(rig):
    assert RETRY_MS == 5 * 60 * 1000
    rig.kick()
    intent = rig.ask()
    assert rig.guard.settle_question(intent) == "refused"  # It never reached the barrier.
    rig.clock.t += 299
    assert rig.ask() is None
    rig.clock.t += 1
    assert rig.ask() is not None


def test_an_unrecorded_dispatch_is_unknown_and_blocks_until_every_window_passed(rig):
    rig.kick()
    intent = rig.ask()
    assert rig.guard.before_transport(intent, context=CONTEXT)
    # No ack recorded (a timeout, a lost connection, a refused say): it may have gone out.
    assert rig.guard.on_recorded(intent, mind_line(rig.store), Ack(1, "m1", False, "offline")) is False
    assert rig.guard.settle_question(intent) == "unknown"
    assert rig.answer("yes") is None  # Never answered: nothing proves he saw it.
    rig.kick(guid=OTHER, ts=KICK_MS + 1)
    rig.clock.t += 2 * 3600
    assert rig.ask() is None  # Never asked again, and it holds the lead's one question.
    assert rig.guard.sweep() == 0
    rig.clock.t = (KICK_MS + WINDOW_MS + 1000) / 1000
    assert rig.guard.sweep() == 1  # Only the other kick, never asked, is past its ask window.
    assert rig.states() == ("unknown", "none", "none", None)  # A reply window could still be open.
    rig.clock.t = (KICK_MS + 2 * WINDOW_MS) / 1000
    assert rig.guard.sweep() == 1  # Now no answer could matter any more.
    assert rig.states() == ("expired", "expired", "none", None)
    assert count(rig.store, "realm_kick_episode", "admission_command_id IS NOT NULL") == 0


def test_the_recorded_line_must_be_the_leads_typed_whisper_to_him(rig):
    rig.kick()
    intent = rig.ask()
    assert rig.guard.before_transport(intent, context=CONTEXT)
    ack = Ack(int(T0 * 1000), "m1", True, "")
    for line in (mind_line(rig.store, channel="guild", text=intent.text),
                 mind_line(rig.store, speaker=OTHER, text=intent.text),
                 mind_line(rig.store, to_guid=OTHER, text=intent.text),
                 # Restyled on its way out: not the question whose hash the row keeps.
                 mind_line(rig.store, text="saw coros got kicked, want them back? (yes/no) :)")):
        assert rig.guard.on_recorded(intent, line, ack) is False
    assert rig.row()["ask_text_sha256"] == hashlib.sha256(intent.text.encode("utf-8")).hexdigest()
    assert rig.guard.on_recorded(intent, mind_line(rig.store, text=intent.text), ack) is True
    row = rig.row()
    assert (row["ask_state"], row["delivered_ms"], row["reply_expires_ms"]) == (
        "delivered", row["ask_ack_ms"], row["ask_ack_ms"] + WINDOW_MS)
    assert row["dispatched_ms"] <= row["ask_ack_ms"] <= row["ack_received_ms"]


def test_native_ack_time_is_bounded_by_the_barrier_and_its_receipt(rig):
    rig.kick()
    intent = rig.ask()
    assert rig.guard.before_transport(intent, context=CONTEXT)
    rig.clock.t += 2
    assert rig.guard.on_recorded(intent, mind_line(rig.store, text=intent.text), Ack(5, "m1", True, ""))
    row = rig.row()
    assert row["ask_ack_ms"] == row["dispatched_ms"] == int(T0 * 1000)
    assert row["ack_received_ms"] == int((T0 + 2) * 1000)


def test_no_question_after_its_window(rig):
    rig.kick()
    rig.clock.t = (KICK_MS + WINDOW_MS) / 1000
    assert rig.ask() is None
    assert rig.guard.sweep() == 1
    assert rig.states() == ("expired", "expired", "none", None)


# -- his answer -----------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [("yes", "yes"), (" Yes ", "yes"), ("YES", "yes"), ("no", "no"),
                                           ("\tNo\n", "no")])
def test_his_exact_answer_is_stored_with_its_own_source(rig, text, expected):
    rig.asked()
    result = rig.answer(text)
    row = rig.row()
    assert row["answer"] == expected
    assert row["consent_state"] == ("pending_native" if expected == "yes" else "declined")
    assert (type(result) is Admission) == (expected == "yes")
    source = rig.store.conn.execute("SELECT * FROM realm_source WHERE id=?", (row["answer_source"],)).fetchone()
    assert (source["event_type"], source["game_event_id"]) == ("chat", None)
    assert source["chat_line_id"] == rig.store.conn.execute("SELECT max(id) FROM chat_line").fetchone()[0]
    assert row["delivered_ms"] <= row["answered_ms"] < row["reply_expires_ms"]


@pytest.mark.parametrize("text", ["yes!", "yes please", "y", "yeah", "sure", "nope", "no.", ""])
def test_anything_but_an_exact_yes_or_no_is_no_answer(rig, text):
    rig.asked()
    assert rig.answer(text) is None
    assert rig.states() == ("delivered", "none", "none", None)


@pytest.mark.parametrize("change", [
    dict(from_guid=OTHER, origin="game"), dict(to_guid=OTHER), dict(channel="guild"),
    dict(via_bridge=True), dict(backlog=True), dict(origin="game"),
])
def test_only_his_live_whisper_to_the_lead_answers(rig, change):
    rig.asked()
    assert rig.answer("yes", **change) is None
    assert rig.states() == ("delivered", "none", "none", None)


def test_an_answer_before_delivery_or_after_the_window_is_none(rig):
    rig.kick()
    intent = rig.ask()
    assert rig.answer("yes") is None  # Reserved, not delivered: he has not been asked.
    rig.deliver(intent)
    delivered = rig.row()["delivered_ms"]
    assert rig.answer("yes", received=delivered / 1000 - 1) is None  # Received before it was asked.
    rig.clock.t = (delivered + WINDOW_MS) / 1000
    assert rig.answer("yes", after=0) is None
    assert rig.guard.sweep() == 1
    assert rig.states() == ("expired", "expired", "none", None)


def test_his_kick_is_final_without_his_yes(rig):
    rig.asked()
    rig.clock.t += 73 * 3600  # 73 h of silence: silence is never a yes.
    assert rig.answer("yes") is None
    rig.guard.sweep()
    assert rig.states() == ("expired", "expired", "none", None)
    r2 = Rig(rig.store)
    r2.asked(guid=OTHER, ts=KICK_MS + 5)
    assert r2.answer("no") == "no" and r2.answer("yes") is None  # His no is final too.
    assert r2.states(OTHER) == ("delivered", "declined", "none", None)
    assert count(rig.store, "realm_kick_episode", "admission_command_id IS NOT NULL") == 0


def test_a_second_answer_changes_nothing(rig):
    rig.asked()
    first = rig.answer("yes")
    assert rig.answer("yes") is None and rig.answer("no") is None
    assert rig.row()["admission_command_id"] == first.command_id
    assert count(rig.store, "realm_source", "event_type='chat'") == 1


# -- his answer answers the question, and nothing else ---------------------------------------

@pytest.mark.parametrize("between", [
    lambda store: mind_line(store, text="u coming to icc tonight?"),  # The lead asked something else.
    lambda store: his_line(store, "wait which one"),  # He said something first.
])
@pytest.mark.parametrize("word", ["yes", "no"])
def test_a_whisper_between_the_question_and_his_answer_unties_it(rig, between, word):
    rig.asked()
    between(rig.store)
    sources = count(rig.store, "realm_source")
    assert rig.answer(word) is None  # It may answer that line: his yes readmits nobody, his no declines nothing.
    assert rig.states() == ("delivered", "none", "none", None)
    row = rig.row()
    assert (row["answer"], row["answer_source"], row["answered_ms"]) == (None, None, None)
    assert count(rig.store, "realm_source") == sources  # Refused before anything is consumed.
    assert rig.answer("yes") is None and rig.answer("no") is None  # The line still lies between.
    rig.clock.t = row["reply_expires_ms"] / 1000
    assert rig.guard.sweep() == 1
    assert rig.states() == ("expired", "expired", "none", None)


def test_only_a_whisper_between_the_lead_and_him_unties_his_answer(rig):
    rig.asked()
    mind_line(rig.store, channel="guild", to_guid=0, text="u coming to icc tonight?")
    mind_line(rig.store, to_guid=OTHER, text="u coming to icc tonight?")
    mind_line(rig.store, speaker=OTHER, text="u coming to icc tonight?")
    his_line(rig.store, "lol", to_guid=OTHER)
    his_line(rig.store, "brb", channel="guild", to_guid=0)
    admission = rig.answer("yes")  # The question itself is the line before it.
    assert type(admission) is Admission and rig.states() == ("delivered", "pending_native", "prepared", None)


def test_an_answer_stored_before_its_question_answers_nothing(rig):
    rig.kick()
    intent = rig.ask()
    assert rig.guard.before_transport(intent, context=CONTEXT)
    ev, receipt = chat_event(rig.store, "yes", ts=int(T0 * 1000) + 1, received=T0 + 1)
    ack = Ack(int(T0 * 1000), "m1", True, "")
    assert rig.guard.on_recorded(intent, mind_line(rig.store, text=intent.text), ack)
    rig.clock.t = T0 + 1
    assert rig.row()["delivered_ms"] <= int((T0 + 1) * 1000)  # In its window, but stored first.
    assert rig.guard.answer(ev, receipt, CONTEXT) is None
    assert rig.states() == ("delivered", "none", "none", None)


# -- the readmission --------------------------------------------------------------------------

@pytest.mark.parametrize("old,rank", [(0, 0), (1, 2), (2, 2), (4, 4), (9, 9), (10, 0), (255, 0)])
def test_readmission_rank_follows_native(old, rank):
    assert readmit_rank(old) == rank


@pytest.mark.parametrize("old,rank", [(4, 4), (1, 2), (7, 7)])
def test_his_yes_readmits_once_at_the_kicks_rank(rig, old, rank):
    rig.asked(rank=old)
    admission = rig.answer("yes")
    assert admission.state == "prepared" and admission.rank == rank and admission.name == "Coros"
    cmd = rig.guard.admission_command(admission)
    assert cmd == protocol.GuildReadmit(admission.command_id, TARGET, KICK_MS, rank)
    assert rig.states() == ("delivered", "pending_native", "prepared", None)
    rig.guard.before_admission(cmd, context=CONTEXT)
    assert rig.states()[2] == "dispatched"
    with pytest.raises(GuardRefused, match="settled"):
        rig.guard.before_admission(cmd, context=CONTEXT)  # Never sent twice.
    assert rig.guard.on_admission_ack(cmd, Ack(1, cmd.id, True, ""))
    assert rig.states() == ("delivered", "pending_native", "admitted", None)
    assert not rig.guard.on_admission_ack(cmd, Ack(1, cmd.id, False, "late"))
    assert not rig.guard.on_admission_send_error(cmd, "AckTimeout")
    with pytest.raises(GuardRefused, match="settled"):
        rig.guard.before_admission(cmd, context=CONTEXT)


def test_an_unknown_rank_sends_nothing_and_is_refused(rig):
    rig.asked(rank=0)
    admission = rig.answer("yes")
    assert (admission.state, admission.rank) == ("refused", 0)
    assert rig.states() == ("delivered", "pending_native", "refused", "rank_unknown")
    with pytest.raises(ValueError):
        rig.guard.admission_command(admission)  # No valid command exists for it.


@pytest.mark.parametrize("change", [dict(rank=5), dict(kick_ts=KICK_MS + 1), dict(guid=OTHER), dict(id="forged_1")])
def test_only_the_exact_owned_readmission_passes_its_barrier(rig, change):
    rig.asked()
    cmd = rig.guard.admission_command(rig.answer("yes"))
    forged = replace(cmd, **change)
    assert not rig.guard.owns_admission(forged)
    with pytest.raises(GuardRefused, match="not_owned"):
        rig.guard.before_admission(forged, context=CONTEXT)
    assert rig.states()[2] == "prepared" and rig.guard.owns_admission(cmd)


def test_a_changed_context_refuses_the_readmission_for_good(rig):
    rig.asked()
    cmd = rig.guard.admission_command(rig.answer("yes"))
    with pytest.raises(GuardRefused, match="context_unavailable"):
        rig.guard.before_admission(cmd, context=replace(CONTEXT, enabled=False))
    assert rig.states() == ("delivered", "pending_native", "refused", "context_unavailable")
    with pytest.raises(GuardRefused, match="settled"):
        rig.guard.before_admission(cmd, context=CONTEXT)


@pytest.mark.parametrize("ok,error,outcome", [
    (False, "not_kicked", ("refused", "not_kicked")),  # Native has no receipt of that kick.
    (False, "receipt_unstored", ("refused", "receipt_unstored")),  # Refused before any add.
    (False, "kick_expired", ("refused", "kick_expired")),
    (False, "already_member", ("refused", "already_member")),
    (False, "not_guildless", ("refused", "not_guildless")),
    (False, "faction", ("refused", "faction")),
    (False, "gone", ("refused", "gone")),
    (False, "guild not found", ("refused", "native_refused")),
    (False, "guild_join_unproved", ("unknown", "guild_join_unproved")),
    (True, "", ("admitted", None)),
])
def test_native_outcomes_settle_the_readmission(rig, ok, error, outcome):
    rig.asked()
    cmd = rig.guard.admission_command(rig.answer("yes"))
    rig.guard.before_admission(cmd, context=CONTEXT)
    assert not rig.guard.on_admission_ack(cmd, Ack(1, "other", ok, error))
    assert rig.guard.on_admission_ack(cmd, Ack(1, cmd.id, ok, error))
    assert rig.states()[2:] == outcome


@pytest.mark.parametrize("error,outcome", [("NotConnected", ("refused", "pretransport_refused")),
                                           ("RealmDisabled", ("refused", "pretransport_refused")),
                                           ("AckTimeout", ("unknown", "transport_unknown")),
                                           ("ConnectionLost", ("unknown", "transport_unknown"))])
def test_a_failed_transport_is_final(rig, error, outcome):
    rig.asked()
    cmd = rig.guard.admission_command(rig.answer("yes"))
    assert not rig.guard.on_admission_send_error(cmd, error)  # Not dispatched: nothing to settle.
    rig.guard.before_admission(cmd, context=CONTEXT)
    assert rig.guard.on_admission_send_error(cmd, error)
    assert rig.states()[2:] == outcome
    assert not rig.guard.abandon_admission(cmd)


def test_an_unsent_readmission_is_abandoned_once(rig):
    rig.asked()
    cmd = rig.guard.admission_command(rig.answer("yes"))
    assert rig.guard.abandon_admission(cmd)
    assert rig.states()[2:] == ("refused", "context_unavailable")
    assert not rig.guard.abandon_admission(cmd)


# -- recovery -------------------------------------------------------------------------------

def test_recovery_reserved_unknown_prepared_refused_dispatched_unknown(roster):
    raid_lead(roster)
    rig = Rig(roster)
    rig.asked(guid=TARGET)
    prepared = rig.answer("yes")
    rig.kick(guid=OTHER, ts=KICK_MS + 1)
    rig.kick(guid=105, ts=KICK_MS + 2)
    raid_lead(roster, 101)  # Two leads now: nothing else can be asked.
    with roster.transaction():
        roster.conn.execute("UPDATE realm_kick_episode SET lead=?,ask_id='ask_x',ask_text_sha256=?,"
                            "ask_process_id=?,ask_generation=0,ask_connection_epoch='1',attempted_ms=?,"
                            "ask_state='reserved' WHERE guid=?",
                            (101, "a" * 64, CONTEXT.process_id, KICK_MS, OTHER))
    assert rig.guard.recover(process_id=CONTEXT.process_id) == (0, 1, 0)  # Its own rows stay reserved.
    assert rig.states(OTHER)[0] == "reserved"
    assert rig.states() == ("delivered", "pending_native", "refused", "recovered_prepared")
    with roster.transaction():  # A dispatched admission of an earlier process.
        roster.conn.execute("UPDATE realm_kick_episode SET admission_state='dispatched',admission_reason=NULL,"
                            "admission_settled_ms=NULL,admission_dispatched_ms=? WHERE guid=?",
                            (int(T0 * 1000), TARGET))
    nxt = Rig(roster, process_id="next")
    assert nxt.guard.recover(process_id="next") == (1, 0, 1)
    assert nxt.states(OTHER)[0] == "unknown" and nxt.states(105)[0] == "unasked"
    assert nxt.states() == ("delivered", "pending_native", "unknown", "recovered_dispatched")
    cmd = nxt.guard.admission_command(prepared)
    with pytest.raises(GuardRefused, match="settled"):
        nxt.guard.before_admission(cmd, context=replace(CONTEXT, process_id="next"))


def test_the_service_recovers_before_anything_can_send_enabled_or_not(tmp_path, roster, clock):
    raid_lead(roster)
    rig = Rig(roster)
    rig.kick()
    rig.ask()
    realm = attach_realm(RealmApp(tmp_path, roster, clock))
    assert not realm.enabled and rig.states()[0] == "unknown"
    run_loop(realm.close())


# -- the schema: 0015 -------------------------------------------------------------------------

def _v14(tmp_path, monkeypatch):
    files = db._migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n < 15])
        conn = db.open_db(tmp_path / "v14.db")
    with conn:
        for identifier in range(1, 7):
            _insert(conn, "game_event", dict(id=identifier, ts=1000.0, type="guard", payload="{}"))
            _insert(conn, "chat_line", dict(id=identifier, ts=1000.0, channel="whisper", from_guid=20,
                    from_name="Example", to_guid=10, text="yes", origin="mind"))
            _insert(conn, "realm_source", _source(identifier))
        _insert(conn, "realm_kick_episode", _episode(episode_id="kick:" + f"{1:064x}", source=1, guid=101))
        _insert(conn, "realm_kick_episode", _episode("delivered", episode_id="kick:" + f"{2:064x}", source=2,
                guid=102, answer="yes", answer_source=3, answered_ms=1000005, consent_state="pending_native"))
        _insert(conn, "realm_kick_episode", _episode("delivered", episode_id="kick:" + f"{4:064x}", source=4,
                guid=103, ask_id="ask_2", lead=21, answer="no", answer_source=5, answered_ms=1000005,
                consent_state="declined"))
    return conn, files


def _others(conn):
    return [tuple(r) for r in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                           "WHERE tbl_name<>'realm_kick_episode' ORDER BY type,name")]


def test_0015_upgrade_keeps_episodes_and_allows_one_admission_per_yes(tmp_path, monkeypatch):
    conn, files = _v14(tmp_path, monkeypatch)
    # This historical upgrade stops at v15 even when newer migrations exist.
    monkeypatch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n <= 15])
    try:
        others = _others(conn)
        before = [tuple(r) for r in conn.execute("SELECT * FROM realm_kick_episode ORDER BY episode_id")]
        keys = [tuple(r) for r in conn.execute("PRAGMA foreign_key_list(realm_kick_episode)")]
        assert db.migrate(conn) == 15
        assert db.migrate(conn) == 15
        after = [tuple(r) for r in conn.execute("SELECT * FROM realm_kick_episode ORDER BY episode_id")]
        assert [row[:27] for row in after] == before  # Every 0011 column, unchanged.
        assert [row[27:] for row in after] == [("none", None, None, None)] * 3
        assert _others(conn) == others
        assert [tuple(r) for r in conn.execute("PRAGMA foreign_key_list(realm_kick_episode)")] == keys
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='realm_kick_episode_v14'").fetchone()[0] == 0
        # A yes takes its one command and keeps its outcome; nothing else may.
        with conn:
            conn.execute("UPDATE realm_kick_episode SET admission_command_id='guild_readmit_1',"
                         "admission_state='prepared' WHERE guid=102")
            conn.execute("UPDATE realm_kick_episode SET admission_state='dispatched',admission_dispatched_ms=5 "
                         "WHERE guid=102")
            conn.execute("UPDATE realm_kick_episode SET admission_state='refused',admission_reason='not_kicked',"
                         "admission_settled_ms=6 WHERE guid=102")
        for guid, change in ((101, "admission_command_id='guild_readmit_2',admission_state='prepared'"),
                             (103, "admission_command_id='guild_readmit_2',admission_state='prepared'"),
                             (102, "admission_reason='not kicked'"),
                             (102, "admission_settled_ms=4"),
                             (102, "admission_state='admitted'"),
                             (102, "admission_command_id=NULL"),
                             (102, "admission_state='none'")):
            with pytest.raises(sqlite3.IntegrityError), conn:
                conn.execute(f"UPDATE realm_kick_episode SET {change} WHERE guid=?", (guid,))
        # The one open question per lead still holds.
        with pytest.raises(sqlite3.IntegrityError), conn:
            _insert(conn, "realm_kick_episode", _episode("reserved", episode_id="kick:" + f"{6:064x}", source=6,
                    guid=104, ask_id="ask_3"))
            _insert(conn, "realm_kick_episode", _episode("reserved", episode_id="kick:" + f"{7:064x}", source=5,
                    guid=105, ask_id="ask_4"))
    finally:
        conn.close()


def test_failed_0015_rolls_back_the_rebuild(tmp_path, monkeypatch):
    conn, files = _v14(tmp_path, monkeypatch)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        schema = [tuple(r) for r in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY 1,2")]
        rows = [tuple(r) for r in conn.execute("SELECT * FROM realm_kick_episode ORDER BY episode_id")]
        migration = next(path for number, path in files if number == 15)
        broken = tmp_path / migration.name
        broken.write_text(migration.read_text(encoding="utf-8") + "\nCREATE TABLE realm_source(x);",
                          encoding="utf-8")
        monkeypatch.setattr(db, "_migration_files", lambda: [(n, p) for n, p in files if n < 15] + [(15, broken)])
        with pytest.raises(sqlite3.OperationalError):
            db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version < 15
        assert [tuple(r) for r in conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY 1,2")] == schema
        assert [tuple(r) for r in conn.execute("SELECT * FROM realm_kick_episode ORDER BY episode_id")] == rows
    finally:
        conn.close()


# -- the service: the real app, bridge reader, pipeline and Store --------------------------------

def _lines(native):
    return [json.loads(line) for writer in native.writers for line in writer.sent]


def _feed_ack(native, command_id, ok=True, error=""):
    native.reader.feed_data(json.dumps(dict(v=1, type="ack", ts=int(native.clock.now() * 1000), id=command_id,
                                            ok=ok, error=error)).encode("utf-8") + b"\n")


def _feed_job(native, body, value, *, guid=0):
    native.reader.feed_data(json.dumps(dict(v=1, type="realm_job", ts=int(native.clock.now() * 1000),
                                            id=body["id"], kind=body["type"], state="done", reason="",
                                            guid=guid, value=value)).encode("utf-8") + b"\n")


async def _acker(native, readmit, say=None):
    """Acks every say at once (with `say(body)`'s (ok, error) when given), each
    guild_readmit with `readmit` (ok, error), the actor set sent at each hello, and what the
    calendar desk sends on a raid day: each post and signup with its job (a post's value is its
    new event id), each update by its ack alone."""
    seen, events = 0, 9000
    while True:
        lines = _lines(native)
        for body in lines[seen:]:
            kind = body.get("type")
            if kind == "say":
                _feed_ack(native, body["id"], *(say(body) if say is not None else ()))
            elif kind == "guild_readmit" and readmit is not None:
                _feed_ack(native, body["id"], *readmit)
            elif kind in ("realm_actors", "calendar_update"):
                _feed_ack(native, body["id"])
            elif kind == "calendar_post":
                events += 1
                _feed_ack(native, body["id"])
                _feed_job(native, body, events, guid=body["creator_guid"])
            elif kind == "calendar_signup":
                _feed_ack(native, body["id"])
                _feed_job(native, body, body["event_id"], guid=body["guid"])
        seen = len(lines)
        await asyncio.sleep(0)


def _episodes(native):
    with native.app.store.transaction():
        return [dict(r) for r in native.app.store.conn.execute("SELECT * FROM realm_kick_episode ORDER BY kick_ms")]


def _readmits(native):
    return [body for body in _lines(native) if body.get("type") == "guild_readmit"]


def _guard_says(native, kind):
    with native.app.store.transaction():
        return [tuple(r) for r in native.app.store.conn.execute(
            "SELECT guid,channel,to_guid,dropped FROM utterance WHERE why='say_as' AND kind=? ORDER BY id", (kind,))]


def _says(native, *, guid=LEAD):
    return [body for body in _lines(native) if body.get("type") == "say" and body.get("guid") == guid]


async def _presence(native, name, guid=HUMAN):
    await native.deliver(dict(v=1, type=name, ts=int(native.clock.now() * 1000), guid=guid, human=guid == HUMAN))


@asynccontextmanager
async def _guild(tmp_path, clock, monkeypatch, *, readmit=(True, ""), enabled=True, say=None):
    async with application(tmp_path, clock, monkeypatch, enabled=enabled) as native:
        native.clock = clock
        app = native.app
        raid_lead(app.store)
        app.personas.invalidate()
        at = int(clock.now() * 1000)
        await native.hello(human=HUMAN, ts=at)
        await native.deliver(dict(v=1, type="login", ts=at, guid=LEAD, human=False))
        app._model_ready = True  # The live model is there (FakeLLM lists none): the guild may speak.
        acks = asyncio.create_task(_acker(native, readmit, say))
        try:
            yield native
        finally:
            acks.cancel()


async def _kick(native, *, guid=TARGET, rank=4, ts=None):
    body = dict(v=1, type="guard", ts=ts or int(native.clock.now() * 1000) - 60_000, guid=guid,
                action="his_kick", rank=rank, reason="")
    await native.deliver(body)
    return body


async def _ask(native):
    await native.app.realm.tick()
    await native.clock.advance(30)
    await reached(lambda: not native.app.realm._asking)


async def _say(native, text, *, to_guid=LEAD):
    await native.deliver(dict(v=1, type="chat", ts=int(native.clock.now() * 1000), channel="whisper",
                              from_guid=HUMAN, from_name="Testmaster", to_guid=to_guid, text=text, map=0, zone=0))


async def _settle(native):
    for _ in range(3):
        await native.clock.advance(30)
        for _ in range(50):
            await asyncio.sleep(0)
    await reached(lambda: not native.app.realm._children)


def test_service_his_kick_question_yes_and_one_readmission(tmp_path, clock, monkeypatch):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch) as native:
            kick = await _kick(native)
            with native.app.store.transaction():
                assert [tuple(r) for r in native.app.store.conn.execute("SELECT guid,rank FROM realm_kick")] == [(TARGET, 4)]
            await _ask(native)
            (episode,) = _episodes(native)
            assert (episode["ask_state"], episode["lead"], episode["kick_ms"]) == ("delivered", LEAD, kick["ts"])
            asked = [b for b in _lines(native) if b.get("type") == "say" and b.get("guid") == LEAD]
            assert len(asked) == 1 and asked[0]["channel"] == "whisper" and asked[0]["to_guid"] == HUMAN
            # Exactly the question, unstyled, and the row keeps the hash of the text that went out.
            assert asked[0]["text"] == QUESTION.format(name="Coros") == "saw Coros got kicked, want them back? (yes/no)"
            assert episode["ask_text_sha256"] == hashlib.sha256(asked[0]["text"].encode("utf-8")).hexdigest()
            # The same kick again, and another tick: nothing more is asked.
            await native.deliver(kick)
            await _ask(native)
            assert len(_episodes(native)) == 1 and _guard_says(native, "whisper") == [(LEAD, "whisper", HUMAN, None)]
            await _say(native, "yes")
            await reached(lambda: _episodes(native)[0]["admission_state"] == "admitted")
            (readmit,) = _readmits(native)
            assert {k: readmit[k] for k in ("guid", "kick_ts", "rank")} == dict(guid=TARGET, kick_ts=kick["ts"], rank=4)
            row = _episodes(native)[0]
            assert (row["answer"], row["consent_state"], row["admission_command_id"]) == (
                "yes", "pending_native", readmit["id"])
            with native.app.store.transaction():
                receipt = native.app.store.conn.execute(
                    "SELECT kind,stage,ok,reason FROM realm_receipt WHERE command_id=?", (readmit["id"],)).fetchone()
            assert tuple(receipt) == ("guild_readmit", "ack", 1, "")
            await _say(native, "yes")  # A second yes: never a second readmission.
            await _settle(native)
            assert len(_readmits(native)) == 1 and _guard_says(native, "reply_human") == []
    run_native(scenario())


@pytest.mark.parametrize("reason", ["not_kicked", "already_member"])
def test_service_refused_readmission_tells_him_to_invite_them(tmp_path, clock, monkeypatch, reason):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch, readmit=(False, reason)) as native:
            await _kick(native)
            await _ask(native)
            await _say(native, "Yes")
            await reached(lambda: _episodes(native)[0]["admission_state"] == "refused")
            await _settle(native)
            assert len(_readmits(native)) == 1
            assert (_episodes(native)[0]["admission_reason"]) == reason
            # One plain whisper from the lead; it promised nothing it could not do.
            assert _guard_says(native, "reply_human") == [(LEAD, "whisper", HUMAN, None)]
    run_native(scenario())


def test_service_an_unknown_kick_rank_sends_nothing_and_says_so(tmp_path, clock, monkeypatch):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch) as native:
            await _kick(native, rank=0)
            await _ask(native)
            await _say(native, "yes")
            await _settle(native)
            assert _readmits(native) == []
            assert _episodes(native)[0]["admission_reason"] == "rank_unknown"
            assert _guard_says(native, "reply_human") == [(LEAD, "whisper", HUMAN, None)]
    run_native(scenario())


def test_service_his_no_sends_nothing(tmp_path, clock, monkeypatch):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch) as native:
            await _kick(native)
            await _ask(native)
            await _say(native, "no")
            await _say(native, "yes")
            await _settle(native)
            row = _episodes(native)[0]
            assert (row["answer"], row["consent_state"], row["admission_state"]) == ("no", "declined", "none")
            assert _readmits(native) == [] and _guard_says(native, "reply_human") == []
    run_native(scenario())


def test_service_disabled_records_only_the_kick(tmp_path, clock, monkeypatch):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch, enabled=False) as native:
            await _kick(native)
            await _ask(native)
            await _say(native, "yes")
            await _settle(native)
            store = native.app.store
            with store.transaction():
                assert [tuple(r) for r in store.conn.execute("SELECT guid FROM realm_kick")] == [(TARGET,)]
                assert count(store, "realm_source") == 0 and count(store, "realm_kick_episode") == 0
            assert _readmits(native) == [] and _guard_says(native, "whisper") == []
    run_native(scenario())


def test_service_refuses_an_unowned_readmission(tmp_path, clock, monkeypatch):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch) as native:
            realm = native.app.realm
            cmd = protocol.GuildReadmit("guild_readmit_forged", TARGET, KICK_MS, 4)
            with pytest.raises(GuardRefused, match="not_owned"):
                realm.on_send(cmd)
            with pytest.raises(GuardRefused, match="not_owned"):
                await realm.ports.send(cmd)
            assert _readmits(native) == [] and not realm._pending
    run_native(scenario())


@pytest.mark.parametrize("word", ["yes", "no"])
def test_service_his_answer_to_an_unrelated_lead_whisper_is_no_answer(tmp_path, clock, monkeypatch, word):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch) as native:
            await _kick(native)
            await _ask(native)
            await native.clock.advance(120)  # Past the lead's own reply interval.
            before = len(_says(native))
            other = asyncio.create_task(native.app.engine.say_as(
                LEAD, "whisper", "u coming to icc tonight?", kind=TurnKind.WHISPER, to_guid=HUMAN))
            await native.clock.advance(60)
            assert await other
            # One ordinary whisper went out; its words are styled, so typos may change them.
            assert len(_says(native)) == before + 1
            assert "want them back" not in _says(native)[-1]["text"]
            await _say(native, word)  # His answer to that whisper, not to the question.
            await _settle(native)
            (row,) = _episodes(native)
            assert (row["ask_state"], row["answer"], row["consent_state"], row["admission_state"]) == (
                "delivered", None, "none", "none")
            assert _readmits(native) == [] and _guard_says(native, "reply_human") == []
    run_native(scenario())


def test_service_a_question_the_model_latch_holds_back_is_refused_and_asked_again(tmp_path, clock, monkeypatch):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch) as native:
            native.app._model_ready = False  # Ollama briefly away: app._send would refuse the say.
            await _kick(native)
            await _ask(native)
            (row,) = _episodes(native)
            assert (row["ask_state"], row["dispatched_ms"]) == ("refused", None)  # Never on the wire.
            assert _says(native) == [] and _guard_says(native, "whisper") == [(LEAD, "whisper", HUMAN,
                                                                                "transport_refused")]
            native.app._model_ready = True
            await native.clock.advance(RETRY_MS / 1000)
            await _ask(native)
            (row,) = _episodes(native)
            assert row["ask_state"] == "delivered" and len(_says(native)) == 1
    run_native(scenario())


def test_service_he_offline_at_the_tick_is_asked_once_he_is_back(tmp_path, clock, monkeypatch):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch) as native:
            await _kick(native)
            await _presence(native, "logout")
            await _ask(native)
            (row,) = _episodes(native)
            assert (row["ask_state"], row["lead"]) == ("unasked", None) and _says(native) == []
            await _presence(native, "login")
            await _ask(native)
            (row,) = _episodes(native)
            assert row["ask_state"] == "delivered" and len(_says(native)) == 1
    run_native(scenario())


def _late_logout(native, monkeypatch):
    """His logout lands after the pipeline's last guard passed a hooked line, right before
    its write (the send lock hands the line to its own task): only the sender's barrier can
    still see it. Once."""
    engine, world, fired = native.app.engine, native.app.world, []
    blocked = engine._blocked

    def late(turn, meta, now, **kw):
        why = blocked(turn, meta, now, **kw)
        if why is None and kw.get("deadline") is False and meta.before_transport is not None and not fired:
            fired.append(turn.kind)
            world.human_online = False
        return why
    monkeypatch.setattr(engine, "_blocked", late)
    return fired


async def _advance_until(native, predicate, limit=60):
    for _ in range(limit):
        if predicate():
            return
        await native.clock.advance(1)
        for _ in range(20):
            await asyncio.sleep(0)
    raise AssertionError("the fake clock never reached it")


@pytest.mark.parametrize("when,dropped", [("typing", "human_offline"), ("write", "transport_refused")])
def test_service_his_logout_between_the_reservation_and_the_send_refuses_the_question(tmp_path, clock,
                                                                                    monkeypatch, when, dropped):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch) as native:
            await _kick(native)
            fired = _late_logout(native, monkeypatch) if when == "write" else None
            await native.app.realm.tick()  # Reserved; the lead's typing comes before the send.
            (row,) = _episodes(native)
            assert row["ask_state"] == "reserved" and native.app.realm._asking is not None
            if when == "typing":  # The pipeline's own guard sees this one.
                await _presence(native, "logout")
            await native.clock.advance(30)
            await reached(lambda: not native.app.realm._asking)
            assert fired is None or fired == [TurnKind.WHISPER]
            (row,) = _episodes(native)
            assert (row["ask_state"], row["dispatched_ms"]) == ("refused", None) and _says(native) == []
            assert _guard_says(native, "whisper") == [(LEAD, "whisper", HUMAN, dropped)]
            await _presence(native, "login")
            await native.clock.advance(RETRY_MS / 1000)
            await _ask(native)
            (row,) = _episodes(native)
            assert row["ask_state"] == "delivered" and len(_says(native)) == 1
    run_native(scenario())


def _fallbacks(native):
    return [body for body in _says(native) if body["text"] == FALLBACK.format(name="Coros")]


async def _refused_yes(native):
    """His yes, and native's refusal of the readmission: the lead's fallback is due."""
    await _kick(native)
    await _ask(native)
    await _say(native, "yes")
    await reached(lambda: _episodes(native)[0]["admission_state"] == "refused")


@pytest.mark.parametrize("held", ["latch", "logout"])
def test_service_a_held_back_fallback_is_tried_again(tmp_path, clock, monkeypatch, held):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch, readmit=(False, "not_kicked")) as native:
            await _refused_yes(native)
            if held == "latch":
                native.app._model_ready = False
            else:
                _late_logout(native, monkeypatch)
            await _advance_until(native, lambda: _guard_says(native, "reply_human"))
            assert _guard_says(native, "reply_human") == [(LEAD, "whisper", HUMAN, "transport_refused")]
            assert _fallbacks(native) == []
            if held == "latch":
                native.app._model_ready = True
            else:
                await _presence(native, "login")
            await _settle(native)
            # Held back before the wire, so tried again: on the wire exactly once.
            assert len(_fallbacks(native)) == 1
            assert _guard_says(native, "reply_human") == [(LEAD, "whisper", HUMAN, "transport_refused"),
                                                          (LEAD, "whisper", HUMAN, None)]
    run_native(scenario())


def test_service_a_fallback_held_back_on_every_try_never_goes_out(tmp_path, clock, monkeypatch):
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch, readmit=(False, "not_kicked")) as native:
            await _refused_yes(native)
            native.app._model_ready = False
            for _ in range(4):
                await _settle(native)
            assert _fallbacks(native) == []
            assert _guard_says(native, "reply_human") == [(LEAD, "whisper", HUMAN, "transport_refused")] * 3
            native.app._model_ready = True
            await _settle(native)
            assert _fallbacks(native) == []
    run_native(scenario())


def test_service_a_fallback_that_reached_the_wire_is_never_sent_again(tmp_path, clock, monkeypatch):
    def say(body):  # Native refuses the fallback whisper after it was written: it is not retried.
        return (False, "target offline") if body["text"] == FALLBACK.format(name="Coros") else (True, "")

    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch, readmit=(False, "not_kicked"), say=say) as native:
            await _refused_yes(native)
            for _ in range(4):
                await _settle(native)
            assert len(_fallbacks(native)) == 1
            assert _guard_says(native, "reply_human") == [(LEAD, "whisper", HUMAN, "target offline")]
    run_native(scenario())


def test_service_a_spoken_yes_never_answers(tmp_path, clock, monkeypatch):
    """Push-to-talk: his transcript is injected into the dispatcher without the native wire
    origin an answer is bound to, so it never counts, and (stored as a whisper between them in
    a private room with the lead) it also unties any typed answer after it. Fail closed."""
    async def scenario():
        async with _guild(tmp_path, clock, monkeypatch) as native:
            await _kick(native)
            await _ask(native)
            at = int(native.clock.now() * 1000)
            native.app.bridge.inject(Chat(at, "whisper", HUMAN, "Testmaster", LEAD, "yes", 0, 0))
            await native.drain()
            with native.app.store.transaction():
                spoken = native.app.store.conn.execute(
                    "SELECT channel,from_guid,to_guid,text,origin FROM chat_line WHERE from_guid=? ORDER BY id DESC",
                    (HUMAN,)).fetchone()
            assert tuple(spoken) == ("whisper", HUMAN, LEAD, "yes", "human")
            await _settle(native)
            assert _episodes(native)[0]["answer"] is None and _readmits(native) == []
            await _say(native, "yes")  # Typed after it: the spoken line lies between.
            await _settle(native)
            (row,) = _episodes(native)
            assert (row["ask_state"], row["answer"], row["consent_state"], row["admission_state"]) == (
                "delivered", None, "none", "none")
            assert _readmits(native) == [] and count(native.app.store, "realm_source", "event_type='chat'") == 0
    run_native(scenario())


# -- the speech hooks: one exact typed line, and nothing changes without them ----------------

LONG = ("we should clear the trash before the boss and then wait for everyone to drink up "
        "before the pull so nobody dies early again tonight")


def _speech_rig(store, tmp_path, **kw):
    return make_rig(store, engine_clock(), tmp_path, **kw)


async def _settled(r, coro, seconds=60):
    task = asyncio.create_task(coro)
    await r.advance(seconds)
    return await task


def test_untagged_speech_output_is_unchanged(store, tmp_path):
    """The exact wire commands and rows of untagged say_as and speak turns. The same values
    were captured from the code before the hooks existed (225c1c7), byte for byte."""
    r = _speech_rig(store, tmp_path)

    async def go():
        return [
            await _settled(r, r.engine.say_as(101, "guild", "fair enough, pulling in five", kind=TurnKind.ASIDE)),
            await _settled(r, r.engine.say_as(102, "whisper", "saw Coros got kicked, want them back? (yes/no)",
                                              kind=TurnKind.WHISPER, to_guid=RIG_HUMAN)),
            await _settled(r, r.engine.say_as(103, "guild", LONG, kind=TurnKind.ASIDE)),
            await _settled(r, r.engine.speak(Turn(kind=TurnKind.ASIDE, speaker=104, channel="guild"))),
        ]
    assert run_loop(go()) == [True, True, True, ["fair enough"]]
    t = 1793304000.0
    assert [(at, c.id, c.guid, c.channel, c.text, c.to_guid) for at, c in r.sent.timed_says()] == [
        (t + 10, "", 101, "guild", "fair enough, pulling in five", 0),
        (t + 73, "", 102, "whisper", "saw coros got kicked, want them back? (yes/no) :)", 1),
        (t + 133, "", 103, "guild", "we should clear the trash befoe the boss", 0),
        (t + 148, "", 103, "guild", "and then wait for everyone to drink up before the pull", 0),
        (t + 185, "", 104, "guild", "fair enough", 0)]
    everyone = "[101, 102, 103, 104, 105]"
    assert [tuple(row) for row in store.conn.execute(
        "SELECT id,ts,channel,from_guid,from_name,to_guid,text,map,zone,origin,audience FROM chat_line "
        "ORDER BY id")] == [
        (1, t, "guild", 101, "Arkon", 0, "fair enough, pulling in five", 0, 10, "mind", everyone),
        (2, t, "whisper", 102, "Belra", 1, "saw coros got kicked, want them back? (yes/no) :)", 0, 10, "mind",
         "[102]"),
        (3, t, "guild", 103, "Coros", 0, "we should clear the trash befoe the boss", 0, 10, "mind", everyone),
        (4, t, "guild", 103, "Coros", 0, "and then wait for everyone to drink up before the pull", 0, 10, "mind",
         everyone),
        (5, t, "guild", 104, "Dunna", 0, "fair enough", 0, 10, "mind", everyone)]
    t = int(t)
    assert [tuple(row) for row in store.conn.execute("SELECT * FROM utterance ORDER BY id")] == [
        (1, t + 10, 101, "guild", "aside", "fair enough, pulling in five", "[]", None, None, None, "say_as",
         None, None, 0, "t1"),
        (2, t + 73, 102, "whisper", "whisper", "saw coros got kicked, want them back? (yes/no) :)", "[]", None,
         None, None, "say_as", None, None, 1, "t2"),
        (3, t + 133, 103, "guild", "aside", "we should clear the trash befoe the boss", "[]", None, None, None,
         "say_as", None, None, 0, "t3"),
        (4, t + 148, 103, "guild", "aside", "and then wait for everyone to drink up before the pull", "[]", None,
         None, None, "say_as", None, None, 0, "t3"),
        (5, t + 185, 104, "guild", "aside", "fair enough", "[]", None, None, None, "speak", None, 0.0, 0, "t4")]


def _whisper(r, text="want them back?", seconds=60, **hooks):
    return run_loop(_settled(r, r.engine.say_as(102, "whisper", text, kind=TurnKind.WHISPER,
                                                to_guid=RIG_HUMAN, **hooks), seconds))


def test_a_barrier_refusal_sends_nothing(store, tmp_path):
    r = _speech_rig(store, tmp_path)
    calls, recorded = [], []

    def refuse():
        calls.append("barrier")
        return False
    assert _whisper(r, before_transport=refuse, on_recorded=lambda *a: recorded.append(a)) is False
    assert calls == ["barrier"] and recorded == []
    assert r.sent.log == [] and count(store, "chat_line") == 0
    assert r.utterances()[-1]["dropped"] == "transport_refused"


def test_a_barrier_turn_is_one_typed_line_recorded_with_its_ack(store, tmp_path):
    r = _speech_rig(store, tmp_path)
    run_loop(_settled(r, r.engine.say_as(103, "whisper", LONG, kind=TurnKind.WHISPER, to_guid=RIG_HUMAN)))
    assert len(r.sent.says()) == 2  # Untagged, this text takes two lines.
    r.sent.log.clear()
    run_loop(r.advance(120))  # Past the member's own reply interval.
    calls, recorded = [], []

    def admit():
        calls.append(len(r.sent.log))
        return True
    said = run_loop(_settled(r, r.engine.say_as(103, "whisper", LONG, kind=TurnKind.WHISPER, to_guid=RIG_HUMAN,
                                                before_transport=admit,
                                                on_recorded=lambda line, ack: recorded.append((line, ack)))))
    assert said is True and calls == [0]  # Right before the one Say, and only once.
    (say,) = r.sent.says()
    assert say.text == LONG  # As given: no typing style.
    ((line, ack),) = recorded
    assert type(ack) is Ack and ack.ok is True and ack.id == say.id
    row = store.conn.execute("SELECT channel,from_guid,to_guid,text,origin FROM chat_line WHERE id=?",
                             (line,)).fetchone()
    assert tuple(row) == ("whisper", 103, RIG_HUMAN, say.text, "mind")


def test_a_barrier_turn_is_sent_exactly_as_given(store, tmp_path):
    """Untagged, this member types the question as "saw coros got kicked, want them back?
    (yes/no) :)" (above). A sender's barrier vouches for its exact text: no typos,
    abbreviations, case changes or emoticon."""
    r = _speech_rig(store, tmp_path)
    text = QUESTION.format(name="Coros")
    assert _whisper(r, text, before_transport=lambda: True) is True
    (say,) = r.sent.says()
    assert say.text == text
    assert store.conn.execute("SELECT text FROM chat_line").fetchone()[0] == text


def test_a_barrier_turn_is_never_voiced(store, tmp_path):
    r = _speech_rig(store, tmp_path)
    r.engine.comms = SimpleNamespace(place=Mock(side_effect=AssertionError("placed")),
                                     voiced=Mock(side_effect=AssertionError("voiced")),
                                     unheard=Mock(return_value=False), available=Mock(return_value=True))
    assert _whisper(r, before_transport=lambda: True) is True
    assert [c.channel for c in r.sent.says()] == ["whisper"]
    r.engine.comms.place.assert_not_called()
    r.engine.comms.voiced.assert_not_called()


@pytest.mark.parametrize("outcome", ["refused", "timeout"])
def test_the_recorded_hook_needs_an_acked_line(store, tmp_path, outcome):
    def ack(cmd):
        if outcome == "timeout":
            raise AckTimeout("no ack")
        return False, "offline"
    r = _speech_rig(store, tmp_path, ack=ack)
    calls, recorded = [], []
    assert _whisper(r, seconds=15, before_transport=lambda: calls.append(1) or True,
                    on_recorded=lambda *a: recorded.append(a)) is False
    assert calls == [1] and len(r.sent.says()) == 1
    if outcome == "timeout":  # Its echo records the line later, without an ack: no hook.
        (say,) = r.sent.says()
        assert r.engine.is_echo(Chat(0, "whisper", 102, "Belra", RIG_HUMAN, say.text, 0, 0, True), r.clock.now())
        assert count(store, "chat_line") == 1
    assert recorded == []


def test_failing_hooks_never_break_speech(store, tmp_path):
    r = _speech_rig(store, tmp_path)

    def broken(*args):
        raise RuntimeError("hook")
    assert _whisper(r, before_transport=broken) is False
    assert r.sent.log == [] and r.utterances()[-1]["dropped"] == "transport_refused"
    assert _whisper(r, before_transport=lambda: True, on_recorded=broken) is True
    assert len(r.sent.says()) == 1 and count(store, "chat_line") == 1


def test_ports_pass_the_hooks_only_when_given(tmp_path, store, clock):
    app = RealmApp(tmp_path, store, clock)
    app.engine = SimpleNamespace(say_as=AsyncMock(return_value=True))
    ports = LivePorts(app)
    ports.set_enabled(True)
    before, recorded = (lambda: True), (lambda line, ack: None)

    async def go():
        assert await ports.say(101, "guild", "line")
        assert await ports.say(102, "whisper", "q", to_guid=HUMAN, kind=TurnKind.WHISPER,
                               before_transport=before, on_recorded=recorded)
        await ports.close()
    run_loop(go())
    plain, hooked = app.engine.say_as.await_args_list
    assert plain.args == (101, "guild", "line")
    assert set(plain.kwargs) == {"kind", "to_guid", "still_valid"} and plain.kwargs["kind"] is TurnKind.ASIDE
    assert hooked.kwargs["before_transport"] is before and hooked.kwargs["on_recorded"] is recorded
    assert hooked.kwargs["kind"] is TurnKind.WHISPER and hooked.kwargs["to_guid"] == HUMAN


def test_service_on_a_raid_day_the_calendar_posts_and_the_guard_still_asks(tmp_path, monkeypatch):
    """With the Realm on and a standing raid lead, the calendar desk posts whatever is due on the
    clock's date (Tuesday: three raid nights and Thursday's meeting); the fake native acks each
    post and sends its job, so nothing waits on the Realm's send child."""
    tuesday = FakeClock(datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc))

    async def scenario():
        async with _guild(tmp_path, tuesday, monkeypatch) as native:
            for _ in range(3):
                await native.app.realm.tick()
                await tuesday.advance(30)
            await _settle(native)
            await native.drain()  # the posts' jobs: each binds its event id
            posts = [b for b in _lines(native) if b.get("type") == "calendar_post"]
            assert len(posts) == 4 and all(b["creator_guid"] == LEAD for b in posts)
            with native.app.store.transaction():
                rows = native.app.store.conn.execute(
                    "SELECT kind,state,event_id FROM realm_calendar ORDER BY at").fetchall()
            assert [(r[0], r[1]) for r in rows] == [("raid", "posted")] * 3 + [("meeting", "posted")]
            assert all(type(r[2]) is int for r in rows)
            await _kick(native)
            await _ask(native)
            assert _episodes(native)[0]["ask_state"] == "delivered"
    run_native(scenario())
