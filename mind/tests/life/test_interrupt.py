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
from discord.test_discord_talk import answers, member_post, post, rig_for, talk_for
from discord.test_own_time import at, hand, life as set_life
from engine_rig import engine_clock, make_rig, numbered, save_member
from leeroy_mind.banter.config import BanterConfig
from leeroy_mind.banter.service import Banter
from leeroy_mind.discord import layout
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.store import messages
from leeroy_mind.discord.talk import DEFERRED_KEY, OWED_TICK_S
from leeroy_mind.discord.transport import PAIR_LINE_TURN
from leeroy_mind.health import OWNER_LATE_MARGIN_S, _own_time_due
from leeroy_mind.life import interrupt
from leeroy_mind.life.day import Block, DayEvent
from leeroy_mind.life.interrupt import (BACK_FIND_S, BACK_LINES, DM_CAP_S, LINES, OWN_BOSS_WORK,
                                        TRANSIT_COMMUTE, Away, Interruption, draw_back,
                                        draw_interruption)
from leeroy_mind.life.presence import Attention, LifePresence, Where, game_cap_s
from leeroy_mind.life.seed import seeded
from leeroy_mind.life.service import (AWAY_KEY, AWAY_WEIGHT, NON_OWED_MAX_S, LifeService,
                                      parse_away)
from leeroy_mind.life.state import State
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.owed import Owed, OwedWatch, count_unanswered
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.shape import Shape, ShapeDraw
from leeroy_mind.talk.style import Styled
from leeroy_mind.talk.turns import Turn, TurnKind


@pytest.fixture
def clock():
    return engine_clock()


@pytest.fixture(autouse=True)
def _as_drawn(monkeypatch):
    """Interruptions as her day draws them, without the owner's promptness rules ([life]
    dm_wait_cap_s = 0, and a brb straight after his line): those have their own tests in
    test_prompt_dms.py."""
    from leeroy_mind.life import config as life_config
    monkeypatch.setattr(life_config, "dm_wait_cap", lambda: 0)
    monkeypatch.setattr(interrupt, "MIN_AFTER_HIS_S", 0.0)


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

    def draw(lp, state, availability, rng, *, cap_s=DM_CAP_S, **_):
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


def put_away(r, guid=102, seconds=300.0, channel="whisper", *, since=None):
    """Her away, set as _step_away sets it (a test's shortcut past the draw)."""
    now = r.clock.now() if since is None else since
    a = Away(since=now, until=now + seconds, channel=channel, reason="work", night="n",
             minute=0)
    r.engine.life._away[guid] = a
    return a


def persona(**rl):
    return SimpleNamespace(real_life=SimpleNamespace(**{"job": "", "rhythm": "", "life_thread": "",
                                                        "household": "", **rl}))


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
    assert {h.line for h in commute} == set(LINES["commute"])
    longest = max(h.away_s for h in commute)
    assert longest > 900 and longest <= DM_CAP_S


def test_back_is_said_half_the_time():
    lines = [draw_back(seeded("back", "n", 3, 1, i)) for i in range(4_000)]
    assert 0.45 <= sum(1 for x in lines if x) / 4_000 <= 0.55
    assert {x for x in lines if x} == set(BACK_LINES)


def test_her_lines_fit_her():
    """Review-353 P3-2: her own boss never says "brb boss", one with no car never has to drive,
    and every block has at least three lines."""
    for reason in ("work", "kids", "home", "commute"):
        assert len(set(LINES[reason])) >= 3
    assert len(set(LINES["door"] + LINES["phone"] + LINES["food"])) >= 3
    assert len(set(OWN_BOSS_WORK)) >= 3 and len(set(TRANSIT_COMMUTE)) >= 3
    assert "brb boss" not in OWN_BOSS_WORK and not any("driv" in x for x in TRANSIT_COMMUTE)
    boss = {draw_interruption(_lp(Block.WORK), None, 10, seeded("interrupt", "n", 7, 5, i),
                              boss=True) for i in range(5_000)} - {None}
    assert {h.line for h in boss} == set(OWN_BOSS_WORK) and {h.reason for h in boss} == {"work"}
    bus = {draw_interruption(_lp(Block.COMMUTE), None, 10, seeded("interrupt", "n", 7, 6, i),
                             car=False) for i in range(3_000)} - {None}
    assert {h.line for h in bus} == set(TRANSIT_COMMUTE)
    assert interrupt.own_boss(persona(job="runs her own little bakery"))
    assert interrupt.own_boss(persona(job="self-employed illustrator"))
    assert not interrupt.own_boss(persona(job="ward nurse", rhythm="runs to the gym after shifts"))
    assert not interrupt.own_boss(SimpleNamespace())
    assert not interrupt.own_boss(persona(rhythm="runs her kids to nursery"))
    assert interrupt.own_boss(persona(job="runs her nursery"))
    assert interrupt.drives(persona(job="bus driver"))
    assert not interrupt.drives(persona(rhythm="takes the bus into town for work"))
    assert not interrupt.drives(persona(household="small flat by the metro"))
    assert interrupt.drives(persona(job="trains rescue dogs")) and interrupt.drives(persona())


