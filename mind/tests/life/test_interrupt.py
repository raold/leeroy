"""Interruptions and coming back ([life] availability above 0, core plan Task 10): after a line of
hers to him in their whisper or her DM, her day can call her away ("brb boss"); while she is away
her answers to him wait (in game through the turn's delay, in her DM through her own time), say
she was away ([talk] shape: the LATE shape, the one excuse in the prompt), and when he left the
conversation quiet she may say she is back. Availability 0 draws nothing. Fakes only: FakeLLM,
FakeClock, a scratch mind.db; invented text."""
import asyncio
import logging
import random
from types import SimpleNamespace

import pytest

from conftest import HUMAN, seed_test_cast
from discord.test_discord_talk import answers, post, rig_for, talk_for
from discord.test_own_time import at, hand, life as set_life
from engine_rig import engine_clock, make_rig, numbered, save_member
from leeroy_mind.discord.store import messages
from leeroy_mind.discord.talk import DEFERRED_KEY, OWED_TICK_S
from leeroy_mind.life import interrupt
from leeroy_mind.life.day import Block, DayEvent
from leeroy_mind.life.interrupt import (BACK_LINES, DM_CAP_S, LINES, Away, Interruption,
                                        draw_back, draw_interruption)
from leeroy_mind.life.presence import Attention, LifePresence, Where, game_cap_s
from leeroy_mind.life.seed import seeded
from leeroy_mind.life.service import NON_OWED_MAX_S, LifeService
from leeroy_mind.life.state import State
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.owed import OwedWatch
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.shape import Shape, ShapeDraw
from leeroy_mind.talk.turns import Turn, TurnKind


@pytest.fixture
def clock():
    return engine_clock()


def _lp(block, *, kids=False):
    return LifePresence(block, Where.BUSY, Attention.GLANCING, DayEvent.NONE, kids)


def rig(store, clock, path, **kw):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", typo_rate=0.0, split_rate=0.0,
                emoticon_rate=0.0, abbreviations={})
    kw.setdefault("availability", 6)
    return make_rig(store, clock, path, **kw)


def force(monkeypatch, *, reason="work", away_s=300.0, line="brb boss", back=""):
    """The first draw is an interruption of `away_s` (within its cap), every later one none;
    `back` is what she says when she is back to a quiet conversation. The calls' caps."""
    calls = []

    def draw(lp, state, availability, rng, *, cap_s=DM_CAP_S):
        calls.append(cap_s)
        return Interruption(reason, min(away_s, cap_s), line) if len(calls) == 1 else None
    monkeypatch.setattr(interrupt, "draw_interruption", draw)
    monkeypatch.setattr(interrupt, "draw_back", lambda rng: back)
    return calls


def said(r, text, *, channel="whisper", to_guid=102):
    """His line in chat (stored and seen), without the engine planning answers to it."""
    ev = Chat(ts=0, channel=channel, from_guid=HUMAN, from_name="Testmage", to_guid=to_guid,
              text=text, map=0, zone=10)
    row = r.store.chat_lines([r.store.insert_chat_line(ev, session_id=None, origin="human")])[0]
    r.world.add_line(row, r.clock.now())
    return row


async def ticking(r, seconds):
    """`seconds` of the mind's ticker: the clock, then her day (LifeService.tick) and the owed
    watch, when the test made one, each second."""
    for _ in range(int(seconds)):
        await r.advance(1)
        r.engine.life.tick(r.clock.now())
        watch = getattr(r.engine, "owed", None)
        if watch is not None:
            watch.tick(r.clock.now())


def watch_of(r):
    app = SimpleNamespace(store=r.store, clock=r.clock, engine=r.engine, world=r.world,
                          scheduler=r.engine.scheduler, discord_talk=None)
    r.engine.owed = OwedWatch(app)
    return r.engine.owed


def whispers(r, guid, since):
    return [(t, s) for t, s in r.sent.timed_says()
            if t >= since and s.guid == guid and s.channel == "whisper"]


def line_calls(r):
    return [c for c in r.chat_calls() if c["format"] is None]


