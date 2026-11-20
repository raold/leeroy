"""[talk] coherence, the hard-case audit's fixes: her brb and back lines take no slips and no
emoticon, a hurried answer makes no late excuse, and her brb's reason follows where she said she
is; and [talk] coherence_length's: a game burst keeps the model's own line break (game-line
layout is the length rule's). Fakes only; invented lines."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import HUMAN, make_persona
from engine_rig import engine_clock
from leeroy_mind.life import interrupt
from leeroy_mind.life.day import Block, DayEvent
from leeroy_mind.life.interrupt import Away
from leeroy_mind.life.presence import Attention, LifePresence, Where
from leeroy_mind.life.service import LifeService
from leeroy_mind.talk.pipeline import SHORT_WAIT_S, TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind
from life.test_interrupt import said
from life.test_interrupt import rig as _rig

BURST = ("check your addons list, it might be switched off\n"
         "lost my keys last night and climbed in through my own window")


pytestmark = pytest.mark.usefixtures("coherent")  # [talk] coherence is off by default


def rig(*args, **kw):
    kw.setdefault("coherence", True)  # the switch under test; a test of "off" says so
    return _rig(*args, **kw)


@pytest.fixture
def clock():
    return engine_clock()


def member(r, **over):
    from engine_rig import save_member
    base = dict(player_gender="female", typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0,
                abbreviations={}, case="lower", end_punct="none", words_per_line=[4, 12])
    save_member(r.store, 102, **{**base, **over})
    return r.engine.personas.get(102)


def whisper(**kw):
    return Turn(TurnKind.REPLY_HUMAN, 102, "whisper", addressee=HUMAN, to_guid=HUMAN,
                max_lines=2, **kw)


def test_a_game_burst_keeps_the_models_line_break(store, clock, tmp_path):
    for length in (True, False):  # [talk] coherence on in both: the break is the length rule's
        r = rig(store, clock, tmp_path / str(length), coherence_length=length)
        try:
            p = member(r)
            lines = [s.plain for s in r.engine._style(whisper(), p, BURST, close=False,
                                                      two_lines=True)]
            if length:
                assert lines == ["check your addons list, it might be switched off",
                                 "lost my keys last night and climbed in through my own window"]
            else:  # as before: one text repacked, the second line's start run into the first
                assert lines[0] != "check your addons list, it might be switched off" or \
                    lines[1] != "lost my keys last night and climbed in through my own window"
            # one line from the model is laid out as ever
            one = r.engine._style(whisper(), p, "fair enough, see you at eight", close=False,
                                  two_lines=True)
            assert " ".join(s.plain for s in one) == "fair enough, see you at eight"
        finally:
            r.close()


def test_her_brb_takes_no_slip_and_no_emoticon(store, clock, tmp_path):
    for coherence in (True, False):
        r = rig(store, clock, tmp_path / str(coherence), coherence=coherence)
        try:
            p = member(r, emoticons=["<3"], emoticon_rate=1.0, typo_rate=0.15)
            brb = [s.line for s in r.engine._style(whisper(template="brb boss"), p, "brb boss",
                                                   close=False, plain=True)]
            assert any("<3" in x for x in brb) is not coherence
            # never a typo, whatever her typo rate (review of #370), over many seeds
            for _ in range(60):
                again = [s.line for s in r.engine._style(whisper(template="gotta do a work thing"
                                                                          " brb"), p,
                                                         "gotta do a work thing brb", close=False,
                                                         plain=True)]
                if coherence:
                    assert again == ["gotta do a work thing brb"]
            # her own line still carries her mark
            own = [s.line for s in r.engine._style(whisper(), p, "see you at eight", close=False)]
            assert own and own[0].endswith("<3")
        finally:
            r.close()


def test_an_answer_hurried_back_makes_no_late_excuse(store, clock, tmp_path):
    for coherence in (True, False):
        r = rig(store, clock, tmp_path / str(coherence), coherence=coherence, shape=True)
        try:
            now = r.clock.now()
            r.engine.life._away[102] = Away(since=now - 13, until=now + 250, channel="whisper",
                                            reason="work", night="n", minute=0)
            row = said(r, "need you now, its important")
            turn = whisper(trigger_line=row.id)
            meta = TurnMeta(r.engine._new_id(), answers_human=True, heard_at=now - 12)
            assert r.engine._waited_away(turn, meta, now) is not coherence
            # a real wait is still excused
            meta = TurnMeta(r.engine._new_id(), answers_human=True,
                            heard_at=now - SHORT_WAIT_S - 30)
            r.engine.life._away[102] = Away(since=now - 200, until=now - 5, channel="whisper",
                                            reason="work", night="n", minute=0)
            assert r.engine._waited_away(turn, meta, now)
        finally:
            r.close()


def test_her_brb_reason_follows_where_she_said_she_is():
    def lp(block):
        return LifePresence(block, Where.AT_WORK, Attention.GLANCING, DayEvent.NONE, False)

    def pipe(lines, on=True):
        return SimpleNamespace(_coherent=lambda: on, _said_here=lambda t, m, now: lines)
    where = LifeService._where_she_said
    home = where(pipe(["home on the sofa with a tea", "im at home today"]), None, None,
                 lp(Block.WORK), 0.0)
    assert home.block is Block.FREE  # door, phone or food: never "brb boss"
    assert where(pipe(["im at work till six"]), None, None, lp(Block.FREE), 0.0).block is Block.WORK
    assert where(pipe(["nice pull"]), None, None, lp(Block.WORK), 0.0).block is Block.WORK
    assert where(pipe(["im at home today"], on=False), None, None, lp(Block.WORK),
                 0.0).block is Block.WORK
    assert where(SimpleNamespace(), None, None, lp(Block.WORK), 0.0).block is Block.WORK


def test_a_trade_with_private_clients_is_her_own_boss():
    def p(job, rhythm="evenings free"):
        return make_persona(101, "Arkon", job=job, rhythm=rhythm)
    assert interrupt.own_boss(p("mobile hairdresser"))
    assert interrupt.own_boss(p("massage therapist with private clients"))
    assert interrupt.own_boss(p("gardener", "works for herself, mornings"))
    assert interrupt.own_boss(p("personal trainer"))
    assert not interrupt.own_boss(p("ward nurse")) and not interrupt.own_boss(p("office admin"))
