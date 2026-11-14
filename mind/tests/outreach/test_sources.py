"""Outreach rechecks full provenance, not just the top memory's scope label."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from nightly.conftest import h  # noqa: F401
from leeroy_mind.discord import store as discord
from leeroy_mind.outreach.sources import source_alive, pair_provenance, DAY
from leeroy_mind.outreach import store as items
from outreach.test_fill import fill, follow, line, plan


@pytest.mark.parametrize("channel,readers,allowed", [
    ("whisper", None, True), ("dm:101", [101], True),
    ("guild", [101, 102], True), ("discord:general", [101, 102], True),
    ("officer", [101, 102], False), ("discord:officer-chat", [101, 102], False),
    ("discord:unknown-room", [101, 102], False),
    ("gdm:1", [1, 101, 102], True), ("gdm:1", [101, 102], False),
    ("gdm:1", None, False)])
def test_followup_source_scope_is_checked_through_open_wrapper(h, channel, readers, allowed):
    root, mid = follow(h, channel=channel, readers=readers)
    with h.store.transaction():
        h.store.conn.execute("UPDATE memory SET scope='open',scope_with=0 WHERE id=?", (mid,))
    fill(h)
    assert bool(items.open_items(h.store, 101)) is allowed


@pytest.mark.parametrize("extra", ["rule:unproved", "memory:999999", "unknown:1", "chat:999999"])
def test_unknown_or_missing_provenance_never_authorizes_followup(h, extra):
    root, mid = follow(h)
    with h.store.transaction():
        h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?", (json.dumps([f"chat:{root}", extra]), mid))
    fill(h)
    assert items.open_items(h.store, 101) == []


def test_nested_private_memory_of_someone_else_cannot_widen_into_his_pair(h):
    root, mid = follow(h)
    private = line(h, guid=102, to=103, text="A private invented confidence.")
    inner = h.mem(102, "Their private confidence.", evidence=(f"chat:{private}",), scope="private", scope_with=103)
    with h.store.transaction():
        h.store.conn.execute("UPDATE memory SET scope='open',scope_with=0,evidence=? WHERE id=?",
            (json.dumps([f"chat:{root}", f"memory:{inner}"]), mid))
    fill(h)
    assert items.open_items(h.store, 101) == []


def test_cycle_fails_closed(h):
    root, mid = follow(h)
    with h.store.transaction():
        h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?", (json.dumps([f"chat:{root}", f"memory:{mid}"]), mid))
    fill(h)
    assert items.open_items(h.store, 101) == []


def test_public_plan_recalling_an_officer_memory_stays_restricted(h):
    officer = line(h, guid=1, channel="officer", to=0, readers=[101], text="Private officer business.")
    memory = h.mem(101, "Private officer business.", evidence=(f"chat:{officer}",), scope="private", scope_with=-1)
    _, source = plan(h, channel="guild", to=0, readers=[101])
    with h.store.transaction():
        h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE text=(SELECT text FROM chat_line WHERE id=?)",
                             (json.dumps([memory]), source))
    fill(h)
    assert items.open_items(h.store, 101) == []


def test_deleted_discord_source_invalidates_existing_item(h):
    plan(h, channel="dm:101", readers=[101])
    fill(h)
    item, = items.open_items(h.store, 101)
    assert source_alive(h.store, item, now=h.T, human=1)
    with h.store.transaction():
        h.store.conn.execute("UPDATE discord_message SET deleted=?", (h.T,))
    assert not source_alive(h.store, item, now=h.T, human=1)


def test_mismatched_discord_post_cannot_use_retained_chat_text(h):
    plan(h, channel="dm:101", readers=[101])
    fill(h)
    item, = items.open_items(h.store, 101)
    with h.store.transaction():
        h.store.conn.execute("UPDATE discord_message SET text='The plan changed.'")
    assert not source_alive(h.store, item, now=h.T, human=1)


def test_removed_group_reader_invalidates_item_without_using_current_membership(h):
    _, source = plan(h, channel="gdm:1", readers=[1, 101, 102])
    fill(h)
    item, = items.open_items(h.store, 101)
    assert source_alive(h.store, item, now=h.T, human=1)
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience='[101,102]' WHERE id=?", (source,))
    assert not source_alive(h.store, item, now=h.T, human=1)


def test_missing_sent_utterance_cannot_supply_a_plan(h):
    _, source = plan(h)
    with h.store.transaction():
        h.store.conn.execute("UPDATE utterance SET dropped='failed'")
    assert not pair_provenance(h.store, 101, [f"chat:{source}"], human=1)
    fill(h)
    assert items.open_items(h.store, 101) == []


def test_wrong_owner_and_audience_kind_mismatch_fail_closed(h):
    plan(h); fill(h)
    item, = items.open_items(h.store, 101)
    assert not source_alive(h.store, replace(item, guid=102), now=h.T, human=1)
    assert not source_alive(h.store, replace(item, audience="guild"), now=h.T, human=1)
    assert not source_alive(h.store, replace(item, kind="unknown"), now=h.T, human=1)


def test_dm_nudge_requires_latest_unanswered_message_even_if_his_reply_was_deleted(h):
    mid = discord.post(h.store, channel="dm:101", author=101, origin="mind", text="An invented hello.", now=h.T - 100)
    item = SimpleNamespace(guid=101, kind="dm_nudge", ref=f"dm_nudge:{mid}", audience="pair")
    assert source_alive(h.store, item, now=h.T, human=1)
    reply = discord.post(h.store, channel="dm:101", author=1, origin="human", text="A reply.", now=h.T - 50)
    with h.store.transaction():
        h.store.conn.execute("UPDATE discord_message SET deleted=? WHERE id=?", (h.T, reply))
    assert not source_alive(h.store, item, now=h.T, human=1)
    assert not source_alive(h.store, item, now=h.T + 3 * DAY, human=1)


def test_dm_nudge_does_not_ignore_a_later_post_with_an_older_timestamp(h):
    mid = discord.post(h.store, channel="dm:101", author=101, origin="mind", text="An invented hello.", now=h.T - 100)
    item = SimpleNamespace(guid=101, kind="dm_nudge", ref=f"dm_nudge:{mid}", audience="pair")
    discord.post(h.store, channel="dm:101", author=1, origin="human", text="A reply.", now=h.T - 200)
    assert not source_alive(h.store, item, now=h.T, human=1)


@pytest.mark.parametrize("medium", ["whisper", "dm:101"])
def test_miss_you_receipt_excludes_only_its_exact_delivery(h, medium):
    p = h.woman(101, "drawn", age=30)
    fill(h, p=p)
    item, = items.open_items(h.store, 101)
    delivered = line(h, channel=medium, at=h.T)
    assert not source_alive(h.store, item, now=h.T, human=1, persona=p)
    assert source_alive(h.store, item, now=h.T, human=1, persona=p, delivered_line=delivered)
    other = "whisper" if medium == "dm:101" else "dm:101"
    line(h, channel=other, guid=1, to=101, at=h.T)
    assert not source_alive(h.store, item, now=h.T, human=1, persona=p, delivered_line=delivered)
