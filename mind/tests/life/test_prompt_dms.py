"""The owner's promptness rules for interruptions (life/interrupt.py, LifeService._after_line;
owner 2026-11-17): none within a minute of his message, none after his bad news, none in an
explicit scene he is turning up, and a "brb boss" only for a member whose work has a workplace.
Also the LATE shape for a DM answer that waited for her own time. Invented members and lines
only."""
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import HUMAN
from discord.test_discord_talk import answers, post, rig_for, talk_for
from discord.test_own_time import at, hand, life as set_life
from engine_rig import numbered, save_member
from life.test_interrupt import _lp, _run, clock, line_calls, persona, rig, said  # noqa: F401
from leeroy_mind.discord import owntime
from leeroy_mind.discord import store as dstore
from leeroy_mind.life import config as life_config
from leeroy_mind.life import interrupt
from leeroy_mind.life.day import Block
from leeroy_mind.life.interrupt import (DM_CAP_S, HOME_WORK, LINES, OWN_BOSS_WORK,
                                        draw_interruption, lines_for, workplace)
from leeroy_mind.life.seed import seeded
from leeroy_mind.talk import hisline
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind


def counting(monkeypatch):
    calls = []

    def draw(lp, state, availability, rng, *, cap_s=DM_CAP_S, **kw):
        calls.append(kw)
        return None
    monkeypatch.setattr(interrupt, "draw_interruption", draw)
    return calls


def after(r, turn, **meta):
    r.engine.life.after_line(r.engine, turn, TurnMeta(r.engine._new_id(), **meta))


def whisper_turn(line):
    return Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=HUMAN, addressee=HUMAN,
                trigger_line=line.id)


def dm_exchange(r, text):
    """His DM post and her answer to it, stored: what after_line follows in her DM."""
    mid = dstore.post(r.store, channel="dm:102", author=HUMAN, origin="human", text=text,
                      now=r.clock.now())
    return mid


def dm_answered(r, mid):
    dstore.post(r.store, channel="dm:102", author=102, origin="mind", text="mm, go on",
                reply_to=mid, now=r.clock.now())
    return Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)


def test_no_brb_within_a_minute_of_his_whisper_or_dm(store, clock, tmp_path, monkeypatch):
    calls = counting(monkeypatch)
    r = rig(store, clock, tmp_path)
    w = said(r, "so the vendor was shut again today")
    after(r, whisper_turn(w), answers_human=True, whisper_reply=True)
    assert calls == []  # he wrote seconds ago: she is in the conversation
    r.run(59, step=59)
    after(r, whisper_turn(w), answers_human=True, whisper_reply=True)
    assert calls == []
    r.run(2, step=2)
    after(r, whisper_turn(w), answers_human=True, whisper_reply=True)
    assert len(calls) == 1  # a minute on, her day may call her away
    mid = dm_exchange(r, "what are you up to later")
    turn = dm_answered(r, mid)
    after(r, turn, answers_human=True)
    assert len(calls) == 1
    r.run(61, step=61)
    after(r, turn, answers_human=True)
    assert len(calls) == 2
    r.close()


@pytest.mark.parametrize("line", [
    "my uncle is in hospital, they think its his heart",
    "got laid off this morning, whole team",
    "we had to put biscuit down yesterday",
    "rough day honestly, the manager yelled at me in front of everyone",
])
def test_no_brb_after_his_bad_news(store, clock, tmp_path, monkeypatch, line):
    calls = counting(monkeypatch)
    r = rig(store, clock, tmp_path)
    assert hisline.bad_news(line)
    w = said(r, line)
    r.run(70, step=70)
    after(r, whisper_turn(w), answers_human=True, whisper_reply=True)
    assert calls == []
    mid = dm_exchange(r, line)
    r.run(70, step=70)
    after(r, dm_answered(r, mid), answers_human=True)
    assert calls == []
    ok = said(r, "anyway what did you have for lunch")
    r.run(70, step=70)
    after(r, whisper_turn(ok), answers_human=True, whisper_reply=True)
    assert len(calls) == 1  # an ordinary line: her day decides again
    r.close()


def test_bad_news_and_goodbye_lexicons():
    for text in ("the kettle finally died this morning", "sick of this boss fight",
                 "that raid was a crash course", "lost my keys again", "the server crashed"):
        assert not hisline.bad_news(text), text
    for text in ("my nan passed on sunday", "was in a car accident last week",
                 "the vet says its his kidneys", "im really sick today", "been ill all week",
                 "she is ill again", "feeling ill honestly", "got laid off on friday"):
        assert hisline.bad_news(text), text
    # "ill" is "I'll" as he types it (review of #368, P2-7: 12 of 500 of his lines, all false)
    for text in ("ill be on at eight", "ok ill bring flasks", "ill do it later", "that was sick lol",
                 "crying laughing at that", "you left me on read", "just let go of it",
                 "sick of the queue times"):
        assert not hisline.bad_news(text), text
    for text in ("ok gotta run, talk tomorrow", "night x", "heading to bed", "gn",
                 "right, off to work. speak later", "i'll head off then",
                 "ok gotta sleep before the early shift. night you x",
                 "lol fine. night belra. saturday then"):
        assert hisline.goodbye(text), text
    for text in ("should i go to bed?", "later we should run naxx", "what a night that was",
                 "rough night at the depot, the forklift died", "free all night, you",
                 "i have to go faster on the pull", ""):
        assert not hisline.goodbye(text), text


