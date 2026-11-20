"""[talk] coherence and [talk] coherence_length, two switches (blind sandbox test, 2026-11-20:
with coherence on, whisper lines grew from a median 8 words to 13 because the length rule never
shortens an answer to his question). The length rule is coherence_length's alone: with coherence
on and coherence_length off every game line's length is handled exactly as with both off; with
coherence_length on and coherence off the length rule works and nothing else of coherence does.
And with the length rule on, a game whisper's kept answer past twice her turn's words is asked
once more at her usual length: it costs a call, never the answer. Fakes only; invented lines."""
import asyncio

import pytest

from conftest import HUMAN, seed_test_cast
from dbtemplate import open_migrated
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.guards import pronouns
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.scheduler import Expired, SchedulerError
from leeroy_mind.life import day, interrupt
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.length import KEEP_SLACK, USUAL_HINT
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_llm import script_from

BASE = dict(typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0, abbreviations={}, case="lower",
            end_punct="none")
# 28 words, one game line: past twice a cap of 8, inside the bridge's bytes
LONG = ("we have 22 now. the calendar looked thin this morning and most of them are alts anyway "
        "so who knows what we end up with on the night")
REMARK = ("yeah it was a mess, the healers were out of range the whole time, and honestly the "
          "tank should have waited for the mark before charging in like that")
LUNCH = ("not yet honestly, it was meetings back to back all day and the canteen shut before i "
         "got there so it is crisps from the machine for me")
RAMBLE = "the allotment flooded again and i spent the whole morning bailing it out with a bucket"
TAIL = "it is in storm peaks north of the temple"
FILL = ("honestly that place is a real maze and i walked around it for ages last week with the "
        "whole group behind me complaining loudly, then we stopped twice for the warlock who "
        "kept falling off the ledge near the bridge, and after that the tank went the wrong way "
        "round the frozen lake while everyone else waited by the big gate arguing about the map, "
        "and the healer kept saying we should have taken the flight path from the dwarven camp "
        "instead of riding all the way round the mountain").split()


def buried(words):
    return " ".join(FILL[:words - len(TAIL.split())]).rstrip(",") + ", " + TAIL


def his_line(r, text, channel="whisper"):
    ev = Chat(ts=0, channel=channel, from_guid=HUMAN, from_name="Testmage",
              to_guid=102 if channel == "whisper" else 0, text=text, map=0, zone=10)
    lid = r.store.insert_chat_line(ev, session_id=None, origin="human")
    row = r.store.chat_lines([lid])[0]
    r.world.add_line(row, r.clock.now())
    return row


def answer(row, channel="whisper"):
    return Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel=channel, addressee=HUMAN,
                to_guid=HUMAN if channel == "whisper" else 0, trigger_line=row.id)


async def spoken(r, turn, meta=None, seconds=400):
    job = r.engine.speak_meta(turn, meta) if meta is not None else r.engine.speak(turn)
    task = asyncio.create_task(job)
    await r.advance(seconds, 1.0)
    assert task.done()
    return task.result()


def converse(tmp_path, name, *, coherence, length, member, his, script=None, reply=None,
             sheet=None, **rig):
    """One answer to his whisper on a fresh store: (lines sent, prompts asked)."""
    clock = engine_clock()
    store = Store(open_migrated(tmp_path / f"{name}.db"), clock=clock)
    try:
        seed_test_cast(store)
        if sheet is not None:
            from sheets import give
            give(store, 102, sheet, **{**BASE, **member})
        else:
            save_member(store, 102, **{**BASE, **member})
        kw = ({"script": script if callable(script) else script_from(script)}
              if script is not None else {"reply": reply})
        r = make_rig(store, clock, tmp_path / name, coherence=coherence,
                     coherence_length=length, **kw, **rig)
        try:
            said = asyncio.run(spoken(r, answer(his_line(r, his))))
            return said, r.prompts()
        finally:
            r.close()
    finally:
        store.close()