# -- the draw ------------------------------------------------------------------------------------

def test_draw_rates_and_caps():
    work = _lp(Block.WORK)
    hits = [draw_interruption(work, None, 6, seeded("interrupt", "2026-11-18", 101, 600, i))
            for i in range(10_000)]
    got = [h for h in hits if h is not None]
    assert 0.05 <= len(got) / 10_000 <= 0.07
    assert all(h.reason == "work" and h.line in LINES["work"] for h in got)
    assert all(0 < h.away_s <= 2700 for h in got)
    assert max(h.away_s for h in got) > 1000  # lognormal around 600 s
    cap = 0.9 * game_cap_s(6)  # in a whisper
    assert all(draw_interruption(work, None, 6, seeded("interrupt", "n", 1, 1, i),
                                 cap_s=cap) is None
               or draw_interruption(work, None, 6, seeded("interrupt", "n", 1, 1, i),
                                    cap_s=cap).away_s <= cap for i in range(2_000))
    for i in range(2_000):
        rng = seeded("interrupt", "2026-11-18", 101, 600, i)
        assert draw_interruption(work, None, 0, rng) is None
        assert draw_interruption(_lp(Block.ASLEEP), None, 6, rng) is None
        assert draw_interruption(_lp(Block.GYM), None, 10, rng) is None
        assert draw_interruption(_lp(Block.OUT), None, 10, rng) is None


def test_draw_by_block_stress_and_availability():
    def share(lp, availability=6, state=None, n=20_000):
        return sum(draw_interruption(lp, state, availability,
                                     seeded("interrupt", "n", 7, 1, i)) is not None
                   for i in range(n)) / n
    assert 0.07 <= share(_lp(Block.COMMUTE)) <= 0.09
    assert 0.010 <= share(_lp(Block.FREE)) <= 0.020
    assert 0.006 <= share(_lp(Block.GAMING)) <= 0.014
    assert 0.09 <= share(_lp(Block.WORK), availability=10) <= 0.11
    stressed = State(valence=0.0, energy=0.0, stress=1.0, social=0.0, toward_him=0.0)
    calm = State(valence=0.0, energy=0.0, stress=-1.0, social=0.0, toward_him=0.0)
    assert 0.11 <= share(_lp(Block.WORK), state=stressed) <= 0.13
    assert 0.05 <= share(_lp(Block.WORK), state=calm) <= 0.07  # never below her block's chance
    kids = {draw_interruption(_lp(Block.HOME_DUTY, kids=True), None, 10,
                              seeded("interrupt", "n", 7, 2, i)) for i in range(3_000)} - {None}
    home = {draw_interruption(_lp(Block.HOME_DUTY), None, 10,
                              seeded("interrupt", "n", 7, 2, i)) for i in range(3_000)} - {None}
    assert {h.reason for h in kids} == {"kids"} and {h.line for h in kids} == set(LINES["kids"])
    assert {h.reason for h in home} == {"home"}
    free = {draw_interruption(_lp(Block.FREE), None, 10, seeded("interrupt", "n", 7, 3, i))
            for i in range(20_000)} - {None}
    assert {h.reason for h in free} == {"door", "phone", "food"}
    commute = [h for h in (draw_interruption(_lp(Block.COMMUTE), None, 6,
                                             seeded("interrupt", "n", 7, 4, i))
                           for i in range(5_000)) if h is not None]
    assert {h.line for h in commute} == {"gotta drive, brb"}
    longest = max(h.away_s for h in commute)
    assert longest > 900 and longest <= DM_CAP_S


def test_back_is_said_half_the_time():
    lines = [draw_back(seeded("back", "n", 3, 1, i)) for i in range(4_000)]
    assert 0.45 <= sum(1 for x in lines if x) / 4_000 <= 0.55
    assert {x for x in lines if x} == set(BACK_LINES)


# -- in game -------------------------------------------------------------------------------------