def test_a_draw_past_its_cap_lands_under_it():
    """Review-353 P3-1: an away drawn past its cap is drawn again under it (40-100% of the cap),
    so whisper aways do not pile up at exactly the cap."""
    cap = 0.9 * game_cap_s(6)
    got = [h.away_s for h in (draw_interruption(_lp(Block.COMMUTE), None, 6,
                                                seeded("interrupt", "n", 9, 1, i), cap_s=cap)
                              for i in range(20_000)) if h is not None]
    assert len(got) > 1000 and max(got) <= cap and min(got) > 0
    assert sum(1 for x in got if x >= cap - 1.0) / len(got) < 0.02
    assert sum(1 for x in got if x < 0.7 * cap) / len(got) > 0.3


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

    def draw(lp, state, availability, rng, *, cap_s=DM_CAP_S, **_):
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
    mid = dstore.post(store, channel="dm:102", author=HUMAN, origin="human", text="hey",
                      now=clock.now())
    dstore.post(store, channel="dm:102", author=102, origin="mind", text="hey back",
                reply_to=mid, now=clock.now())  # her answer to it, as after_line follows one
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

    def draw(lp, state, availability, rng, *, cap_s=DM_CAP_S, **_):
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


def test_nothing_of_hers_goes_out_while_she_is_away(store, clock, tmp_path):
    """Review-353 P2-1: while she is away nothing of hers he can see goes out but her answers
    to him: no guild line, no banter cast, no opener pool; a line of his still finds her among
    those who may answer it (her answer waits), and someone else is likelier to take it."""
    r = rig(store, clock, tmp_path)
    e = r.engine
    weight = e.life.weight(102)
    assert 102 in e._candidates()
    put_away(r, 102)
    assert e._stepped_away(102) and not e._stepped_away(103)
    assert 102 not in e._candidates() and 102 in e._candidates(stepped_away=True)
    row = said(r, "anyone up for a run later", channel="guild", to_guid=0)
    assert 102 in e._reply_candidates(row, "guild", clock.now())[1]
    assert e.life.weight(102) == pytest.approx(weight * AWAY_WEIGHT)
    assert r.speak_turn(Turn(TurnKind.CONTINUE, 102, "guild")) == []
    assert "stepped_away" in [u["dropped"] for u in r.utterances()]
    app = SimpleNamespace(engine=e, world=r.world, personas=e.personas, store=store,
                          clock=r.clock, rng=random.Random(4), human_guid=HUMAN, arbiter=None,
                          is_backlog=lambda ev: False)
    banter = Banter(app, BanterConfig(True, 2))
    assert banter.candidates("guild") and 102 not in banter.candidates("guild")
    assert 102 not in banter.candidates("discord:general")
    r.close()


def test_the_answer_he_is_owed_goes_to_one_who_is_there(store, clock, tmp_path):
    """Review-353 P3-3: of the members planned for his guild line, one who stepped away is never
    the one he is owed while another is there."""
    from leeroy_mind.talk.select import ReplyTurn
    r = rig(store, clock, tmp_path)
    put_away(r, 102, seconds=600.0)
    row = said(r, "anyone up for a run later", channel="guild", to_guid=0)
    planned = [ReplyTurn(102, "a", TurnKind.REPLY_HUMAN, "guild", "guild"),
               ReplyTurn(103, "b", TurnKind.REPLY_HUMAN, "guild", "guild")]
    out = r.engine._life_delays(row, planned, whisper=False, called=[], push=None)
    by = {rt.speaker: d for rt, d, _ in out}
    assert by[103] <= game_cap_s(6)  # the owed one
    assert 102 not in by or by[102] >= 600.0 - 1  # hers waits for her, or she missed it
    r.close()


