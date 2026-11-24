"""[talk] wow_topic's pure rules (talk/wowtopic.py, talk/entities.py, guards/register.py): which
turns are held, the hold and DKP lines, the length trim and num_predict, the chat register, the
mind's own instruction text without unknown game entities, the raid night's time in words, DKP
treated as transferable and her flirts on the public channels. Invented text only."""
from datetime import datetime, timedelta, timezone

import pytest

from leeroy_mind.guards import register
from leeroy_mind.realm.calendar_time import clock_words, when_words
from leeroy_mind.talk import wowtopic as w
from leeroy_mind.talk.entities import known_only
from leeroy_mind.talk.turns import TurnKind


# -- which turns ----------------------------------------------------------------------------------

@pytest.mark.parametrize("channel", ["guild", "officer", "party", "raid", "raid_warning",
                                     "discord:loot-and-dkp", "discord:druid", "voice:raid"])
def test_wow_channels(channel):
    assert w.wow_topic_channel(channel, his_raid=False)


@pytest.mark.parametrize("channel", ["whisper", "dm:102", "gdm:4", "discord:general", "say",
                                     "voice:irl", "discord:irl"])
def test_never_private_or_general(channel):
    assert not w.wow_topic_channel(channel, his_raid=False)


def test_say_and_yell_only_while_he_raids():
    assert w.wow_topic_channel("say", his_raid=True) and w.wow_topic_channel("yell", his_raid=True)
    assert not w.wow_topic_channel("whisper", his_raid=True)
    assert not w.wow_topic_channel("discord:general", his_raid=True)


@pytest.mark.parametrize("kind", sorted(w.HELD_KINDS, key=lambda k: k.value))
def test_the_channels_business_is_held(kind):
    assert w.held(kind, "go when ready")


@pytest.mark.parametrize("kind", [TurnKind.BIT, TurnKind.FLIRT, TurnKind.GREET,
                                  TurnKind.GOODNIGHT, TurnKind.EMOTE_REACT, TurnKind.WHISPER])
def test_a_turn_that_asks_for_something_else_is_never_held(kind):
    assert not w.held(kind, "go when ready")


def test_an_opener_is_held_only_on_a_game_topic():
    assert not w.held(TurnKind.OPEN)
    assert w.held(TurnKind.OPEN, game_topic=True)


@pytest.mark.parametrize("line", ["how was work today", "kids asleep yet?", "did you eat dinner",
                                  "back home from the office", "you working tomorrow?",
                                  "hows the weather over there", "how was your day job"])
def test_a_line_about_her_life_is_not_held(line):
    assert w.asks_life(line) and not w.held(TurnKind.REPLY_HUMAN, line)


@pytest.mark.parametrize("line", ["good job on that pull", "kill the boss first", "go cat form",
                                  "dismiss your pet", "hearth and come back", "does the strat work"])
def test_game_words_are_no_life(line):
    assert not w.asks_life(line)


def test_the_hold_line_names_the_channel():
    assert w.hold_text("raid") == ("Stay on what this channel is about (the raid): bring up your "
                                   "job, home or day only if someone asks.")
    assert "(loot and DKP)" in w.hold_text("discord:loot-and-dkp")
    assert "(your class)" in w.hold_text("discord:death-knight")
    assert "(your class)" in w.hold_text("voice:mages")
    assert "(guild business)" in w.hold_text("officer")
    assert "(groups and alts)" in w.hold_text("discord:lfg-and-alts")
    assert "(the game)" in w.hold_text("voice:pvp")


# -- loot -----------------------------------------------------------------------------------------

def test_loot_turns():
    assert w.loot_turn("discord:loot-and-dkp") and w.loot_turn("voice:loot_dkp")
    assert w.loot_turn("raid", ("bids on the helm to me",))
    assert w.loot_turn("raid", ("[Invented Helm] ms or os?",))
    assert w.loot_turn("guild", ("", "how much dkp do you have"))
    assert not w.loot_turn("raid", ("pull when ready", "nice kill"))
    assert not w.loot_turn("guild", ("os tonight anyone?", "barrel roll off the ledge"))
    assert w.loot_turn("raid", ("ms/os on the ring", "roll for the token"))
    assert "never gold" in w.DKP_RULE and "DKP" in w.DKP_RULE


def test_the_dkp_line_says_dkp_is_her_own():
    assert "can't be given, lent or pooled" in w.DKP_RULE


@pytest.mark.parametrize("text", [
    "got 30 dkp for ya, grab the ring", "just pass me your DKP lol",
    "ill give you my dkp for it", "lend me some dkp?", "we could pool our dkp for the bow",
    "gonna send 50 dkp to him", "you can borrow my dkp", "have 40 dkp for u",
    "Give Me Your DKP"])
def test_dkp_treated_as_transferable(text):
    assert w.dkp_transfer(text)


@pytest.mark.parametrize("text", [
    "bidding my dkp on the boots", "spend 50 dkp and its yours", "I have 146 dkp",
    "they give dkp for attendance", "saving my dkp for the helm", "dkp standings are up",
    "we got dkp for the kill", "lost the bid to you, gz", "", "hand me the flask"])
def test_dkp_spent_as_her_own(text):
    assert not w.dkp_transfer(text)


# -- public flirts ---------------------------------------------------------------------------------

@pytest.mark.parametrize("channel", sorted(w.PUBLIC_CHANNELS))
def test_a_flirt_on_a_public_game_channel(channel):
    assert w.public_flirt(TurnKind.FLIRT, channel)
    assert not w.public_flirt(TurnKind.OPEN, channel)