def test_brb_then_late_answer_with_excuse(store, clock, tmp_path, caplog, monkeypatch):
    """Her answer to his whisper, then "brb boss" a few seconds later; his next whisper waits
    until she is back and her answer says she was away (the LATE shape), once; he wrote, so no
    "back" is said."""
    caplog.set_level(logging.INFO)
    calls = force(monkeypatch, away_s=300.0)

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("yeah {n}, the vendor was shut"),
                shape=True)
        watch = watch_of(r)
        await r.session()
        await ticking(r, 70)  # past the watch's start grace
        t0 = clock.now()
        await r.human("you around tonight?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        said_ = whispers(r, 102, t0)
        assert [s.text for _, s in said_][-1] == "brb boss" and len(said_) == 2
        assert 2.0 <= said_[1][0] - said_[0][0] <= 30.0
        assert calls == [pytest.approx(0.9 * game_cap_s(6))]  # a whisper's cap
        until = r.engine.life.away_until(102)
        assert until == pytest.approx(said_[1][0] + 300.0, abs=2.0)
        t1 = clock.now()
        await r.human("so what happened with the vendor", channel="whisper", to_guid=102)
        await ticking(r, 420)
        later = whispers(r, 102, t1)
        assert len(later) == 1 and later[0][0] >= until
        user = line_calls(r)[-1]["messages"][-1]["content"]
        assert ("You're only now seeing this (you were away): say so in a few words, then "
                "answer.") in user
        assert user.count("only now seeing") == 1 and "(you're" not in user
        # her answer's shape is drawn once, as it starts composing after the wait (no turn of
        # hers resumes across a brb: review of #343, Task 10 note 1)
        shapes = [m for m in caplog.messages if m.startswith("talk_shape: guid=102")]
        assert len(shapes) == 2 and "shape=late" in shapes[-1] and "owed=1" in shapes[-1]
        assert "life_interrupt: guid=102 reason=work away_s=300" in caplog.text
        assert "life_back: guid=102 said=0" in caplog.text  # he wrote while she was away
        assert "owed_rescue" not in caplog.text and ("whisper", 102) not in watch._game
        assert "brb" not in caplog.text and "vendor" not in caplog.text  # never a text
        r.close()
    asyncio.run(main())


def test_goodbye_hurries_and_logout_carries_while_she_is_away(store, clock, tmp_path, caplog,
                                                              monkeypatch):
    """Review Focus 1 holds through her away: his goodbye sends her waiting answer at once, and
    at his logout an answer still waiting goes to her DM (CARRY_TEXT, its one excuse)."""
    caplog.set_level(logging.INFO)

    async def main():
        force(monkeypatch, away_s=300.0)
        r = rig(store, clock, tmp_path, script=numbered("yeah {n}, the vendor was shut"),
                shape=True)
        await r.session()
        t0 = clock.now()
        await r.human("you around tonight?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        until = r.engine.life.away_until(102)
        assert until > clock.now() + 200
        t1 = clock.now()
        await r.human("so what happened with the vendor", channel="whisper", to_guid=102)
        await ticking(r, 20)
        assert whispers(r, 102, t1) == []
        await r.human("gn, off to bed", channel="whisper", to_guid=102)
        await ticking(r, 20)
        hurried = whispers(r, 102, t1)
        assert len(hurried) == 1 and hurried[0][0] < until
        r.close()
    asyncio.run(main())

    async def logout():
        force(monkeypatch, away_s=300.0)
        r = rig(store, clock, tmp_path / "out", script=numbered("yeah {n}, the vendor was shut"),
                shape=True)
        await r.session()
        await r.human("you around tonight?", channel="whisper", to_guid=104)
        await ticking(r, 60)
        until = r.engine.life.away_until(104)
        assert until > clock.now() + 200
        await r.human("so what happened with the vendor", channel="whisper", to_guid=104)
        await ticking(r, 10)
        t2 = clock.now()
        from leeroy_mind.protocol import Logout
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.engine.end_session()
        assert "life_carried: guid=104" in caplog.text
        await ticking(r, until - clock.now() + 60)
        assert [s for t, s in r.sent.timed_says() if t >= t2 and s.channel == "whisper"] == []
        rows = r.store.conn.execute(
            "SELECT author, ts FROM discord_message WHERE channel = 'dm:104'").fetchall()
        assert len(rows) == 1 and rows[0][1] >= until - 1.0
        user = line_calls(r)[-1]["messages"][-1]["content"]
        assert "logged off before you saw it" in user and "only now seeing" not in user
        r.close()
    asyncio.run(logout())


def test_back_line_when_he_stayed_quiet(store, clock, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    force(monkeypatch, reason="door", line="brb door", away_s=200.0, back="back, sorry")

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("sure {n}, sounds good"))
        await r.session()
        t0 = clock.now()
        await r.human("want to run something later?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        until = r.engine.life.away_until(102)
        assert until > clock.now()
        assert [s.text for _, s in whispers(r, 102, t0)][-1] == "brb door"
        await ticking(r, until - clock.now() - 2)
        assert [s.text for _, s in whispers(r, 102, t0)][-1] == "brb door"  # not back yet
        await ticking(r, 30)
        texts = [s.text for _, s in whispers(r, 102, t0)]
        assert texts[-1] == "back, sorry" and texts.count("back, sorry") == 1
        assert r.engine.life.away_until(102) == 0
        assert "life_back: guid=102 said=1" in caplog.text
        await ticking(r, 120)  # said once
        assert [s.text for _, s in whispers(r, 102, t0)].count("back, sorry") == 1
        r.close()
    asyncio.run(main())


def test_no_back_line_on_the_coin(store, clock, tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.INFO)
    force(monkeypatch, away_s=100.0, back="")

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("sure {n}, sounds good"))
        await r.session()
        t0 = clock.now()
        await r.human("want to run something later?", channel="whisper", to_guid=102)
        await ticking(r, 200)
        assert [s.text for _, s in whispers(r, 102, t0)][-1] == "brb boss"
        assert "life_back: guid=102 said=0" in caplog.text
        r.close()
    asyncio.run(main())


def test_brb_withheld_when_he_writes_first(store, clock, tmp_path, caplog, monkeypatch):
    """He writes again in the seconds before her "brb": she has seen his line, so she stays
    and answers it (no brb, no away): a brb after it would read as her answer to it."""
    caplog.set_level(logging.INFO)
    force(monkeypatch, away_s=300.0)

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("ha {n}, true"))
        await r.session()
        t0 = clock.now()
        await r.human("you around tonight?", channel="whisper", to_guid=102)
        for _ in range(60):
            await r.advance(1)
            if whispers(r, 102, t0):
                break
        assert len(whispers(r, 102, t0)) == 1
        t1 = clock.now()
        await r.human("also did you see the auction", channel="whisper", to_guid=102)
        await ticking(r, 90)
        texts = [s.text for _, s in whispers(r, 102, t0)]
        assert "brb boss" not in texts
        assert r.engine.life.away_until(102) == 0
        assert whispers(r, 102, t1)  # his line is answered as usual
        assert "life_interrupt_dropped: guid=102 reason=his_line" in caplog.text
        r.close()
    asyncio.run(main())


def test_only_her_line_to_him_in_a_live_whisper_or_dm(store, clock, tmp_path, monkeypatch):
    """Drawn only after her line to him in their whisper or her DM with his last line to her
    under 300 s old, outside his raid's fight, never on a template, a rescue or a carried DM,
    nor while an answer of hers to him is on its way."""
    calls = []

    def draw(lp, state, availability, rng, *, cap_s=DM_CAP_S):
        calls.append(cap_s)
        return None
    monkeypatch.setattr(interrupt, "draw_interruption", draw)
    r = rig(store, clock, tmp_path)
    life = r.engine.life

    def after(turn, **meta):
        life.after_line(r.engine, turn, TurnMeta(r.engine._new_id(), **meta))
        return len(calls)

    w = said(r, "you around tonight?")
    whisper = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=HUMAN, addressee=HUMAN,
                   trigger_line=w.id)
    assert after(whisper, answers_human=True, whisper_reply=True) == 1
    from leeroy_mind.discord import store as dstore
    dstore.post(store, channel="dm:102", author=HUMAN, origin="human", text="hey", now=clock.now())
    dm = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN)
    assert after(dm, answers_human=True) == 2
    assert after(Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN),
                 answers_human=True) == 2
    assert after(Turn(TurnKind.REPLY_HUMAN, 102, "officer", addressee=HUMAN),
                 answers_human=True) == 2
    assert after(Turn(TurnKind.REPLY_HUMAN, 102, "gdm:1", addressee=HUMAN),
                 answers_human=True) == 2
    assert after(Turn(TurnKind.REPLY_HUMAN, 103, "whisper", to_guid=HUMAN, addressee=HUMAN),
                 answers_human=True, whisper_reply=True) == 2  # he never whispered 103
    templ = Turn(TurnKind.WHISPER, 102, "whisper", to_guid=HUMAN, template="back")
    assert after(templ) == 2
    assert after(whisper, answers_human=True, whisper_reply=True, owed_rescue=True) == 2
    assert after(dm, answers_human=True, why="reply:carried") == 2
    real_phase = r.raid.phase
    for phase in (RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF):
        r.raid.phase = lambda now, phase=phase: phase
        assert after(whisper, answers_human=True, whisper_reply=True) == 2
    r.raid.phase = real_phase
    waiting = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=HUMAN, addressee=HUMAN,
                   trigger_line=w.id, delay=60.0)

    async def with_waiting():
        task = r.engine._queue([(waiting, TurnMeta(r.engine._new_id(), answers_human=True,
                                                   whisper_reply=True))])
        await asyncio.sleep(0)
        n = after(whisper, answers_human=True, whisper_reply=True)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return n
    assert r.sync(with_waiting()) == 2  # her answer to him still on its way: she stays
    assert after(whisper, answers_human=True, whisper_reply=True) == 3
    r.run(301, step=30)  # his last line to her is stale now
    assert after(whisper, answers_human=True, whisper_reply=True) == 3
    assert after(dm, answers_human=True) == 3
    r.close()