def test_she_stays_and_says_no_back_in_a_quiet(store, clock, tmp_path, monkeypatch):
    """Review-353 P2-2 and P3-4: no brb and no back line in a boss fight of his group, while he
    is AFK or in his login's quiet, nor a back line while an answer of hers to him is on its
    way anywhere (it says she was away: one excuse)."""
    r = rig(store, clock, tmp_path)
    e, life, t = r.engine, r.engine.life, clock.now()
    a = Away(since=t - 100.0, until=t - 1.0, channel="dm:102", reason="door", night="n",
             minute=0, back="back")
    assert life._stays(e, 102, HUMAN, t) == "" and not life._back_held(e, 102, a)
    monkeypatch.setattr(e, "_in_boss_fight", lambda now: True)
    assert life._stays(e, 102, HUMAN, t) == "boss" and life._back_held(e, 102, a)
    monkeypatch.setattr(e, "_in_boss_fight", lambda now: False)
    r.world.human_afk = True
    assert life._stays(e, 102, HUMAN, t) == "afk" and life._back_held(e, 102, a)
    r.world.human_afk = False
    monkeypatch.setattr(e, "_login_quiet", lambda now: True)
    assert life._stays(e, 102, HUMAN, t) == "login" and life._back_held(e, 102, a)
    monkeypatch.setattr(e, "_login_quiet", lambda now: False)

    async def queued():
        turn = Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN, delay=60.0)
        task = e._queue([(turn, TurnMeta(e._new_id(), answers_human=True))])
        await asyncio.sleep(0)
        held = life._back_held(e, 102, a)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return held
    assert r.sync(queued())
    assert not life._back_held(e, 102, a)
    r.close()


def test_a_failed_spawn_never_leaves_her_stepping(store, clock, tmp_path, monkeypatch):
    """Review-353 P3-7: a brb that could not be spawned leaves her free to draw again."""
    force(monkeypatch)
    r = rig(store, clock, tmp_path)
    w = said(r, "you around tonight?")
    turn = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=HUMAN, addressee=HUMAN,
                trigger_line=w.id)

    def broken(coro):
        coro.close()
        raise RuntimeError("no loop")
    monkeypatch.setattr(r.engine, "_spawn", broken)
    r.engine.life.after_line(r.engine, turn, TurnMeta("t1", answers_human=True,
                                                      whisper_reply=True))
    assert 102 not in r.engine.life._stepping and r.engine.life.away_until(102) == 0
    r.close()


def test_no_excuse_for_a_line_just_before_she_was_back(store, clock, tmp_path):
    """Review-353 P3-5: his line written in her away's last 20 s has no lateness to excuse."""
    r = rig(store, clock, tmp_path)
    life, now = r.engine.life, clock.now()
    put_away(r, 102, seconds=400.0, since=now - 300.0)  # back at now + 100
    assert life.away_between(102, now, now + 200.0)  # 100 s before she is back
    assert not life.away_between(102, now + 85.0, now + 200.0)  # 15 s before
    assert not life.away_between(102, now + 150.0, now + 200.0)  # after
    r.close()


