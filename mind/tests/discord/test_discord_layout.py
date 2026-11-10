from leeroy_mind.discord import layout
from leeroy_mind.discord.layout import CATEGORIES, CHANNELS, CLASS_CHANNEL, audience
from leeroy_mind.voice.rooms import CLASS_ROOM
from tests.conftest import HUMAN, OFFICERS, make_persona, mem


def test_officer_chat_audience_is_officers_and_him():
    assert audience("officer-chat", cast={101, 102, 103}, officers={102}, human=1) == {102, 1}
    assert audience("dm:103", cast={101, 102, 103}, officers={102}, human=1) == {103, 1}
    assert audience("general", cast={101, 102}, officers=set(), human=1) == {101, 102, 1}


def test_every_channel_id_is_unique_and_class_channels_cover_every_class():
    ids = [c.id for c in CHANNELS]
    assert len(ids) == len(set(ids)) == 30
    assert set(CLASS_CHANNEL) == set(CLASS_ROOM)
    assert all(layout.channel(v) is not None for v in CLASS_CHANNEL.values())
    assert CLASS_CHANNEL[6] == "death-knight"
    assert [c.id for c in CHANNELS if c.category == "officers"] == [
        "officer-chat", "applications", "bank-requests"]
    assert all(c.officers_only == (c.category == "officers") for c in CHANNELS)
    assert all(c.category in CATEGORIES and c.topic.strip() for c in CHANNELS)
    assert layout.channel("nope") is None


def test_dm_ids():
    assert layout.is_dm("dm:103") and layout.dm_peer("dm:103") == 103
    assert not layout.is_dm("general") and layout.dm_peer("general") is None
    assert layout.dm_peer("dm:x") is None


def test_roles_follow_guild_role_and_the_trial_rank():
    p = make_persona(101, "Arkon")
    assert layout.role_of(mem(101, rank=3), p) == "raider"
    assert layout.role_of(mem(101, rank=4), p) == "trial"
    assert layout.role_of(mem(101, rank=3), make_persona(102, "B", guild_role="officer")) == "officer"
    assert layout.role_of(mem(101, rank=3), make_persona(102, "B", guild_role="social")) == "social"
    assert layout.role_of(mem(101, rank=3), None) == "social"
    assert layout.role_of(mem(101, rank=5), p, trial_rank=5) == "trial"
    assert layout.class_key(6) == "death-knight" and layout.class_key(11) == "druid"