def test_availability_zero_draws_nothing(store, clock, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(interrupt, "draw_interruption",
                        lambda *a, **k: calls.append(1) or Interruption("work", 99.0, "brb"))
    r = rig(store, clock, tmp_path, availability=0)
    w = said(r, "you around tonight?")
    turn = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=HUMAN, addressee=HUMAN,
                trigger_line=w.id)
    r.engine.life.after_line(r.engine, turn, TurnMeta("t1", answers_human=True,
                                                      whisper_reply=True))
    assert calls == [] and r.engine.life.away_until(102) == 0
    r.close()


def test_at_most_once_per_member_per_half_hour(store, clock, tmp_path, monkeypatch):
    seen = []

    def draw(lp, state, availability, rng, *, cap_s=DM_CAP_S):
        seen.append(1)
        return Interruption("door", 60.0, "brb door") if len(seen) == 1 else None
    monkeypatch.setattr(interrupt, "draw_interruption", draw)
    monkeypatch.setattr(interrupt, "draw_back", lambda rng: "")

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("ok {n} then"))
        await r.session()
        await r.human("you around tonight?", channel="whisper", to_guid=102)
        await ticking(r, 140)  # her answer, her brb, back after a minute
        assert len(seen) == 1 and r.engine.life.away_until(102) == 0
        await r.human("cool, what are you up to", channel="whisper", to_guid=102)
        await ticking(r, 60)
        assert len(seen) == 1  # not again within 1800 s
        await ticking(r, 1800)
        await r.human("still there?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        assert len(seen) == 2
        r.close()
    asyncio.run(main())


def test_a_refused_brb_still_steps_her_away(store, clock, tmp_path, caplog, monkeypatch):
    """The guards refuse her brb: she is still away, without a line (core plan Task 10), and so
    says no back line when she is back to a conversation he left quiet."""
    caplog.set_level(logging.INFO)
    force(monkeypatch, away_s=200.0, back="back, sorry")

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("sure {n}, sounds good"))
        real = r.engine.say_as

        async def refused(guid, channel, text, **kw):
            if text == "brb boss":
                return False
            return await real(guid, channel, text, **kw)
        r.engine.say_as = refused
        await r.session()
        t0 = clock.now()
        await r.human("want to run something later?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        until = r.engine.life.away_until(102)
        assert until > clock.now()
        assert len(whispers(r, 102, t0)) == 1  # her answer, and no brb
        assert "life_interrupt: guid=102 reason=work away_s=200 said=0" in caplog.text
        await ticking(r, until - clock.now() + 30)
        assert len(whispers(r, 102, t0)) == 1 and r.engine.life.away_until(102) == 0
        assert "life_back: guid=102 said=0" in caplog.text
        r.close()
    asyncio.run(main())


def test_a_whisper_brb_never_goes_to_him_offline(store, clock, tmp_path, caplog, monkeypatch):
    """He leaves the game in the seconds before her brb: no whisper goes to an offline player,
    and she is still away (said=0), with no back line."""
    caplog.set_level(logging.INFO)
    force(monkeypatch, away_s=200.0, back="back")

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("sure {n}, sounds good"))
        await r.session()
        t0 = clock.now()
        await r.human("want to run something later?", channel="whisper", to_guid=102)
        for _ in range(60):
            await r.advance(1)
            if whispers(r, 102, t0):
                break
        assert len(whispers(r, 102, t0)) == 1 and 102 in r.engine.life._stepping
        r.world.human_online = False
        await ticking(r, 30)
        assert [s for t, s in r.sent.timed_says() if t >= t0 and s.channel == "whisper"
                and s.text in ("brb boss", "back")] == []
        assert r.engine.life.away_until(102) > clock.now()
        assert "life_interrupt: guid=102 reason=work away_s=200 said=0" in caplog.text
        await ticking(r, 200)
        assert len(whispers(r, 102, t0)) == 1
        r.close()
    asyncio.run(main())