def test_her_brb_is_typed_her_way(store, clock, tmp_path, monkeypatch):
    """Review-353 P3-2: her brb goes through her own typing (Pipeline._style), one line."""
    force(monkeypatch, away_s=200.0)

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("sure {n}, sounds good"))
        real = r.engine._style

        def style(turn, p, text, **kw):
            if turn.template == "brb boss":
                return [Styled("BRB boss", text), Styled("never sent", text)]
            return real(turn, p, text, **kw)
        monkeypatch.setattr(r.engine, "_style", style)
        await r.session()
        t0 = clock.now()
        await r.human("want to run something later?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        texts = [s.text for _, s in whispers(r, 102, t0)]
        assert texts[-1] == "BRB boss" and "never sent" not in texts
        assert r.engine.life.away_until(102) > clock.now()
        r.close()
    asyncio.run(main())


def test_a_mind_stop_during_her_away_keeps_his_whisper_owed(store, clock, tmp_path, caplog,
                                                            monkeypatch):
    """Review-353 P3-8: the mind stops while her answer to his whisper waits out her away. The
    owed watch keeps it for the next start, which has no away (in memory only), answers it, and
    draws no interruption for that rescue."""
    import json
    from leeroy_mind.talk.owed import PERSIST_KEY
    caplog.set_level(logging.INFO)
    calls = force(monkeypatch, away_s=300.0)

    async def main():
        r = rig(store, clock, tmp_path, script=numbered("yeah {n}, the vendor was shut"))
        watch_of(r)
        await r.session()
        await ticking(r, 70)
        await r.human("you around tonight?", channel="whisper", to_guid=102)
        await ticking(r, 60)
        assert r.engine.life.away_until(102) > clock.now() + 200
        line = await r.human("so what happened with the vendor", channel="whisper", to_guid=102)
        await ticking(r, 10)
        r.engine.stopping = True  # MindApp._shutdown, him online
        await r.engine.end_session()
        assert json.loads(store.get_meta(PERSIST_KEY)) == [{"line": line.id, "guid": 102,
                                                            "dm": 0}]
        r2 = make_rig(store, clock, tmp_path, script=numbered("sorry {n}, got pulled away"),
                      availability=6)
        w2 = watch_of(r2)
        assert r2.engine.life.away_until(102) == 0
        t1 = clock.now()
        for _ in range(80):
            await r2.advance(1)
            r2.engine.life.tick(r2.clock.now())
            w2.tick(r2.clock.now())
        assert len(whispers(r2, 102, t1)) == 1 and ("whisper", 102) not in w2._game
        assert len(calls) == 1  # the rescue drew nothing
    asyncio.run(main())


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
    assert store.conn.execute("SELECT turn FROM discord_message WHERE id = ?",
                              (_hers(rig)[-1]["id"],)).fetchone()[0].startswith(PAIR_LINE_TURN)
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


def test_a_dm_post_still_owed_keeps_her(store, tmp_path, caplog, monkeypatch):
    """Review-353 P1-1: his second post's answer was lost (never handed over) when her answer
    to his first landed. Since 2026-11-17 (self-play audit 3) a post of his that arrives before
    she composes is answered by the same reply: one answer, built with both posts, replying to
    the newest, and nothing is left for the owed watch to rescue."""
    caplog.set_level(logging.INFO)
    calls = force(monkeypatch, away_s=600.0)
    rig = _dm_rig(store, tmp_path)
    talk = talk_for(rig)
    watch = OwedWatch(SimpleNamespace(store=rig.store, clock=rig.clock, engine=rig.engine,
                                      world=rig.world, scheduler=rig.engine.scheduler,
                                      discord_talk=talk))
    rig.engine.owed = watch
    first = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, first)
    second = post(rig, "dm:101", "did you get a new one yet")
    _run(rig, talk, 90, watch=watch)
    assert answers(rig, "dm:101", first) == [] and len(answers(rig, "dm:101", second)) == 1
    user = line_calls(rig)[0]["messages"][-1]["content"]
    assert "wrote 2 messages you have not answered yet" in user
    assert "1. the kettle finally died this morning" in user
    assert "2. did you get a new one yet" in user
    assert "discord_dm_coalesced: guid=101" in caplog.text
    hand(rig, talk, second)  # the desk hands it over late: already answered, no second reply
    _run(rig, talk, 900, watch=watch)
    assert len(answers(rig, "dm:101", second)) == 1
    assert "owed_rescue: medium=discord" not in caplog.text
    rig.close()


def test_a_pair_line_answers_nothing(store, tmp_path):
    """Review-353 P1-1: her brb or back in her DM (a pair line) never counts as an answer to his
    post, for the owed watch, health's count or her own time; her real answer does."""
    rig = _dm_rig(store, tmp_path)
    talk = talk_for(rig)
    watch = OwedWatch(SimpleNamespace(store=rig.store, clock=rig.clock, engine=rig.engine,
                                      world=rig.world, scheduler=rig.engine.scheduler,
                                      discord_talk=talk))
    t0 = rig.clock.now()
    mid = post(rig, "dm:101", "the kettle finally died this morning")
    store.set_meta(DEFERRED_KEY.format(guid=101), str(mid))
    rig.run(10)
    dstore.post(store, channel="dm:101", author=101, origin="mind", text="brb boss",
                turn=PAIR_LINE_TURN + "t9.1", now=rig.clock.now())
    owed = Owed("discord", "dm:101", 101, [mid], t0, t0)
    assert watch._discord_answered(owed) == []
    assert [g for g, _, _ in talk._owed()] == [101]
    rig.run(400)
    assert count_unanswered(store.conn, HUMAN, 0, rig.clock.now()).unanswered == {"discord_dm": 1}
    dstore.post(store, channel="dm:101", author=101, origin="mind", reply_to=mid,
                text="ah no, which one was it", now=rig.clock.now())  # her answer to it
    assert watch._discord_answered(owed) == [mid]
    rig.close()


