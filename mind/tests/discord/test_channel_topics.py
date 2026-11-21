"""discord/topics.py: each channel's purpose, its hold line and the lexical off-topic check
([server] channel_topics). Invented lines only."""
import pytest

from leeroy_mind.discord import layout, topics


def test_every_channel_has_a_purpose_and_only_general_takes_anything():
    for ch in layout.CHANNELS:
        assert ch.about, ch.id
        if ch.id == "general":
            assert not ch.not_here and topics.hold_text(ch.id) is None
        else:
            assert ch.not_here, ch.id
            line = topics.hold_text(ch.id)
            assert line.startswith(f"Stay on what this channel is about ({ch.about}): ")
            assert line.endswith(".") and len(line) < 220


def test_event_only_channels_are_the_ones_whose_talk_rests_on_a_post():
    assert {c.id for c in layout.CHANNELS if c.event_only} == {
        "announcements", "rules", "raid-schedule", "pics", "memes", "logs", "applications",
        "bank-requests"}


def test_memes_purpose_says_no_plain_chat():
    line = topics.hold_text("memes")
    assert "meme" in line and "never plain chat" in line


def test_unknown_channels_and_dms_have_no_hold():
    assert topics.hold_text("dm:101") is None
    assert topics.hold_text("gdm:3") is None
    assert topics.hold_text("nowhere") is None


@pytest.mark.parametrize("text", [
    "POV: the tank pulls before the healers are even mounted",
    "nobody: / the raid: lets pull the next boss with half of us dead",
    "me when the loot drops and I bid everything I have",
    "when the warlock soulstones himself instead of the healer",
    "tanks be like: i'll just stand in the fire, it's fine",
    "Patchwerk has entered the chat",
    "the healers: we need mana. the tank: pulls anyway",
])
def test_text_memes_are_recognised(text):
    assert topics.meme_format(text)


@pytest.mark.parametrize("text", [
    "Last raid was rough, two wipes and no kills, we forgot how to pull",
    "had the worst morning, coffee spilled everywhere",
    "anyone up for heroics later?",
])
def test_plain_lines_are_not_memes(text):
    assert not topics.meme_format(text)


def test_a_memes_opener_must_be_a_meme_unless_it_carries_the_picture():
    plain = "Last raid was rough, two wipes and no kills, we forgot how to pull"
    assert topics.off_topic("memes", plain, opener=True) == topics.MEME_CODE
    assert topics.off_topic("memes", plain, opener=True, photo=True) is None
    assert topics.off_topic("memes", "POV: two wipes and no kills", opener=True) is None


def test_a_memes_reply_is_a_short_reaction_that_brings_no_plain_chat():
    meme = "POV: the tank pulls before the healers are even mounted"
    assert topics.off_topic("memes", "lmao every single tuesday", parent=meme) is None
    assert topics.off_topic("memes", "that tank needs a bigger leash", parent=meme) is None
    # her day, food or the calendar the meme did not bring
    assert topics.off_topic("memes", "noted, its on the calendar", parent=meme) == topics.MEME_CODE
    assert topics.off_topic("memes", "lol i need more coffee after work", parent=meme) \
        == topics.MEME_CODE
    # a word the meme brought itself is answered, not drift
    food_meme = "me when the guild feast is just a bowl of soup"
    assert topics.off_topic("memes", "the soup is a lie", parent=food_meme) is None
    # long is a story, not a reaction
    long = " ".join(["haha"] * (topics.MEME_WORDS + 1))
    assert topics.off_topic("memes", long, parent=meme) == topics.MEME_CODE


def test_general_and_info_channels_are_never_off_topic():
    drift = "long shift at the hospital then the kids and the laundry all evening"
    assert topics.off_topic("general", drift) is None
    assert topics.off_topic("announcements", drift) is None
    assert topics.off_topic("nowhere", drift) is None


def test_a_wow_channel_draft_is_off_only_when_it_is_clearly_her_life():
    drift = "long shift at the hospital then the kids wanted pizza for dinner"
    assert topics.off_topic("loot-and-dkp", drift, parent="who won the helm?") == topics.CODE
    # any word of the game keeps it on the channel
    assert topics.off_topic("loot-and-dkp", drift + ", but the helm was worth the dkp") is None
    # one stray word in a short reply is no drift
    assert topics.off_topic("loot-and-dkp", "lol same, kids asleep") is None
    # nor is answering the life the message she answers brought up
    assert topics.off_topic("loot-and-dkp", "rough shift, hope the kids slept",
                            parent="ugh long shift and the kids were up all night") is None
    assert topics.off_topic("mage", drift) == topics.CODE
    assert topics.off_topic("officer-chat", drift) == topics.CODE
    assert topics.off_topic("officer-chat", "the trial is doing fine, promote after the kids "
                                            "nap and my shift ends") is None


def test_words_the_game_shares_with_english_never_count_against_a_draft():
    # "fire", "kill", "party", "holy" are everyday words too: no drift in #food on their own
    assert topics.off_topic("food", "that hot sauce will kill me, my mouth is on fire") is None
    assert topics.off_topic("food", "my boss threw a party and holy moly the whole office came "
                                    "with nothing at all") == topics.CODE  # her day, not food


def test_content_channels():
    assert topics.off_topic("food", "baking bread tonight, the kitchen smells amazing") is None
    assert topics.off_topic("food", "we wiped on sindragosa three times, the dkp is a mess",
                            parent="what's for dinner?") == topics.CODE
    assert topics.off_topic("irl", "we wiped on sindragosa three times, the dkp is a mess",
                            parent="how was work?") == topics.CODE
    assert topics.off_topic("irl", "work was fine, the commute was the worst part") is None
    assert topics.off_topic("pets", "my dog stole a sock again") is None
    assert topics.off_topic("pets", "the loot council gave the dkp tokens away") == topics.CODE


def test_subjects_route_by_words_no_one_uses_outside_them():
    assert topics.subjects("trying a new pasta recipe tonight") == ("food",)
    assert topics.subjects("took the dog to the vet") == ("pets",)
    assert topics.subjects("farming saronite for the auction house") == ("professions-and-gold",)
    assert topics.subjects("doing some 3v3 arena after work") == ("pvp-arena",)
    assert topics.subjects("finally got the proto-drake mount") == ("achievements-and-mounts",)
    assert topics.subjects("levelling my alt this weekend") == ("lfg-and-alts",)
    # everyday words never route: a run, a party, fishing with her dad, selling her car
    assert topics.subjects("went for a run, then a party, then fishing with dad") == ()
    assert topics.subjects("selling my car and buying a new couch") == ()


def test_real_life_and_game():
    assert topics.real_life("long day at work") and not topics.game("long day at work")
    assert topics.game("raid tonight at nine") and not topics.real_life("raid tonight at nine")