def test_his_urgent_whisper_brings_her_back(store, clock, tmp_path, caplog, monkeypatch):
    """His urgent whisper while she is away is answered at once (it says she was away) and ends
    her away: no back line after it, and his next whisper is answered without that excuse."""
    caplog.set_level(logging.INFO)
    force(monkeypatch, away_s=300.0, back="ok back")

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("yeah {n}, the vendor was shut"),
                shape=True)
        await r.session()
        await r.human("you around tonight?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        until = r.engine.life.away_until(102)
        assert until > clock.now() + 200
        t1 = clock.now()
        await r.human("are you there? need help with something", channel="whisper",
                      to_guid=102)
        await ticking(r, 40)
        got = whispers(r, 102, t1)
        assert len(got) == 1 and got[0][0] < until - 100
        assert "(you were away)" in line_calls(r)[-1]["messages"][-1]["content"]
        assert r.engine.life.away_until(102) == 0
        assert "life_back: guid=102 said=0" in caplog.text
        t2 = clock.now()
        await r.human("thanks, all sorted", channel="whisper", to_guid=102)
        await ticking(r, game_cap_s(6) + 60)
        texts = [s.text for _, s in whispers(r, 102, t2)]
        assert len(texts) == 1 and "ok back" not in texts
        assert "you were away" not in line_calls(r)[-1]["messages"][-1]["content"]
        r.close()
    asyncio.run(main())


def test_a_turn_left_on_read_draws_no_interruption(store, clock, tmp_path, monkeypatch):
    """A turn left on read is dropped before any interruption is drawn for it (review of #343,
    Task 10 note 2): only a line she said draws one."""
    calls = force(monkeypatch)

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("sure {n}, sounds good"), shape=True)

        def leave(turn, meta, p, now, *, voiced=False):
            turn.shape = ShapeDraw(Shape.LEAVE, 0, 0, None)
        monkeypatch.setattr(r.engine, "_draw_shape", leave)
        await r.session()
        t0 = clock.now()
        await r.human("want to run something later?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        assert whispers(r, 102, t0) == [] and calls == []
        assert "left_on_read" in [u["dropped"] for u in r.utterances()]
        assert r.engine.life.away_until(102) == 0
        r.close()
    asyncio.run(main())


def test_she_stays_while_an_answer_to_him_is_on_its_way(store, clock, tmp_path):
    """The brb's barrier (LifeService._stays): an answer of hers to him on its way keeps her
    (her own brb or back, a template, answers nothing), as do his line since hers and his
    raid's fight."""
    r = rig(store, clock, tmp_path)
    life, t = r.engine.life, clock.now()
    assert life._stays(r.engine, 102, HUMAN, t) == ""

    async def queued(turn):
        task = r.engine._queue([(turn, TurnMeta(r.engine._new_id(), answers_human=True))])
        await asyncio.sleep(0)
        why = life._stays(r.engine, 102, HUMAN, t)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return why
    brb = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN,
               template="brb", delay=60.0)
    reply = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=HUMAN, addressee=HUMAN,
                 delay=60.0)
    assert r.sync(queued(brb)) == ""
    assert r.sync(queued(reply)) == "answering"
    real_phase = r.raid.phase
    r.raid.phase = lambda now: RaidPhase.COMBAT
    assert life._stays(r.engine, 102, HUMAN, t) == "raid"
    r.raid.phase = real_phase
    said(r, "still on for later?")
    assert life._stays(r.engine, 102, HUMAN, t) == "his_line"
    r.close()