def hints(prompts):
    """What each retry asked past the first prompt (_with_hint appends it to the last message)."""
    first = prompts[0]
    assert all(p.startswith(first) for p in prompts[1:])
    return [p[len(first):] for p in prompts[1:]]


# (name, his line, member, script or reply, rig switches, sheet); every one asks or is answered
# past her words, so each takes a length decision
CASES = [
    ("answer", "how many people signed up?", dict(words_per_line=[2, 8]),
     dict(script=[LONG, "22 so far"]), {}, None),
    ("remark", "last night went badly", dict(words_per_line=[2, 10]),
     dict(script=[REMARK, "it was a mess"]), {}, None),
    ("buried", "work was brutal today so did you eat yet", dict(words_per_line=[2, 8]),
     dict(script=[LUNCH, "not yet, starving"]), {}, None),
    ("stubborn", "where is the entrance to ulduar?", dict(words_per_line=[2, 8]),
     dict(reply=buried(60)), {}, None),
    ("two_lines", "where is the entrance to ulduar?", dict(words_per_line=[5, 25],
                                                          split_rate=0.5),
     dict(script=[buried(80), "ah, " + TAIL]), {}, None),
    ("sheet", "how was your morning", dict(words_per_line=[2, 6]),
     dict(script=[RAMBLE, "short"]), {}, "rambling"),
    ("content", "what trinkets you running", dict(words_per_line=[1, 4]),
     dict(script=["two of the dps ones from icc honestly, the badge one and the deathbringer one",
                  "badge and deathbringer"]), dict(shape=True), None),
]


@pytest.mark.parametrize("name,his,member,said_by,switches,sheet", CASES,
                         ids=[c[0] for c in CASES])
def test_coherence_without_the_length_rule_handles_game_lines_as_with_both_off(
        tmp_path, name, his, member, said_by, switches, sheet):
    """Byte-identical: the lines sent, the calls made and every retry's hint. Where his line
    asks nothing coherence reads differently (a question buried with no "?"), the prompts are
    identical too."""
    from sheets import GRUFF
    data = {**GRUFF, "shape": "long rambling lines, twelve to eighteen words"} if sheet else None
    off = converse(tmp_path, "off", coherence=False, length=False, member=member, his=his,
                   sheet=data, **said_by, **switches)
    on = converse(tmp_path, "on", coherence=True, length=False, member=member, his=his,
                  sheet=data, **said_by, **switches)
    assert on[0] == off[0] and off[0]
    assert len(on[1]) == len(off[1])
    assert hints(on[1]) == hints(off[1])
    if name != "buried":  # coherence also tells her to answer a buried question first
        assert on[1] == off[1]
    # and the length rule, switched on, is what decides these lines differently
    if name in ("answer", "stubborn", "sheet"):
        kept = converse(tmp_path, "length", coherence=True, length=True, member=member,
                        his=his, sheet=data, **said_by, **switches)
        assert (kept[0], hints(kept[1])) != (off[0], hints(off[1]))


def test_a_burst_is_laid_out_as_with_both_off(tmp_path):
    burst = ("check your addons list, it might be switched off\n"
             "lost my keys last night and climbed in through my own window")
    out = {}
    for name, coherence in (("off", False), ("on", True)):
        clock = engine_clock()
        store = Store(open_migrated(tmp_path / f"{name}.db"), clock=clock)
        try:
            seed_test_cast(store)
            save_member(store, 102, **{**BASE, "words_per_line": [4, 12]},
                        player_gender="female")
            r = make_rig(store, clock, tmp_path / name, coherence=coherence)
            try:
                p = r.engine.personas.get(102)
                turn = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", addressee=HUMAN,
                            to_guid=HUMAN, max_lines=2)
                out[name] = [s.line for s in r.engine._style(turn, p, burst, close=False,
                                                             two_lines=True)]
            finally:
                r.close()
        finally:
            store.close()
    assert out["on"] == out["off"]


