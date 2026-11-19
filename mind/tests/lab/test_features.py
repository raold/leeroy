"""The judge's model-free features on invented lines."""
import pytest

from leeroy_mind.lab import features


def test_every_feature_is_there_and_bounded():
    v = features.vector("did you get the flasks for tonight", "got 4, need like 6 more. you have herbs?")
    assert tuple(v) == features.NAMES
    assert all(isinstance(x, float) for x in v.values())
    assert -3 <= v["ratio"] <= 4 and 0 <= v["over"] <= 4
    for name in features.NAMES[2:]:
        assert 0 <= v[name] <= 1, name


def test_length_is_read_against_his_line():
    short = features.vector("lol", "ha")
    long = features.vector("lol", " ".join(["word"] * 40))
    assert short["short_both"] == 1.0 and long["short_both"] == 0.0
    assert long["ratio"] > short["ratio"] and long["over"] > 2 and short["over"] == 0.0
    assert long["words"] > short["words"]
    even = features.vector("so what did you end up doing about the car", "took it to the garage on main st")
    assert even["over"] == 0.0 and abs(even["ratio"]) < 1


def test_overlap_run_and_echo_see_his_words_said_back():
    his = "i think im gonna skip the raid on friday"
    back = features.vector(his, "you think youre gonna skip the raid on friday? aw")
    own = features.vector(his, "nooo we need you. who tanks then")
    assert back["overlap"] > own["overlap"] and back["run"] > own["run"]
    assert back["run"] >= 5 / 8
    assert features.vector("the flask prices went up again this week on our server",
                           "the flask prices went up again this week on our server")["echo"] == 1.0
    assert own["echo"] == 0.0


def test_repetition_is_read_against_her_last_replies():
    earlier = ["honestly the bakery shift wrecked me today, my feet are dead",
               "raid at 8 right"]
    again = features.vector("how are you", "the bakery shift wrecked me today, feet are dead",
                            earlier=earlier)
    fresh = features.vector("how are you", "good, just made tea", earlier=earlier)
    assert again["repeat"] > 0.5 and again["rerun"] >= 4 / 8
    assert fresh["repeat"] == 0.0 and fresh["rerun"] == 0.0
    # only her last EARLIER replies count
    old = features.vector("how are you", "the bakery shift wrecked me today, feet are dead",
                          earlier=earlier[:1] + ["x"] * features.EARLIER)
    assert old["rerun"] == 0.0


@pytest.mark.parametrize("his, ask", [
    ("are you coming tonight?", "yesno"), ("so do you want the mount?", "yesno"),
    ("what time is the raid?", "number"), ("how many flasks do you have?", "number"),
    ("who is tanking?", "name"), ("what do you think?", None), ("why though?", None),
    ("you coming tonight", None), ("nice. you in?", "yesno")])
def test_what_his_line_asks_is_read_from_its_words(his, ask):
    assert features.ask_of(his) == ask


def test_a_dodged_question_and_a_question_back():
    assert features.vector("are you coming tonight?", "the weather is something else today")["dodge"] == 1.0
    assert features.vector("are you coming tonight?", "yeah, after dinner")["dodge"] == 0.0
    assert features.vector("what do you think?", "the weather is something else")["dodge"] == 0.0
    v = features.vector("long day", "same. what happened?")
    assert v["asks_back"] == 1.0 and v["asked"] == 0.0


def test_polish_lines_and_rewritten():
    prose = features.vector("work was rough", "That sounds really hard; it makes sense that you feel "
                                              "drained. Remember to rest tonight.")
    text = features.vector("work was rough", "ugh same. what happened")
    assert prose["polish"] > text["polish"] and prose["polish"] >= 0.75
    assert prose["sentences"] == pytest.approx(2 / 6)
    assert features.vector("hi", "hey\nyou ok\nmissed you")["lines"] == pytest.approx(2 / 3)
    assert features.vector("hi", "hey", rewritten=True)["rewritten"] == 1.0
    assert features.vector("hi", "hey")["rewritten"] == 0.0


def test_an_empty_reply_or_line_is_all_zeros_but_length():
    v = features.vector("", "")
    assert v["overlap"] == 0.0 and v["echo"] == 0.0 and v["short_both"] == 0.0