def test_answer_delay_waits_out_her_away(store, clock, tmp_path):
    r = rig(store, clock, tmp_path)
    life = r.engine.life
    now = clock.now()
    life._away[102] = Away(since=now, until=now + 200.0, channel="whisper", reason="work",
                           night="n", minute=0)
    assert life.away_until(102) == now + 200.0 and life.away_until(103) == 0
    for seed in range(20):
        assert life.answer_delay(102, rng=random.Random(seed), owed=True) >= 200.0
        d = life.answer_delay(102, rng=random.Random(seed), owed=False)
        assert d is not None and d >= 200.0
    life._away[102].until = now + NON_OWED_MAX_S + 60.0
    assert all(life.answer_delay(102, rng=random.Random(s), owed=False) is None
               for s in range(20))  # she would only see it past NON_OWED_MAX_S: missed
    life._away[102].until = now + 5 * game_cap_s(6)  # an away from her DM, in game
    assert all(life.answer_delay(102, rng=random.Random(s), owed=True) <= game_cap_s(6)
               for s in range(20))
    r.close()


# -- in her DM -----------------------------------------------------------------------------------

def _dm_rig(store, tmp_path, **kw):
    rig = rig_for(store, at(2026, 11, 18, 20), tmp_path,  # a Wednesday, 20:00 NY: free
                  script=numbered("ah {n}, not yet"), **kw)
    set_life(rig)
    return rig


