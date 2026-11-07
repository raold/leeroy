"""Situational and guild photos in mind.db (P4 Task 4). Every person here is invented."""
import sqlite3
from dataclasses import replace

import pytest

from leeroy_mind.personas.schema import Appearance
from leeroy_mind.selfies.snaps import (SnapError, counts, expire, guild_photo_since, last_sent,
                                       last_sent_to_him, looks_hash, mark_rendered, mark_sent,
                                       open_stock, plan_snap, planned, ready_for, ready_guild,
                                       sent_count, sweep)

from .fakes import LOOKS, expired_snap, planned_snap, ready_snap, sent_snap

DAY = 86400.0
GUILD = dict(kind="guild", source="raid", pick=None, looks=None)


def test_sweep_expires_passed_and_stale_photos(store):
    a = planned_snap(store, 101, expires=10.0)
    b = planned_snap(store, 102, pick=1)
    c = planned_snap(store, 103, looks="old")
    gone = sweep(store, now=10.0, pick_of={101: 1, 102: 2, 103: 1}.get,
                 looks_of=lambda g: "new" if g == 103 else LOOKS)
    assert {(r.id, r.ended) for r in gone} == {(a, "passed"), (b, "photo_changed"),
                                              (c, "looks_changed")}
    ended = dict(store.conn.execute("SELECT id, ended FROM selfie_snap").fetchall())
    assert ended == {a: "passed", b: "photo_changed", c: "looks_changed"}
    assert open_stock(store) == 0


def test_sent_needs_ready_and_records_the_line(store, data):
    s = ready_snap(store, data, 101)
    assert mark_sent(store, s, sender=101, channel="whisper", line_id=77, now=5.0) is True
    assert mark_sent(store, s, sender=101, channel="whisper", line_id=78, now=6.0) is False
    p = planned_snap(store, 102)
    assert mark_sent(store, p, sender=102, channel="whisper", line_id=79, now=6.0) is False
    assert last_sent(store, 101) == 5.0 and last_sent(store, 102) is None
    assert last_sent_to_him(store, 101, 0.0).id == s
    assert last_sent_to_him(store, 101, 6.0) is None
    assert sent_count(store, 5.0) == 1 and sent_count(store, 5.5) == 0


def test_a_member_and_ref_is_planned_once(store):
    planned_snap(store, 101, ref="beat:9")
    assert plan_snap(store, kind="situation", guid=101, source="life", ref="beat:9",
                     night="n", scene={}, audience="him", phases=("day",), not_before=0,
                     expires=5, now=1, pick=1, looks=LOOKS) is None
    planned_snap(store, 102, ref="beat:9")  # another member may share the ref


def test_ready_for_keeps_to_its_phases_and_window(store, data):
    a = ready_snap(store, data, 101, phases=("day",), not_before=10.0, expires=100.0)
    b = ready_snap(store, data, 101, phases=("day", "late"), not_before=0.0, expires=50.0)
    planned_snap(store, 101, phases=("day",))
    ready_snap(store, data, 102, phases=("day",))
    assert [r.id for r in ready_for(store, 101, now=20.0, phase="day")] == [b, a]
    assert [r.id for r in ready_for(store, 101, now=20.0, phase="late")] == [b]
    assert [r.id for r in ready_for(store, 101, now=5.0, phase="day")] == [b]
    assert ready_for(store, 101, now=50.0, phase="late") == []
    assert [r.id for r in ready_for(store, 101, now=50.0, phase="day")] == [a]
    assert ready_for(store, 101, now=100.0, phase="day") == []
    row = ready_for(store, 101, now=20.0, phase="late")[0]
    assert row.phases == ("day", "late") and row.scene == {"shows": "an invented scene"}


def test_a_guild_photo_has_members_and_comes_from_ready_guild(store, data):
    g = ready_snap(store, data, 0, ref="raid:4", members=[(101, 1), (102, 2)],
                   phases=("late",), **GUILD)
    assert ready_guild(store, now=5.0, phase="day") is None
    row = ready_guild(store, now=5.0, phase="late")
    assert row.id == g and row.members == ((101, 1), (102, 2)) and row.guid == 0
    assert guild_photo_since(store, 0.5) and not guild_photo_since(store, 1.5)
    assert planned(store, 5) == []


def test_planned_lists_guild_photos_first(store):
    a = planned_snap(store, 101)
    g = planned_snap(store, 0, ref="raid:1", members=[(1, 1)], **GUILD)
    b = planned_snap(store, 102)
    assert [r.id for r in planned(store, 10)] == [g, a, b]
    assert [r.id for r in planned(store, 2)] == [g, a]


def test_mark_rendered_and_expire_need_the_right_state(store):
    s = planned_snap(store, 101)
    mark_rendered(store, s, width=1, height=2, seed=3, tries=1, nsfw=0.1, prompt="p", now=2.0)
    with pytest.raises(SnapError):
        mark_rendered(store, s, width=1, height=2, seed=3, tries=1, nsfw=0.1, prompt="p", now=2.0)
    t = planned_snap(store, 102)
    with pytest.raises(SnapError):
        mark_rendered(store, t, width=1, height=2, seed=3, tries=1, nsfw=None, prompt="p", now=2.0)
    expire(store, s, "flagged", now=3.0)
    with pytest.raises(SnapError):
        expire(store, s, "passed", now=3.0)


def test_the_checks_refuse_a_guild_photo_with_a_guid_and_a_sent_row_without_its_line(store):
    with pytest.raises(sqlite3.IntegrityError):
        planned_snap(store, 101, members=[(1, 1)], **GUILD)
    store.conn.rollback()
    s = planned_snap(store, 101)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE selfie_snap SET status='sent', rendered=1, sent=1, sender=101 "
                           "WHERE id=?", (s,))
    store.conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):  # the window must have a length
        planned_snap(store, 102, not_before=5.0, expires=5.0)


def test_counts_read_only(store, data):
    ready_snap(store, data, 101)
    planned_snap(store, 102)
    sent_snap(store, data, 103)
    expired_snap(store, data, 104, expires=3 * DAY)
    expired_snap(store, data, 105, expires=1.0)
    store.conn.commit()
    path = store.conn.execute("PRAGMA database_list").fetchone()[2]
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert counts(ro, now=7 * DAY + 3.0) == {"planned": 1, "ready": 1, "sent_week": 1,
                                                    "expired_week": 1}
        assert counts(ro, now=11 * DAY)["expired_week"] == 0
    finally:
        ro.close()


def test_open_stock_counts_situations_only(store, data):
    planned_snap(store, 101)
    ready_snap(store, data, 101)
    sent_snap(store, data, 101)
    ready_snap(store, data, 0, ref="raid:2", members=[(1, 1)], **GUILD)
    assert open_stock(store) == 2 and open_stock(store, 101) == 2 and open_stock(store, 102) == 0


def test_looks_hash_follows_the_looks():
    a = Appearance(height_cm=170, build="average", weight_kg=65, hair_colour="brown",
                   hair_length="short", hair_style="plain", skin_tone="fair", eye_colour="green",
                   face=("freckles",), at_the_pc="a hoodie")
    assert looks_hash(a) == looks_hash(a) and len(looks_hash(a)) == 64
    assert looks_hash(a) != looks_hash(replace(a, hair_colour="red"))