def test_a_channel_post_goes_to_one_who_is_there_or_waits_for_her(store, tmp_path):
    """Review-353 P2-1: his post in a channel goes to a member who is there; one he answers by
    replying to her waits until she is back."""
    rig = _dm_rig(store, tmp_path)
    talk = talk_for(rig)
    now = rig.clock.now()
    put_away(rig, 101, seconds=600.0, channel="dm:101")
    picked = set()
    for i in range(30):
        mid = post(rig, "general", f"anyone free for a quick run, take {i}")
        picked |= {t.speaker for t, _ in talk.plan(mid)}
    assert picked and 101 not in picked
    hers = member_post(rig, 101, "general", "the cart wheel came off again")
    mid = post(rig, "general", "how did that even happen", reply_to=hers)
    items = talk.plan(mid)
    assert [t.speaker for t, _ in items] == [101]
    assert items[0][0].delay >= 600.0 + BACK_FIND_S[0] - 1.0
    assert items[0][1].owed_answer and items[0][1].discord_named
    assert layout.channel("general") is not None and now == rig.clock.now()
    rig.close()


def test_health_reads_her_away(store, tmp_path, monkeypatch):
    """Review-353 P2-3: her away is in meta life:away:<guid> (numbers only), so health's bound
    for his DM post made during it is her return plus the time she takes to find it; keys of
    aways long over are dropped."""
    force(monkeypatch, away_s=900.0)
    rig = _dm_rig(store, tmp_path)
    talk = talk_for(rig)
    store.set_meta(AWAY_KEY.format(guid=105), "1000,1100")  # an away of long ago
    first = post(rig, "dm:101", "the kettle finally died this morning")
    hand(rig, talk, first)
    _run(rig, talk, 90)
    a = rig.engine.life.last_away(101)
    assert a is not None and a.until > rig.clock.now()
    assert parse_away(store.get_meta(AWAY_KEY.format(guid=101))) == (round(a.since),
                                                                      round(a.until))
    assert store.get_meta(AWAY_KEY.format(guid=105)) is None
    second = post(rig, "dm:101", "did you get a new one yet")
    due = _own_time_due(store.conn, HUMAN, rig.clock.now(), 6)
    assert due("dm:101", second, rig.clock.now()) >= (round(a.until) + BACK_FIND_S[1]
                                                      + OWNER_LATE_MARGIN_S)
    rig.close()


def test_health_keeps_previous_away_and_updates_early_return_atomically(store, clock, tmp_path, monkeypatch):
    from leeroy_mind.life.service import parse_aways
    from leeroy_mind.discord import owntime
    monkeypatch.setattr(owntime, "due_at", lambda *args, **kw: kw['post_ts'])
    r = rig(store, clock, tmp_path)
    try:
        now = r.clock.now()
        life = r.engine.life
        # Old persisted format still reads; a second interruption preserves it.
        store.set_meta(AWAY_KEY.format(guid=101), f"{now - 3600},{now - 2700}")
        store.set_meta(AWAY_KEY.format(guid=105), "1000,1100")
        a = Away(since=now, until=now + 900, channel="dm:101", reason="work", night="2026-11-21",
                 minute=0, back="back")
        life._mark(101, a)
        history = parse_aways(store.get_meta(AWAY_KEY.format(guid=101)))
        assert history == ((now - 3600, now - 2700), (now, now + 900))
        due = _own_time_due(store.conn, HUMAN, now, 6)
        assert due("dm:101", 1, now - 3570) == now - 2700 + BACK_FIND_S[1] + OWNER_LATE_MARGIN_S
        a.until = now + 60
        life._mark(101, a)
        assert parse_aways(store.get_meta(AWAY_KEY.format(guid=101))) == (
            (now - 3600, now - 2700), (now, now + 60))
        # A second connection sees the prune immediately, without an unrelated later unit.
        import sqlite3
        path = store.conn.execute("PRAGMA database_list").fetchone()["file"]
        with sqlite3.connect(path) as other:
            assert other.execute("SELECT 1 FROM meta WHERE key=?", (AWAY_KEY.format(guid=105),)).fetchone() is None
        assert not parse_aways("nan,inf;inf,inf;bad")
    finally:
        r.close()


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