def _run(rig, talk, seconds, *, watch=None):
    async def go():
        for _ in range(int(seconds)):
            await rig.advance(1)
            now = rig.clock.now()
            talk.tick(now)
            rig.engine.life.tick(now)
            if watch is not None:
                watch.tick(now)
    rig.sync(go())


def _hers(rig, channel="dm:101"):
    return [m for m in messages(rig.store.conn, channel) if m["origin"] == "mind"]


def test_dm_away_moves_the_deferral(store, tmp_path, caplog, monkeypatch):
    """Her DM answer, then "brb boss" in the DM; his next post waits until she is back (meta
    discord:dm:deferred set, the owed watch holding it for her own time), then is answered."""
    caplog.set_level(logging.INFO)
    calls = force(monkeypatch, away_s=600.0)
    rig = _dm_rig(store, tmp_path)
    talk = talk_for(rig)
    watch = OwedWatch(SimpleNamespace(store=rig.store, clock=rig.clock, engine=rig.engine,
                                      world=rig.world, scheduler=rig.engine.scheduler,
                                      discord_talk=talk))
    first = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, first)
    _run(rig, talk, 90, watch=watch)
    assert len(answers(rig, "dm:101", first)) == 1
    assert _hers(rig)[-1]["text"] == "brb boss" and _hers(rig)[-1]["reply_to"] is None
    assert calls == [DM_CAP_S]
    until = rig.engine.life.away_until(101)
    assert until == pytest.approx(_hers(rig)[-1]["ts"] + 600.0, abs=2.0)
    second = post(rig, "dm:101", "did you get a new one yet")
    hand(rig, talk, second)
    assert store.get_meta(DEFERRED_KEY.format(guid=101)) == str(second)
    assert talk.owes(101)
    _run(rig, talk, until - rig.clock.now() + 20, watch=watch)
    assert answers(rig, "dm:101", second) == []
    assert "reason=own_time" in caplog.text and "owed_rescue" not in caplog.text
    _run(rig, talk, 160, watch=watch)
    got = answers(rig, "dm:101", second)
    assert len(got) == 1 and got[0]["ts"] >= until + 30.0
    assert store.get_meta(DEFERRED_KEY.format(guid=101)) is None
    assert "kettle" not in caplog.text and "brb" not in caplog.text
    rig.close()


