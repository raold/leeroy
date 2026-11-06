import pytest

from leeroy_mind.talk.moves import classify, is_summon, main_move, topics


@pytest.mark.parametrize("text,move", [
    ("then just get over here", "summon"), ("i've been waiting all night", "summon"),
    ("door's open, your move", "summon"), ("hurry up already", "summon"),
    ("i want you so bad", "declare"), ("you look good in that tier", "compliment"),
    ("bet you can't even pull that", "tease"), ("what did you do today?", "question"),
    ("remember arathi last week", "callback"), ("we should actually meet one day", "plan"),
    ("ugh my boss kept me late again", "complaint"), ("after raid ;)", "later"),
    ("hey handsome", "petname"), ("sent you 200g", "gift")])
def test_each_move_is_classified(text, move):
    assert move in classify(text)


@pytest.mark.parametrize("text", ["meet me in dalaran", "come duel me", "come to vent",
                                  "get over here we're queueing for the bg", "find me in shatt"])
def test_in_game_invitations_are_never_summons(text):
    assert "summon" not in classify(text) and "invite_game" in classify(text)
    assert not is_summon(text)


def test_main_move_prefers_the_summons_then_the_declaration():
    assert main_move(classify("i want you. get over here")) == "summon"
    assert main_move(classify("i want you")) == "declare" and main_move(frozenset()) == ""


def test_topics_skip_game_words_names_and_stopwords():
    assert topics("the table is up, arkon wiped the raid on the table", names={"arkon"}) == ("table",)


def test_typographic_apostrophes_fold():
    assert "summon" in classify("door’s open")


@pytest.mark.parametrize("text", [
    "come over after raid", "come over after the raid, i'm waiting", "hurry up, the raid starts",
    "find me after raid", "door's open, the bg is up", "I'm waiting, hurry up, the queue is long",
    "get over here, i have a group of tanks for you", "come over after this bg and hurry up"])
def test_a_game_word_as_a_time_or_aside_does_not_hide_a_summons(text):
    assert "summon" in classify(text) and "invite_game" not in classify(text)


@pytest.mark.parametrize("text", ["come to org with me", "join the raid", "inv me",
                                  "queue with me", "come help me with the quest",
                                  "meet me at the arena", "come to my group voice chat"])
def test_destinations_and_activities_are_invitations(text):
    assert "invite_game" in classify(text) and "summon" not in classify(text)


def test_right_now_is_not_a_later():
    assert "later" not in classify("i want you right now")
