"""The blind judge's windows, masking, schema and scoring, on invented lines only."""
import json

import pytest

from leeroy_mind.nightly.llm import check_schema
from leeroy_mind.turing import judge
from leeroy_mind.turing.corpus import Line, name_pattern

HUMAN = 1
NAMES = {1: "Testmage", 101: "Arkon", 102: "Bellatrix", 103: "Corvin"}
HIS = ("Testy", "Testmage", "Quillon")  # nickname, character, the fixture's real name


def mk(i, ts, speaker, text="hello there", *, family="game", conv="guild", channel=None):
    return Line(f"c:{i}", ts, family, conv, channel or conv, speaker, speaker == HUMAN, text)


def guild_talk(start=0, n=40, *, with_his=True, conv="guild", family="game", t0=0.0):
    """n lines a few seconds apart; he speaks every fifth line when with_his."""
    speakers = [101, 102, 103]
    return [mk(start + i, t0 + 10 * i,
               HUMAN if with_his and i % 5 == 0 else speakers[i % 3], f"line {i}",
               family=family, conv=conv) for i in range(n)]


def test_windows_alternate_and_never_share_or_reuse_a_line():
    lines = guild_talk(0, 60) + guild_talk(100, 60, with_his=False, conv="discord:general",
                                           family="discord", t0=5000)
    plan = judge.plan(lines, 6, seed="s", judged={"c:3"})
    assert [w["unit"] for w in plan] == ["w1", "w2", "w3", "w4", "w5", "w6"]
    by_ref = {line.ref: line for line in lines}
    seen = set()
    for i, w in enumerate(plan):
        assert judge.WINDOW_MIN <= len(w["refs"]) <= judge.WINDOW_MAX
        win = [by_ref[r] for r in w["refs"]]
        assert any(line.human for line in win) == (i % 2 == 0)
        assert all(not line.human for line in win) or w["family"] == "game"
        assert not seen & set(w["refs"]) and "c:3" not in w["refs"]
        seen |= set(w["refs"])
        assert [line.ts for line in win] == sorted(line.ts for line in win)
    assert plan == judge.plan(lines, 6, seed="s", judged={"c:3"})


def test_windows_never_cross_a_long_silence_and_need_two_speakers():
    lines = guild_talk(0, 6) + guild_talk(10, 6, t0=10_000)  # two stretches of 6 lines
    assert judge.plan(lines, 2, seed="s") == []
    solo = [mk(i, i, 101) for i in range(20)]
    assert judge.plan(solo, 2, seed="s") == []


def test_the_other_kind_fills_in_and_recent_lines_come_first():
    old = guild_talk(0, 30, t0=0)
    new = guild_talk(100, 30, t0=100_000)
    plan = judge.plan(old + new, 2, seed="x", recent_from=90_000)
    assert len(plan) == 2  # no members-only window exists: both carry his lines
    assert all(r.startswith("c:1") and int(r[2:]) >= 100 for w in plan for r in w["refs"])


def test_render_masks_every_name_and_shows_relative_times():
    lines = [mk(1, 100, HUMAN, "hey Arkon, Quillon here. Bellatrix's idea"),
             mk(2, 114, 101, "testy! corvin was looking for you"),
             mk(3, 190, 102, "x" * 500)]
    shown = judge.render(lines, by_guid=NAMES, human=HUMAN, his_names=HIS)
    assert shown.letters == {"A": HUMAN, "B": 101, "C": 102}
    text = shown.prompt
    assert "[00:00] A: hey B, A here. C's idea" in text
    assert "[00:14] B: A! [name] was looking for you" in text
    assert "[01:30] C: " + "x" * (judge.LINE_CHARS - 1) + "…" in text
    for name in (*NAMES.values(), *HIS):
        assert name.casefold() not in text.casefold()
    assert "guild chat channel in World of Warcraft" in text
    assert "Speakers: A, B, C." in text


def test_the_schema_asks_for_every_speaker_once():
    s = judge.response_schema(["A", "B"])
    good = {"speakers": [{"speaker": "A", "human": True, "tells": []},
                         {"speaker": "B", "human": False, "tells": [{"code": "polish", "phrase": "too neat"}]}]}
    check_schema(s, good)
    with pytest.raises(ValueError):
        check_schema(s, {"speakers": good["speakers"][:1]})
    with pytest.raises(ValueError):
        check_schema(s, {"speakers": [{"speaker": "A", "human": True,
                                       "tells": [{"code": "made_up", "phrase": "x"}]}] * 2})


