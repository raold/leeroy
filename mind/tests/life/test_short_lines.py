"""Length on his short lines (talk/shape.py dm_words_for, self-play audit 2026-11-17): a DM line
of his of 5 words or fewer caps her at about 12 words, 6-12 words at about 25, the mirror formula
above that; a short line that asks her for something keeps the middle cap; an owed rescue draws a
shape like any answer; never under 3 words. Invented lines only."""
import logging
import random
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import HUMAN
from discord.test_dm_prompt import answer, answers, rig_for, talk_for
from engine_rig import numbered
from leeroy_mind.personas.schema import Typing
from leeroy_mind.talk.shape import (MID_CAP, MID_HIS, SHORT_CAP, SHORT_HIS, dm_words_for,
                                    short_cap)
from leeroy_mind.testing.fake_clock import FakeClock
from life.test_shape import T
from test_sofar import his_dm


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)


def draws(his, mirror=5, cap=200, **kw):
    return [dm_words_for(his, T, mirror, cap, random.Random(i), **kw) for i in range(1500)]


def test_caps_by_his_length():
    assert (SHORT_HIS, SHORT_CAP, MID_HIS, MID_CAP) == (5, 12, 12, 25)
    for his in (1, 2, 5):
        w = draws(his)
        assert max(w) <= 12 and min(w) >= 3, his
        assert sum(w) / len(w) <= 12
    for his in (6, 9, 12):
        w = draws(his)
        assert max(w) <= 25 and min(w) >= 3, his
    assert max(draws(13)) > 25 and max(draws(40)) > 60  # the mirror formula above that
    assert max(draws(4, asks=True)) <= 25 and max(draws(4, asks=True)) > 12  # room to answer
    assert draws(3, mirror=0) == [200] * 1500  # dm_mirror 0: today's DM, as full as she likes
    assert max(draws(3, cap=8)) <= 8  # never over the owner's dm_words
    assert draws(0)[:50] == [dm_words_for(0, T, 5, 200, random.Random(i)) for i in range(50)]
    assert short_cap(0) == 0 and short_cap(5) == 12 and short_cap(5, True) == 25
    assert short_cap(12) == 25 and short_cap(13) == 0


def test_a_capped_draw_is_spread_not_one_number():
    assert len(set(draws(3))) >= 3 and len(set(draws(9))) >= 5


def test_her_dm_answer_to_a_two_word_line_is_asked_short(store, clock, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rig = rig_for(store, clock, tmp_path, script=numbered("ha {n}, fair"), shape=True)
    talk = talk_for(rig)
    mid = his_dm(rig, "lol ok")
    answer(rig, talk, mid)
    assert answers(rig, "dm:102", mid)
    shapes = [m for m in caplog.messages if m.startswith("talk_shape: guid=102")]
    assert shapes and all(int(m.split("words=")[1].split()[0]) <= 12 for m in shapes)
    mid = his_dm(rig, "describe it properly then")  # short, but it asks her for something
    answer(rig, talk, mid)
    words = int([m for m in caplog.messages if m.startswith("talk_shape")][-1]
                .split("words=")[1].split()[0])
    assert words <= 25
    rig.close()


def test_an_owed_rescue_draws_a_shape_like_any_answer(store, clock, tmp_path):
    from leeroy_mind.discord.transport import DiscordTarget
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn, TurnKind
    rig = rig_for(store, clock, tmp_path, shape=True)
    mid = his_dm(rig, "you there")
    turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)
    meta = TurnMeta("rescue", answers_human=True, why="discord:reply", owed_answer=True,
                    owed_rescue=True, discord=DiscordTarget(mid, None))
    assert not rig.engine._want_eligible(turn, meta)  # no want for the watchdog's rescue
    assert rig.engine._shapes(turn, meta)  # ...but a shape, so its length follows his line
    rig.engine._draw_shape(turn, meta, rig.engine.personas.get(102), rig.clock.now())
    assert turn.shape is not None and 3 <= turn.shape.words <= 12
    plain = replace(meta, owed_rescue=False) if hasattr(meta, "__dataclass_fields__") else meta
    assert rig.engine._shapes(turn, plain)
    rig.close()
