"""[server] channel_topics in the guards (discord/topics.py): a server draft clearly off its
channel's purpose gets one soft rewrite (`channel_topic`, `channel_meme` in #memes) whose hint
says so in words; a draft with no channel to keep to is never checked. Invented lines only."""
from datetime import datetime, timezone

import pytest

from conftest import seed_test_cast
from leeroy_mind.discord import topics
from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, review
from leeroy_mind.guards.review import DROP_CODES, HARD_CODES, HINTS, rewrite_hint
from leeroy_mind.guards.types import HistoryLine

NOW = datetime(2026, 11, 21, 20, tzinfo=timezone.utc).timestamp()
MEME = "POV: the tank pulls before the healers are even mounted"


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    return store


def stores(store, *history):
    return GuardStores(store.conn, GuardsCfg(enabled=True), history=tuple(history))


def line(id_, text, room, guid=102):
    return HistoryLine(id_, NOW - 30, guid, text, 'discord', room=room, readers=(101, 102, 103))


def draft(text, room, **kw):
    return Draft(101, 'discord', text, room=room, now=NOW, **kw)


def test_codes_are_soft_rewrites_with_words():
    for code in (topics.CODE, topics.MEME_CODE):
        assert code not in DROP_CODES and code not in HARD_CODES
        assert code in HINTS and code not in rewrite_hint((code,))
    assert "#memes" in rewrite_hint((topics.MEME_CODE,))


def test_a_drifting_reply_is_rewritten_once(cast):
    parent = line(7, "who won the helm last night?", "loot-and-dkp")
    text = "long shift at the hospital then the kids wanted pizza for dinner"
    v = review(stores(cast, parent), draft(text, "loot-and-dkp", parent_id=7,
                                           channel_topic="loot-and-dkp"))
    assert v.action == "rewrite" and topics.CODE in v.codes
    # without the channel to keep to, nothing is checked
    plain = review(stores(cast, parent), draft(text, "loot-and-dkp", parent_id=7))
    assert topics.CODE not in plain.codes


def test_a_memes_opener_must_be_a_meme_unless_it_carries_the_picture(cast):
    plain = "the raid last night was two wipes and no kills"
    assert topics.MEME_CODE in review(stores(cast), draft(plain, "memes", channel_topic="memes")).codes
    assert topics.MEME_CODE not in review(stores(cast), draft(
        plain, "memes", channel_topic="memes", with_photo=True)).codes
    assert topics.MEME_CODE not in review(stores(cast), draft(
        "POV: two wipes and no kills", "memes", channel_topic="memes")).codes


def test_a_memes_reaction_bringing_her_day_is_rewritten(cast):
    meme = line(9, MEME, "memes")
    off = review(stores(cast, meme), draft("noted, it's on the calendar for after work", "memes",
                                           parent_id=9, channel_topic="memes"))
    assert topics.MEME_CODE in off.codes
    ok = review(stores(cast, meme), draft("that tank needs a shorter leash", "memes", parent_id=9,
                                          channel_topic="memes"))
    assert topics.MEME_CODE not in ok.codes


def test_a_bad_channel_topic_is_refused(cast):
    with pytest.raises(ValueError, match="guard_channel_topic_invalid"):
        review(stores(cast), draft("ok", "food", channel_topic="nowhere"))
    with pytest.raises(ValueError, match="guard_channel_topic_invalid"):
        review(stores(cast), draft("ok", "food", channel_topic="food", with_photo=1))