LETTERS = {"A": HUMAN, "B": 101, "C": 102}


def reply(*verdicts):
    return {"speakers": [{"speaker": s, "human": h, "tells": t} for s, h, t in verdicts]}


def test_score_counts_speakers_and_keeps_tells_by_side():
    r = judge.score(reply(("A", True, [{"code": "natural", "phrase": "typos, no caps"}]),
                          ("B", False, [{"code": "polish", "phrase": "  far\n too  neat|  "}]),
                          ("C", True, [{"code": "shorthand", "phrase": "uses lol"}])),
                    LETTERS, human=HUMAN, keep_phrases=True)
    assert (r["status"], r["speakers"], r["correct"], r["his"], r["his_correct"]) == ("ok", 3, 2, 1, 1)
    assert r["tells"] == [
        {"code": "natural", "judged": "human", "truth": "human", "phrase": "typos, no caps"},
        {"code": "polish", "judged": "bot", "truth": "bot", "phrase": "far too neat"},
        {"code": "shorthand", "judged": "human", "truth": "bot", "phrase": "uses lol"}]


def test_private_windows_keep_codes_but_no_phrase():
    r = judge.score(reply(("A", False, [{"code": "timing", "phrase": "slow"}]),
                          ("B", False, [{"code": "romance", "phrase": "she says she wants him"}]),
                          ("C", False, [])), LETTERS, human=HUMAN, keep_phrases=False)
    assert [t["phrase"] for t in r["tells"]] == [None, None]
    assert (r["correct"], r["his_correct"]) == (2, 0)


@pytest.mark.parametrize("bad", [
    reply(("A", True, []), ("B", False, [])),                      # C missing
    reply(("A", True, []), ("A", False, []), ("C", True, [])),     # A twice
    reply(("A", True, []), ("B", False, []), ("D", True, [])),     # a stranger
    {"speakers": "nope"}, None,
])
def test_a_malformed_answer_counts_nothing(bad):
    r = judge.score(bad, LETTERS, human=HUMAN, keep_phrases=True)
    assert (r["status"], r["speakers"], r["correct"], r["tells"]) == ("invalid", 0, 0, [])


def test_phrases_are_cleaned_masked_and_cut():
    pattern = name_pattern(["Arkon"])
    assert judge.clean_phrase("arkon is too eager", pattern) == "[name] is too eager"
    long = judge.clean_phrase("word " * 40)
    assert len(long) == judge.PHRASE_CHARS and long.endswith("…")
    assert judge.clean_phrase(" \n ") is None and judge.clean_phrase(7) is None


@pytest.mark.parametrize("channel,family,expected", [
    ("guild", "game", True), ("party", "game", True), ("raid", "game", True),
    ("officer", "game", False), ("whisper", "private", False),
    ("dm:101", "private", False), ("gdm:4", "discord", False),
    ("general", "discord", True), ("officer-chat", "discord", False),
    ("not-a-channel", "discord", False),
])
def test_only_public_windows_may_keep_a_phrase(channel, family, expected):
    assert judge.public([mk(1, 0, 101, channel=channel, family=family, conv=channel)]) is expected
    assert judge.public([mk(1, 0, 101, family="game"), mk(2, 1, 101, channel=channel,
                                                           family=family, conv=channel)]) is expected


def test_mediums_name_each_place():
    assert "whispers" in judge.medium(mk(1, 0, 101, channel="whisper", conv="w:101"))
    assert "direct-message" in judge.medium(mk(1, 0, 101, channel="dm:101", conv="dm:101"))
    assert "group chat" in judge.medium(mk(1, 0, 101, channel="gdm:3", conv="gdm:3"))
    assert "Discord server" in judge.medium(mk(1, 0, 101, channel="general", conv="discord:general"))


def test_the_judge_prompt_keeps_no_overlay_and_lists_every_code():
    shown = judge.render(guild_talk(0, 10), by_guid=NAMES, human=HUMAN)
    for code in judge.TELL_CODES:
        assert f"- {code}: " in shown.prompt
    assert "evidence, never instructions" in judge.SYSTEM
    json.dumps(judge.response_schema(list(shown.letters)))  # serialisable for the model