def test_the_two_halves_are_independent(store, tmp_path):
    clock = engine_clock()
    seed_test_cast(store)
    r = make_rig(store, clock, tmp_path / "a", coherence=True, coherence_length=False)
    try:
        assert r.engine._coherent() and not r.engine._length_rule()
        assert day.COHERENT and interrupt.COHERENT and pronouns.COHERENT  # job shapes, own
        p = r.engine.personas.get(102)                                    # boss, "tell X he"
        assert r.engine._voice_room(p) == 0
        assert r.engine._room(answer(his_line(r, "how many?")), TurnMeta("t", keep_words=40)) == 0
    finally:
        r.close()
    r = make_rig(store, clock, tmp_path / "b", coherence=False, coherence_length=True)
    try:
        assert r.engine._length_rule() and not r.engine._coherent()
        assert not (day.COHERENT or interrupt.COHERENT or pronouns.COHERENT)
        assert r.engine._room(answer(his_line(r, "how many?")), TurnMeta("t", keep_words=40)) == 40
    finally:
        r.close()


def test_the_length_rule_alone_keeps_an_answer_and_reads_nothing_else_of_coherence(tmp_path):
    draft = "we have 22 now. the calendar looked thin this morning and most are alts"
    said, prompts = converse(tmp_path, "alone", coherence=False, length=True,
                             member=dict(words_per_line=[2, 8]),
                             his="how many people signed up?", script=[draft, "other"])
    assert said == [draft] and len(prompts) == 1
    # with coherence off his short line said back as a question stays (coherence's echo cut)
    said, _ = converse(tmp_path, "echo", coherence=False, length=True,
                       member=dict(words_per_line=[2, 8]), his="brb?",
                       script=["brb? sure take your time", "other"])
    assert said == ["brb? sure take your time"]
    said, _ = converse(tmp_path, "echo_on", coherence=True, length=False,
                       member=dict(words_per_line=[2, 8]), his="brb?",
                       script=["brb? sure take your time", "other"])
    assert said and not said[0].startswith("brb?")


