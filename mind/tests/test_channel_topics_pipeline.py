"""[server] channel_topics in the talk pipeline (discord/topics.py): a server conversation's turn
on a guild Discord channel is held to the channel's purpose by its hold line, leaves her unasked
topics out but in #irl, and its drafts are checked against the purpose; #general, DMs, group DMs
and every answer to him are as they were, and so is a WoW channel's turn under [talk] wow_topic.
Invented cast and lines only."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import HUMAN, run, seed_test_cast
from engine_rig import engine_clock, make_rig
from leeroy_mind.discord import topics
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.server.config import ServerConfig
from leeroy_mind.talk import wowtopic
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from sheets import give


@pytest.fixture
def clock():
    return engine_clock()


@pytest.fixture
def rig(store, clock, tmp_path):
    seed_test_cast(store)
    give(store, 103)  # GRUFF: brings up the allotment and night shifts at the depot
    r = make_rig(store, clock, tmp_path)
    serve(r, True)
    yield r
    r.close()


def serve(r, on):
    """The server's attachment as the pipeline reads it: only its config's switch."""
    r.engine.server_app = SimpleNamespace(server=SimpleNamespace(
        config=ServerConfig(enabled=True, channel_topics=on)))


def turn(channel, kind=TurnKind.CONTINUE, guid=103, **kw):
    """A server conversation's turn: a reply answers another member's message (101's)."""
    if kind is not TurnKind.OPEN:
        kw.setdefault("addressee", 101)
    return Turn(kind=kind, speaker=guid, channel=channel, instruction="Reply to the chosen message.",
                **kw)


def meta(**kw):
    return TurnMeta("t", why=kw.pop("why", "server:life"), discord=DiscordTarget(None, None), **kw)


def prompt(r, t, m=None):
    c = run(r.engine._compose(t, m or meta(), live=False))
    return "\n\n".join(x["content"] for x in c.messages)


HOOKS = "Unasked, you bring up: the allotment; night shifts at the depot."


def test_a_server_turn_gets_its_channels_hold_line(rig):
    for cid in ("food", "memes", "pets", "pics", "irl", "applications"):
        text = prompt(rig, turn(f"discord:{cid}"))
        assert topics.hold_text(cid) in text, cid
    opener = prompt(rig, turn("discord:food", kind=TurnKind.OPEN))
    assert topics.hold_text("food") in opener


def test_food_and_memes_leave_her_unasked_topics_out_but_irl_keeps_them(rig):
    assert HOOKS not in prompt(rig, turn("discord:food"))
    assert HOOKS not in prompt(rig, turn("discord:memes"))
    assert HOOKS in prompt(rig, turn("discord:irl"))


def test_general_dms_and_group_dms_are_as_they_were(rig):
    for channel in ("discord:general", "dm:103"):
        t = turn(channel, to_guid=HUMAN if channel.startswith("dm:") else 0)
        on = prompt(rig, t)
        serve(rig, False)
        off = prompt(rig, t)
        serve(rig, True)
        assert on == off, channel
        assert "Stay on what this channel is about" not in on


def test_an_answer_to_him_is_never_held(rig):
    his = turn("discord:memes", kind=TurnKind.REPLY_HUMAN, addressee=HUMAN)
    assert "Stay on what this channel is about" not in prompt(rig, his, meta(why="discord:reply",
                                                                             answers_human=True))
    addressed = turn("discord:food", addressee=HUMAN)
    assert topics.hold_text("food") not in prompt(rig, addressed)
    assert rig.engine._channel_topic(addressed, meta()) == ""


def test_a_template_turn_is_never_held(rig):
    t = replace(turn("discord:food"), template="gz")
    assert rig.engine._channel_topic(t, meta()) == ""


def test_off_holds_nothing(rig):
    serve(rig, False)
    text = prompt(rig, turn("discord:food"))
    assert "Stay on what this channel is about" not in text and HOOKS in text
    assert rig.engine._channel_topic(turn("discord:food"), meta()) == ""
    rig.engine.server_app = None  # the server not attached at all
    assert rig.engine._channel_topic(turn("discord:food"), meta()) == ""


def test_a_wow_channel_keeps_its_wow_topic_behaviour(rig):
    switch = lambda on: setattr(rig.engine, "cfg", replace(  # noqa: E731
        rig.engine.cfg, talk=replace(rig.engine.cfg.talk, wow_topic=on)))
    switch(True)
    t = turn("discord:loot-and-dkp")
    on = prompt(rig, t)
    serve(rig, False)
    off = prompt(rig, t)
    serve(rig, True)
    assert on == off and wowtopic.hold_text("discord:loot-and-dkp") in on
    assert topics.hold_text("loot-and-dkp") not in on
    # with [talk] wow_topic off, the channel's own purpose holds it instead
    switch(False)
    assert topics.hold_text("loot-and-dkp") in prompt(rig, t)


def test_the_guard_draft_carries_the_channel_for_server_turns_only(rig):
    review = rig.engine.live_review
    assert review.draft(turn("discord:food"), meta(), "x").channel_topic == "food"
    assert review.draft(turn("discord:general"), meta(), "x").channel_topic == ""
    assert review.draft(turn("dm:103", to_guid=HUMAN), meta(), "x").channel_topic == ""
    owed = turn("discord:food", kind=TurnKind.REPLY_HUMAN, addressee=HUMAN)
    assert review.draft(owed, meta(why="discord:reply", answers_human=True), "x").channel_topic == ""
    # a WoW channel's drafts are checked too, whatever [talk] wow_topic says
    assert review.draft(turn("discord:loot-and-dkp"), meta(), "x").channel_topic == "loot-and-dkp"
    photo = meta()
    photo.photo = object()
    assert review.draft(turn("discord:memes", kind=TurnKind.OPEN), photo, "x").with_photo