@pytest.mark.parametrize("shape", [True, False])
def test_her_late_dm_answer_says_she_was_away_once(store, tmp_path, monkeypatch, shape):
    """[talk] shape on: the LATE shape says it and the own-time late note is left out; off: the
    late note says she was away."""
    force(monkeypatch, away_s=900.0)
    rig = _dm_rig(store, tmp_path, shape=shape)
    talk = talk_for(rig)
    first = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, first)
    _run(rig, talk, 90)
    until = rig.engine.life.away_until(101)
    assert until > rig.clock.now()
    second = post(rig, "dm:101", "did you get a new one yet")
    hand(rig, talk, second)
    _run(rig, talk, until - rig.clock.now() + 150)
    assert len(answers(rig, "dm:101", second)) == 1
    user = line_calls(rig)[-1]["messages"][-1]["content"]
    if shape:
        assert "You're only now seeing this (you were away)" in user
        assert "This is your private Discord DM, not the game" in user
        assert "ago while you were" not in user
    else:
        assert "ago while you were away; you are only now seeing it" in user
        assert "You're only now seeing this" not in user
    rig.close()


def test_an_urgent_dm_cuts_through_her_away(store, tmp_path, monkeypatch):
    force(monkeypatch, away_s=900.0)
    rig = _dm_rig(store, tmp_path)
    talk = talk_for(rig)
    first = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, first)
    _run(rig, talk, 90)
    assert rig.engine.life.away_until(101) > rig.clock.now()
    urgent = post(rig, "dm:101", "are you there? need help with something")
    hand(rig, talk, urgent)
    _run(rig, talk, 90)
    assert len(answers(rig, "dm:101", urgent)) == 1
    rig.close()


def test_restart_forgets_away(store, tmp_path, monkeypatch):
    """Aways live in memory only: a new LifeService has none, and his post deferred by one is
    answered by today's own-time rules (she is online and talking with him: at once)."""
    force(monkeypatch, away_s=900.0)
    rig = _dm_rig(store, tmp_path)
    talk = talk_for(rig)
    first = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, first)
    _run(rig, talk, 90)
    second = post(rig, "dm:101", "did you get a new one yet")
    hand(rig, talk, second)
    assert store.get_meta(DEFERRED_KEY.format(guid=101)) == str(second)
    fresh = LifeService(store=rig.store, clock=rig.clock, personas=rig.engine.personas,
                        world=rig.world, cfg=rig.engine.cfg, engine=rig.engine)
    assert all(fresh.away_until(g) == 0 for g in (101, 102, 103, 104, 105))
    rig.engine.life = fresh
    restarted = talk_for(rig)
    _run(rig, restarted, OWED_TICK_S + 60)
    assert len(answers(rig, "dm:101", second)) == 1
    assert store.get_meta(DEFERRED_KEY.format(guid=101)) is None
    rig.close()