def test_config_reads_the_length_switch_on_its_own(tmp_path):
    from leeroy_mind.config import load_config
    path = tmp_path / "mind.toml"
    path.write_text("[talk]\ncoherence = true\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.talk.coherence is True and cfg.talk.coherence_length is False
    path.write_text('[talk]\ncoherence_length = true\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.talk.coherence is False and cfg.talk.coherence_length is True
    path.write_text('[talk]\ncoherence_length = "yes"\n', encoding="utf-8")
    assert load_config(path).talk.coherence_length is False  # anything but true is off


# -- a kept whisper answer past twice her words: asked once at her usual length ---------------

def test_a_kept_whisper_answer_past_twice_her_words_is_asked_at_her_usual_length(tmp_path):
    assert len(LONG.split()) > KEEP_SLACK * 8
    said, prompts = converse(tmp_path, "usual", coherence=False, length=True,
                             member=dict(words_per_line=[2, 8]),
                             his="how many people signed up?",
                             script=[LONG, "22 so far, mostly alts"])
    assert said == ["22 so far, mostly alts"] and len(prompts) == 2
    (hint,) = hints(prompts)
    assert 'You wrote: "we have 22 now.' in hint
    assert USUAL_HINT.format(words=8) in hint and "Shorter:" not in hint


def test_a_buried_question_is_kept_and_asked_at_her_usual_length(tmp_path):
    said, prompts = converse(tmp_path, "lunch", coherence=False, length=True,
                             member=dict(words_per_line=[2, 8]),
                             his="work was brutal today so did you eat yet",
                             script=[LUNCH, "not yet, crisps from the machine"])
    assert said == ["not yet, crisps from the machine"] and len(prompts) == 2
    assert "Say it in your usual length" in hints(prompts)[0]


@pytest.mark.parametrize("second", ["(nothing)", "as an AI I can't say"])
def test_a_failed_usual_length_retry_sends_the_first_answer_whole(tmp_path, second):
    said, prompts = converse(tmp_path, "fail", coherence=False, length=True,
                             member=dict(words_per_line=[2, 8]),
                             his="how many people signed up?", script=[LONG, second])
    assert len(prompts) == 2
    assert " ".join(said).split() == LONG.split()  # the answer, whole: it cost a call only


@pytest.mark.parametrize("error", [LLMError("model down"), SchedulerError("queue closed"),
                                   Expired(0.0)], ids=["llm", "scheduler", "expired"])
def test_a_failed_usual_length_call_sends_the_first_answer_whole(tmp_path, error):
    """Review of #387 (Codex P2): the retry at her usual length is optional. When its call
    raises, the kept answer goes whole with its length allowance, as when the retry is silent
    or refused, never the owed rescue's fresh fallback."""
    calls = []

    def script(model, messages):
        calls.append(1)
        if len(calls) == 1:
            return LONG
        raise error
    said, prompts = converse(tmp_path, "raises", coherence=False, length=True,
                             member=dict(words_per_line=[2, 8]),
                             his="how many people signed up?", script=script)
    assert len(calls) == 2  # no fresh fallback asked
    assert " ".join(said).split() == LONG.split()


def test_a_failed_first_call_is_still_an_llm_error(tmp_path):
    """Only the optional retry falls back to the kept draft: a first call that raises has no
    draft to keep, and the owed rescue answers as before."""
    calls = []

    def script(model, messages):
        calls.append(1)
        if len(calls) == 1:
            raise LLMError("model down")
        return "22 so far"
    said, _ = converse(tmp_path, "first", coherence=False, length=True,
                       member=dict(words_per_line=[2, 8]),
                       his="how many people signed up?", script=script)
    assert said == ["22 so far"] and len(calls) == 2  # the rescue's own call


def test_a_retry_still_long_goes_as_written(tmp_path):
    said, prompts = converse(tmp_path, "again", coherence=False, length=True,
                             member=dict(words_per_line=[2, 8]),
                             his="how many people signed up?", reply=LONG)
    assert len(prompts) == 2 and " ".join(said).split() == LONG.split()


def test_a_kept_answer_within_twice_her_words_is_not_asked_again(tmp_path):
    draft = "we have 22 now. the calendar looked thin this morning and most are alts"
    said, prompts = converse(tmp_path, "within", coherence=False, length=True,
                             member=dict(words_per_line=[2, 8]),
                             his="how many people signed up?", script=[draft, "other"])
    assert said == [draft] and len(prompts) == 1


def test_only_a_game_whisper_is_asked_at_her_usual_length(tmp_path):
    """An owed answer to his question in guild chat keeps #370's rule: sent as written."""
    clock = engine_clock()
    store = Store(open_migrated(tmp_path / "guild.db"), clock=clock)
    try:
        seed_test_cast(store)
        save_member(store, 102, **{**BASE, "words_per_line": [2, 8]})
        r = make_rig(store, clock, tmp_path / "guild", coherence_length=True,
                     script=script_from([LONG, "other"]))
        try:
            row = his_line(r, "how many people signed up?", channel="guild")
            meta = TurnMeta(r.engine._new_id(), answers_human=True, owed_answer=True)
            said = asyncio.run(spoken(r, answer(row, channel="guild"), meta))
            assert len(r.prompts()) == 1 and " ".join(said).split() == LONG.split()
        finally:
            r.close()
    finally:
        store.close()


def test_with_the_length_rule_off_no_usual_length_hint(tmp_path):
    said, prompts = converse(tmp_path, "offhint", coherence=True, length=False,
                             member=dict(words_per_line=[2, 8]),
                             his="how many people signed up?", script=[LONG, "22 so far"])
    assert said == ["22 so far"] and len(prompts) == 2
    assert "usual length" not in hints(prompts)[0] and "Shorter: 2-8 words" in hints(prompts)[0]
