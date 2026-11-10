"""Saved Discord membership before the first bridge cast; no invented game presence."""
import dataclasses

import pytest

from conftest import HUMAN, OFFICERS, TEST_CAST, seed_test_cast
from leeroy_mind.protocol import Cast
from leeroy_mind.talk.world import WorldView
from tests.discord.test_discord_talk import (answer, answers, clock, post, rig_for,
                                           speakers, talk_for)


def cold_world(store):
    world = WorldView(is_officer=lambda g: g in OFFICERS, discord_roster=store.cast)
    world.human_guid = HUMAN
    return world


def test_saved_roster_is_discord_only_and_respects_private_channels(store):
    seed_test_cast(store)
    store.upsert_cast([dataclasses.replace(m, online=True) for m in store.cast().values()], 0)
    world = cold_world(store)
    saved = store.cast()
    assert any(m.online for m in saved.values())
    assert not any(m.online for m in world.discord_members().values())
    assert store.cast() == saved  # sanitising the view never rewrites the roster
    assert world.audience("discord:general", from_guid=HUMAN) == set(TEST_CAST) | {HUMAN}
    assert world.audience("discord:officer-chat", from_guid=HUMAN) == set(OFFICERS) | {HUMAN}
    assert world.audience("discord:unknown", from_guid=HUMAN) == set()
    assert world.audience("dm:102", from_guid=HUMAN) == {HUMAN, 102}
    assert world.audience("guild", from_guid=HUMAN) == set()
    assert not world.members and not world.online and not world.cast_guids()
    assert not world.human_online and world.group is None


@pytest.mark.parametrize("remaining", [(), (102,)])
def test_first_authoritative_cast_permanently_replaces_saved_members(store, remaining):
    seed_test_cast(store)
    world = cold_world(store)
    saved = store.cast()
    assert 101 in world.discord_members()
    current = tuple(dataclasses.replace(saved[g], online=False) for g in remaining)
    world.apply(Cast(ts=0, members=current), 0)
    assert world.discord_members() == {m.guid: m for m in current}
    assert world.audience("discord:general", from_guid=HUMAN) == set(remaining) | {HUMAN}
    # The disk still holds the old roster, including after an empty bridge cast.
    assert 101 in store.cast()
    assert 101 not in world.discord_members()
    assert not world.online


def test_roster_source_failure_is_closed_and_logs_only_its_class(caplog):
    calls = []

    def broken():
        calls.append(1)
        raise RuntimeError("invented private roster detail")

    world = WorldView(is_officer=lambda _: True, discord_roster=broken)
    world.human_guid = HUMAN
    assert world.audience("discord:general", from_guid=HUMAN) == {HUMAN}
    assert world.audience("discord:officer-chat", from_guid=HUMAN) == {HUMAN}
    assert len(calls) == 2  # failed reads remain retryable rather than caching emptiness
    assert "RuntimeError" in caplog.text
    assert "invented private roster detail" not in caplog.text
    world.apply(Cast(ts=0, members=()), 0)
    caplog.clear()
    assert world.discord_members() == {}
    assert not caplog.records  # never consult the fallback after an authoritative cast
    assert len(calls) == 2


def test_cold_named_answer_keeps_name_protection_and_member_romance_screen(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path, reply="i have a crush on belra")
    rig.world = rig.engine.world = rig.engine.raid.world = cold_world(store)
    talk = talk_for(rig)
    mid = post(rig, "general", "@Arkon are you around?")
    assert speakers(talk, mid) == [[101]]
    assert rig.engine._names("discord:general")[102] == "Belra"
    assert "Belra" in rig.engine._protect("discord:general")
    assert "Belra" not in rig.engine._protect("guild")
    answer(rig, talk, mid)
    assert not answers(rig, "general", mid)
    assert not rig.sent.log and not rig.world.online
    rig.close()


def test_cold_channel_planning_reads_the_saved_roster_once(store, clock, tmp_path):
    rig = rig_for(store, clock, tmp_path)
    calls = []

    def roster():
        calls.append(1)
        return store.cast()

    world = WorldView(is_officer=lambda g: g in OFFICERS, discord_roster=roster)
    world.human_guid = HUMAN
    rig.world = rig.engine.world = rig.engine.raid.world = world
    talk = talk_for(rig)
    mid = post(rig, "general", "anyone around?")
    queries = []
    store.conn.set_trace_callback(queries.append)
    try:
        assert talk.plan(mid)
        assert len(calls) == 1
        assert sum("FROM cast_member" in q for q in queries) == 1
        assert talk.plan(mid)
        assert rig.engine._names("discord:general")[102] == "Belra"
        assert len(calls) == 1
        world.apply(Cast(ts=0, members=()), 0)
        assert not talk.plan(mid) and not world.discord_members()
        assert len(calls) == 1
    finally:
        store.conn.set_trace_callback(None)
        rig.close()