@pytest.mark.parametrize("channel", ["whisper", "dm:102", "gdm:4", "discord:general", "voice:raid"])
def test_a_flirt_anywhere_else_is_no_public_one(channel):
    assert not w.public_flirt(TurnKind.FLIRT, channel)


def test_the_public_flirt_line_names_who_reads_it():
    assert w.public_flirt_line("guild") == (
        "Everyone in the guild reads this: one light line, 10 words at most, and save anything "
        "more for a whisper.")
    assert "Everyone in the party" in w.public_flirt_line("party", 8)
    assert "8 words at most" in w.public_flirt_line("party", 8)


def test_game_topics_are_what_she_wants_in_game():
    assert w.game_topics(["Invented Mount", " the ICC 25 achievement. "]) == [
        "how close you are to Invented Mount in game",
        "how close you are to the ICC 25 achievement in game"]
    assert w.game_topics([]) == [w.GAME_TOPIC]


# -- length ---------------------------------------------------------------------------------------

def test_predict_follows_her_words_within_the_preset():
    assert w.predict(12) == 36
    assert w.predict(1) == w.PREDICT_MIN
    assert w.predict(200) == w.PREDICT_MAX


def test_a_draft_that_fits_is_not_trimmed():
    assert w.trim("pulling now, stay behind me", 8) is None


def test_trim_keeps_her_first_line_then_her_first_sentence():
    assert w.trim("pulling now stay behind me\nand honestly the last pull was a mess", 8) == (
        "pulling now stay behind me")
    assert w.trim("pulling the pack now. honestly that last pull was such a mess for us all", 8) == (
        "pulling the pack now.")


def test_trim_falls_back_to_her_leading_clauses():
    text = "ok so i grab the left pack, you take the right one, and we meet at the door after"
    cut = w.trim(text, 8)
    assert cut == "ok so i grab the left pack"
    assert len(cut.split()) <= 8


def test_trim_never_leaves_too_little():
    assert w.trim("ok. then we all go in together and pull the whole room at once", 5) is None


# -- the chat register ----------------------------------------------------------------------------

@pytest.mark.parametrize("text, reason", [
    ("It is a pleasure to raid with all of you tonight.", "formal"),
    ("I am ready for the next pull and I will stand behind the boss.", "formal"),
    ("it is fine, just pull", "it_is"),
    ("nice kill honey", "pet_name"),
    ("thanks, dear", "pet_name"),
    ("ready when you are, my dear", "pet_name"),
    ("Dear, the adds are up", "pet_name"),
    ("[objection: we should kill the adds first]", "aside"),
    ("that went well (Tidy.)", "aside"),
])
def test_written_prose_in_raid_chat(text, reason):
    assert reason in register.reasons(text)


@pytest.mark.parametrize("text", [
    "I'm ready for the next pull, stand behind the boss.",
    "it is what it is, next pull",
    "oh dear, wiped again",
    "i love this fight",
    "can i get [Pattern: Invented Bag] from the bank",
    "brb (lol)",
    "Ready when you are",
    "I am here",
    "pulling in 5",
])
def test_chat_is_chat(text):
    assert register.reasons(text) == ()


# -- the mind's own instruction text --------------------------------------------------------------

def test_known_only_drops_the_sentences_that_name_an_unknown_entity():
    invented = lambda s: "Gloomfang" in s  # noqa: E731 - as the game name tell would read it
    text = "Bring up the raid tonight. Gloomfang dropped nothing again. Ask who is coming."
    assert known_only(text, invented=invented) == ("Bring up the raid tonight. Ask who is coming.", 1)


def test_known_only_drops_an_unknown_bracketed_item():
    known = {"Invented Helm"}
    kept, n = known_only("She won [Invented Helm]. She wants [Made Up Blade] next.",
                         item=lambda name: name in known)
    assert (kept, n) == ("She won [Invented Helm].", 1)


def test_known_only_leaves_quoted_material_whole():
    invented = lambda s: "Gloomfang" in s  # noqa: E731
    text = ("Riff on it. Gloomfang is back. Use only this.\n<obs>Arkon: we wiped on Gloomfang. "
            "twice.</obs> Keep it kind.")
    kept, n = known_only(text, invented=invented)
    assert n == 1 and "<obs>Arkon: we wiped on Gloomfang. twice.</obs>" in kept
    assert kept.startswith("Riff on it. Use only this.")
    quoted = 'She said "we beat Gloomfang." Answer her.'
    assert known_only(quoted, invented=invented) == (quoted, 0)


def test_known_only_fails_open():
    text = "Gloomfang dropped [Made Up Blade]."
    assert known_only(text) == (text, 0)
    assert known_only("", invented=lambda s: True) == ("", 0)
    assert known_only("Gloomfang again.", invented=lambda s: True) == ("", 1)


# -- the raid night's time ------------------------------------------------------------------------

TZ = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=TZ)  # a Friday afternoon


def _at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ).timestamp()


@pytest.mark.parametrize("at, said", [
    (_at(20, 21), "tonight at 9pm server time"),
    (_at(20, 14, 30), "today at 2:30pm server time"),
    (_at(21, 20), "tomorrow night at 8pm server time"),
    (_at(23, 20), "on Monday at 8pm server time"),
    (_at(21, 3), "late tonight, at 3am server time"),
    (_at(24, 1), "late on Monday night, at 1am server time"),
    (_at(30, 20), "on Monday 30 November at 8pm server time"),
])
def test_when_words(at, said):
    assert when_words(at, NOW) == said


def test_clock_words():
    assert [clock_words(datetime(2026, 11, 20, h, m, tzinfo=TZ)) for h, m in
            ((0, 0), (12, 0), (3, 0), (21, 30), (12, 15))] == [
        "midnight", "noon", "3am", "9:30pm", "12:15pm"]