def test_no_brb_in_an_explicit_scene_he_is_turning_up(store, clock, tmp_path, monkeypatch):
    calls = counting(monkeypatch)
    r = rig(store, clock, tmp_path)
    from leeroy_mind.talk import romance
    monkeypatch.setattr(romance, "explicit_whisper", lambda p, rel, **kw: kw["channel"] == "whisper")
    monkeypatch.setattr(romance, "dm_explicit", lambda p, rel, **kw: kw["channel"] != "whisper")
    hot = "cant stop thinking about you in bed"
    assert romance.his_heat(hot) and not romance.his_heat("did the patch notes drop")
    w = said(r, hot)
    r.run(70, step=70)
    after(r, whisper_turn(w), answers_human=True, whisper_reply=True)
    mid = dm_exchange(r, hot)
    r.run(70, step=70)
    after(r, dm_answered(r, mid), answers_human=True)
    assert calls == []
    cool = said(r, "did the patch notes drop")
    r.run(70, step=70)
    after(r, whisper_turn(cool), answers_human=True, whisper_reply=True)
    assert len(calls) == 1  # the overlay is in force but he is not turning it up
    monkeypatch.setattr(romance, "explicit_whisper", lambda p, rel, **kw: False)
    again = said(r, hot)
    r.run(70, step=70)
    after(r, whisper_turn(again), answers_human=True, whisper_reply=True)
    assert len(calls) == 2  # no explicit overlay in force: a flirt alone holds nobody
    r.close()


def test_a_work_line_needs_a_workplace(store, clock, tmp_path, monkeypatch):
    assert workplace(persona(job="paralegal on maternity leave")) == "none"
    assert workplace(persona(job="stay-at-home mum")) == "none"
    assert workplace(persona(job="warehouse picker", rhythm="laid off since spring")) == "none"
    assert workplace(persona(job="freelance illustrator, works from home")) == "remote"
    assert workplace(persona(job="support agent", rhythm="remote three days a week")) == "remote"
    assert workplace(persona(job="ward nurse")) == "site"
    assert workplace(SimpleNamespace()) == "site"
    assert lines_for("work") == LINES["work"] and "brb boss" in LINES["work"]
    assert lines_for("work", place="remote") == HOME_WORK and len(set(HOME_WORK)) >= 3
    assert not any("boss" in x for x in HOME_WORK)
    assert lines_for("work", boss=True, place="remote") == OWN_BOSS_WORK
    draws = lambda place, kids=False: {  # noqa: E731
        draw_interruption(_lp(Block.WORK, kids=kids), None, 10,
                          seeded("interrupt", "n", 7, 5, i), place=place)
        for i in range(4_000)} - {None}
    assert {h.line for h in draws("site")} == set(LINES["work"])
    assert {h.line for h in draws("remote")} == set(HOME_WORK)
    home = draws("none")
    assert {h.reason for h in home} == {"home"} and {h.line for h in home} == set(LINES["home"])
    assert {h.reason for h in draws("none", kids=True)} == {"kids"}
    # the service passes her place: a member on leave never draws a work line
    calls = counting(monkeypatch)
    r = rig(store, clock, tmp_path)
    save_member(store, 102, player_gender="female", job="Paralegal on maternity leave")
    r.engine.personas.invalidate(102)
    w = said(r, "what did you have for lunch")
    r.run(70, step=70)
    after(r, whisper_turn(w), answers_human=True, whisper_reply=True)
    assert [c["place"] for c in calls] == ["none"]
    r.close()


def test_a_dm_answer_that_waited_for_her_own_time_is_late_and_says_where_she_was(
        store, tmp_path, caplog, monkeypatch):
    """[talk] shape on, [life] availability 6, the cap 75 s: his DM at noon on a workday waits
    under the cap and her answer's shape is LATE with her day's excuse, once; the own-time late
    note is left out (one excuse in the prompt)."""
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(owntime, "CAP_SPAN", (0.9, 1.0))  # the wait is over LATE_OWN_S
    r = rig_for(store, at(2026, 11, 18, 12), tmp_path,  # a Wednesday noon: at work
                script=numbered("ah {n}, not yet"), shape=True)
    set_life(r)
    assert life_config.dm_wait_cap() == 75
    talk = talk_for(r)
    mid = post(r, "dm:101", "did you see the patch notes")
    posted = r.clock.now()
    hand(r, talk, mid)
    assert "discord_dm_later: guid=101" in caplog.text
    _run(r, talk, 140)
    got = answers(r, "dm:101", mid)
    assert len(got) == 1 and owntime.LATE_OWN_S <= got[0]["ts"] - posted <= 140
    user = line_calls(r)[-1]["messages"][-1]["content"]
    assert "shape=late" in caplog.text
    assert "You're only now seeing this (you're at work)" in user
    assert user.count("only now seeing") == 1 and "ago while you were" not in user
    assert "patch notes" not in caplog.text
    r.close()


def test_without_shapes_her_waited_dm_answer_carries_the_late_note(store, tmp_path, caplog,
                                                                    monkeypatch):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(owntime, "CAP_SPAN", (0.9, 1.0))
    r = rig_for(store, at(2026, 11, 18, 12), tmp_path, script=numbered("ah {n}, not yet"))
    set_life(r)
    talk = talk_for(r)
    mid = post(r, "dm:101", "did you see the patch notes")
    hand(r, talk, mid)
    _run(r, talk, 140)
    assert len(answers(r, "dm:101", mid)) == 1
    user = line_calls(r)[-1]["messages"][-1]["content"]
    assert "ago while you were at work; you are only now seeing it" in user
    assert "You're only now seeing this" not in user
    r.close()
