"""Members start conversations with him (owner, 2026-11-07: "they only respond, never
actually initiate"): the WhisperPlanner's own clock (romantic and personal openers by whisper)
and the burst budget's clock of openers addressed to him in party or guild, at mind.toml's
[talk] whispers_per_hour and openers_per_hour, scaled by the world and member dials, and never
in a fight, the wipe's silence, its debrief or his login's quiet."""
import logging
import random
import re
from collections import Counter

from dataclasses import replace

import pytest

from conftest import HUMAN, TEST_CAST, make_persona, run, seed_test_cast
from engine_rig import engine_clock, make_rig, numbered, save_member
from leeroy_mind.config import ConfigError, load_config
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import (AfkEvent, Boss, Cast, Emote, Group, Login, LootWon, Member,
                                  TextEmote, Zone)
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk import budget as budget_mod
from leeroy_mind.talk import emotes as emotes_mod
from leeroy_mind.talk import romance as romance_mod
from leeroy_mind.talk.budget import (AFTER_BURST_S, TO_HIM_HOUR_MAX, BurstBudget, DayPhase)
from leeroy_mind.talk.guards import Guards
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.reactions import LOGIN_QUIET_S
from leeroy_mind.talk.romance import (FOND_SHARE, HOUR_S, INVARIANTS, WHISPERS_APART_S,
                                      WHISPERS_HOUR_MAX, WhisperPlanner, romance_lift,
                                      whisper_kind, whisper_weight)
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.tuning.state import Tuning

R = random.Random
P = RaidPhase
ROMANCE = INVARIANTS.split(",")[1].strip()  # in every romance instruction
START = "You're the one starting this conversation"
TO_HIM = "Start a conversation with Zan"


@pytest.fixture
def clock():
    return engine_clock()


def woman(guid, name, stance, **kw):
    return make_persona(guid, name, player_gender="female", stance=stance, **kw)


def man(guid, name, **kw):
    return make_persona(guid, name, **kw)


def rel(guid, affinity=0.0):
    return Relationship(a=guid, b=HUMAN, affinity=affinity)


def tune(p, **dials):
    t = Tuning()
    for dial, value in dials.items():
        t = t.with_value("member", str(p.guid), dial, value)
    return t.apply(p)


# -- mind.toml ---------------------------------------------------------------------------------

def test_the_rates_are_talk_keys_with_defaults_and_validation(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert (cfg.talk.whispers_per_hour, cfg.talk.openers_per_hour) == (4, 6)
    (tmp_path / "ok.toml").write_text("[talk]\nwhispers_per_hour = 2.5\nopeners_per_hour = 0\n",
                                      encoding="utf-8")
    ok = load_config(tmp_path / "ok.toml")
    assert (ok.talk.whispers_per_hour, ok.talk.openers_per_hour) == (2.5, 0)
    for bad in ("-1", "61", '"often"', "true"):
        for key in ("whispers_per_hour", "openers_per_hour"):
            p = tmp_path / "bad.toml"
            p.write_text(f"[talk]\n{key} = {bad}\n", encoding="utf-8")
            with pytest.raises(ConfigError, match=re.escape(f"talk.{key}")):
                load_config(p)


# -- who whispers him first --------------------------------------------------------------------

def test_lust_and_drawn_women_whisper_most_fond_rarely_men_only_casually():
    lust, drawn = woman(201, "Belra", "lust"), woman(202, "Dunna", "drawn")
    fond, none = woman(203, "Ebba", "fond"), woman(204, "Fira", "none")
    friend = man(301, "Coros")
    w = {p.guid: whisper_weight(p, rel(p.guid)) for p in (lust, drawn, fond, none, friend)}
    assert w[201] > w[202] > w[203] > 0
    assert w[204] == 0 and w[301] == 0  # no attitude and no warmth: no reason to whisper
    for affinity in (0.0, 20.0, 60.0):  # fond rarely: a quarter of lust at equal affinity
        assert whisper_weight(fond, rel(203, affinity)) == pytest.approx(
            FOND_SHARE * whisper_weight(lust, rel(201, affinity)))
    assert 0 < whisper_weight(friend, rel(301, 50.0)) <= w[203] * 2  # a friend, now and then
    assert [whisper_kind(p) for p in (lust, drawn, fond, none, friend)] == [
        "romantic", "romantic", "casual", "casual", "casual"]  # fond: never romantic
    assert whisper_kind(make_persona(302, "Arkon", stance="fond")) == "casual"  # a man: a friend


def test_the_romance_dial_lifts_only_the_women_who_want_him():
    lust, fond, friend = woman(201, "Belra", "lust"), woman(203, "Ebba", "fond"), man(301, "Coros")
    assert romance_lift(5) == 1.0 and romance_lift(1) < 1.0 < romance_lift(9)
    lo, hi = romance_lift(1), romance_lift(10)
    assert whisper_weight(lust, rel(201), lift=hi) > whisper_weight(lust, rel(201), lift=lo)
    assert whisper_weight(fond, rel(203), lift=hi) == whisper_weight(fond, rel(203), lift=lo)
    assert (whisper_weight(friend, rel(301, 40.0), lift=hi)
            == whisper_weight(friend, rel(301, 40.0), lift=lo))


def hours_of_whispers(per_hour, cands, *, hours=40, lift=1.0, seed=3):
    """Whispers the planner's clock starts per hour, and who started them."""
    w = WhisperPlanner(R(seed), per_hour=per_hour)
    by, now, end = Counter(), 10_000.0, 10_000.0 + hours * 3600
    while now < end:
        if w.due(now):
            g = w.start(now, cands, lift=lift)
            if g is not None:
                by[g] += 1
        now += 5.0
    return sum(by.values()) / hours, by


def test_the_planner_whispers_at_its_rate_with_pacing():
    cands = {g: (woman(g, f"W{g}", s), rel(g, 20.0)) for g, s in
             ((201, "lust"), (202, "drawn"), (203, "fond"), (204, "lust"))}
    rate, by = hours_of_whispers(4.0, cands)
    assert 3.5 <= rate <= 4.5
    assert by[201] > by[203] and by[204] > by[203]
    assert hours_of_whispers(0.0, cands, hours=5)[0] == 0
    # within one session, an hour holds 3-6 of them
    for seed in range(20):
        n = hours_of_whispers(4.0, cands, hours=1, seed=seed)[0]
        assert 3 <= n <= 6, seed


def test_member_dials_move_the_rate_itself():
    base = {g: (woman(g, f"W{g}", "lust"), rel(g)) for g in (201, 202, 203)}
    bold = {g: (tune(p, forward=10), r) for g, (p, r) in base.items()}
    quiet = {g: (tune(p, talkative=0), r) for g, (p, r) in base.items()}
    assert hours_of_whispers(4.0, bold)[0] > 1.6 * hours_of_whispers(4.0, base)[0]
    assert hours_of_whispers(4.0, quiet, hours=5)[0] == 0
    assert (hours_of_whispers(4.0, base, lift=romance_lift(9))[0]
            > hours_of_whispers(4.0, base, lift=romance_lift(1))[0])


def test_an_event_whisper_takes_the_clocks_place_and_a_hold_waits():
    cands = {201: (woman(201, "Belra", "lust"), rel(201))}
    w = WhisperPlanner(R(1), per_hour=4.0)
    assert not w.due(1000.0)  # the first call draws the first gap
    t = 1000.0
    while w.maybe(t, "idle", cands) is None:
        t += 700.0  # past the member's own pacing every time
    assert not any(w.due(t + s) for s in range(0, int(0.6 * 900)))  # a whole gap again
    w = WhisperPlanner(R(1), per_hour=4.0)
    w.due(0.0)
    assert not any(w.due(float(s), hold=True) for s in range(0, 4000, 5))
    assert any(w.due(4000.0 + s) for s in range(0, 120))  # due once the hold has ended
    w.set_rate(0.0)
    assert not any(w.due(5000.0 + s) for s in range(0, 4000, 5))


# -- the openers addressed to him ----------------------------------------------------------------

def test_the_opener_to_him_has_its_own_instruction_only_when_asked_for():
    from leeroy_mind.talk.prompt import render_instruction
    text = render_instruction(TurnKind.OPEN, addressee="Zan", topic="ask him how his day went",
                              forms=("he", "him", "his"), to_him=True)
    assert text.startswith(TO_HIM) and "say his name and ask him how his day went" in text
    assert "{" not in text and "(nothing)" not in text
    plain = render_instruction(TurnKind.OPEN, addressee="Zan")  # any other OPEN, as before
    assert TO_HIM not in plain and "(nothing)" in plain
    assert (render_instruction(TurnKind.CONTINUE, addressee="Zan", to_him=True)
            == render_instruction(TurnKind.CONTINUE, addressee="Zan"))


def openers(chattiness, *, hours=30, raid=P.NONE, quiet_until=None, seed=5, per_hour=6.0):
    b = BurstBudget(chattiness, R(seed), to_him_per_hour=per_hour)
    now, end, n = 0.0, hours * 3600.0, 0
    while now < end:
        plan = b.to_him(now, raid=raid, quiet_until=quiet_until)
        if plan is not None:
            assert plan.to_him and plan.size == 1
            if quiet_until is not None:
                assert now >= quiet_until
            n += 1
            b.to_him_done(now + 10.0)
        now += 5.0
    return n / hours


def test_openers_to_him_come_at_their_rate_scaled_by_chattiness():
    assert 5.0 <= openers(4) <= 7.0
    assert 10.0 <= openers(8) <= 14.0
    assert openers(0, hours=5) == 0 and openers(4, hours=5, per_hour=0.0) == 0
    for phase in (P.COMBAT, P.WIPE_SILENCE, P.WIPE_DEBRIEF):
        assert openers(4, hours=3, raid=phase) == 0
    assert openers(4, hours=3, quiet_until=1800.0) > 0


def test_an_opener_to_him_waits_for_a_running_burst():
    b = BurstBudget(4, R(2), to_him_per_hour=60.0)
    burst = None
    now = 0.0
    while burst is None:
        burst = b.tick(now, phase=DayPhase.EVENING, human_idle=False, raid=P.NONE)
        now += 1.0
    assert all(b.to_him(now + s, raid=P.NONE) is None for s in range(0, 200))
    b.burst_done(now + 200)
    assert all(b.to_him(now + 200 + s, raid=P.NONE) is None for s in range(0, 59))
    assert any(b.to_him(now + 260 + s, raid=P.NONE) for s in range(0, 600))


# -- an hour of his play ---------------------------------------------------------------------------

def cast_for_initiation(store):
    """The test cast as a guild: Belra wants him, Dunna is drawn to him, Coros a woman fond of
    him, Arkon a man who likes him, Ebbo a man with no warmth for him."""
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="lust", style="bold")
    save_member(store, 104, player_gender="female", stance="drawn", style="bold")
    save_member(store, 103, player_gender="female", stance="fond")
    store.put_relationship(Relationship(a=101, b=HUMAN, affinity=50.0, familiarity=40))


async def play(r, seconds, *, step=4.0, t0=None):
    """He plays: a tick every `step`, and something he does every few minutes keeps him
    active (not idle)."""
    t0 = r.clock.now() if t0 is None else t0
    end = r.clock.now() + seconds
    i = 0
    while r.clock.now() < end:
        await r.advance(step)
        if i % 45 == 0:
            r.world.apply(Zone(ts=0, guid=HUMAN, zone_id=10, zone_name="Dalaran"),
                          r.clock.now())
        await r.engine.tick(r.clock.now())
        i += 1


def started(r, since=0.0):
    """(time, guid, channel, why) of every line that started something with him."""
    return [(u["ts"], u["guid"], u["channel"], u["why"]) for u in r.store.conn.execute(
        "SELECT ts, guid, channel, why FROM utterance WHERE dropped IS NULL AND ts >= ? AND "
        "(why LIKE 'whisper:%' OR why = 'burst:to_him') ORDER BY id", (since,))]


@pytest.mark.parametrize("seed", [1, 2])
def test_an_hour_of_his_play_brings_whispers_and_openers_to_him(store, clock, tmp_path, seed):
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, seed=seed, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        await r.session()
        t0 = clock.now()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await play(r, 3600)
        await r.engine.end_session()
        lines = started(r, t0)
        whispers = [x for x in lines if x[2] == "whisper"]
        to_him = [x for x in lines if x[3] == "burst:to_him"]
        assert 3 <= len(whispers) <= 6, lines
        assert 4 <= len(to_him) <= 8, lines
        assert all(ts >= t0 + LOGIN_QUIET_S for ts, *_ in lines)  # never in his login's quiet
        assert all(ch == "guild" for _, _, ch, _ in to_him)  # he isn't grouped: guild
        assert {g for _, g, _, _ in whispers} <= {101, 102, 103, 104}  # Ebbo has no reason
        whisper_to = {u["to_guid"] for u in store.conn.execute(
            "SELECT to_guid FROM utterance WHERE why LIKE 'whisper:%'")}
        assert whisper_to == {HUMAN}
    run(main())


def test_romance_goes_by_whisper_and_the_openers_to_him_stay_casual(store, clock, tmp_path):
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        await r.session()  # at the default rate, so the members' own pacing rarely binds
        await play(r, 6 * 3600, step=6.0)
        await r.engine.end_session()
        by = Counter(g for _, g, ch, _ in started(r) if ch == "whisper")
        # Belra (lust) and Dunna (drawn) most, Coros (fond) rarely, Arkon (a friend) now and
        # then, Ebbo (no warmth) never
        assert min(by[102], by[104]) > max(by[103], by[101]) and by[102] >= 2 * by[103]
        assert by[105] == 0
        prompts = r.prompts()
        opening = [p for p in prompts if TO_HIM in p]
        assert opening and not any(ROMANCE in p for p in opening)  # casual, even from Belra
        whispered = [p for p in prompts if START in p]
        assert whispered and any(ROMANCE in p for p in whispered)
        # a man's whisper is a friend's casual one, never romance
        men = [p for p in whispered if "Something casual" in p]
        assert all(ROMANCE not in p for p in men)
    run(main())


def test_an_opener_to_him_goes_to_party_when_he_is_grouped_with_them(store, clock, tmp_path):
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 102, 103)))
        assert r.engine._to_him_channel(102) == "party"
        assert r.engine._to_him_channel(104) == "guild"  # not in his group
        await r.session()
        await play(r, 2 * 3600, step=6.0)
        await r.engine.end_session()
        to_him = [(g, ch) for _, g, ch, why in started(r) if why == "burst:to_him"]
        assert to_him and all(ch == ("party" if g in (102, 103) else "guild")
                              for g, ch in to_him)
        assert any(ch == "party" for _, ch in to_him)
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=True,
                            members=(HUMAN, 101, 102, 103, 104, 105)))
        assert r.engine._to_him_channel(102) == "guild"  # a raid of six: guild chat
    run(main())


def test_nothing_is_started_in_a_fight_the_wipe_or_its_debrief(store, clock, tmp_path):
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        r.engine.whispers.set_rate(30.0)
        r.engine.budget.set_to_him_rate(30.0)
        await r.event(Group(ts=0, leader_guid=102, raid=True,
                            members=(HUMAN, 101, 102, 103, 104, 105)))
        await r.session()
        await play(r, 900)
        assert started(r), "initiation runs between pulls"
        engaged = clock.now()
        await r.event(Boss(ts=0, map=631, instance_id=5, boss_id=0, boss_name="", state="engaged",
                           members=(HUMAN, 101, 102, 103, 104, 105)))
        await play(r, 600)
        await r.event(Boss(ts=0, map=631, instance_id=5, boss_id=0, boss_name="", state="wiped",
                           members=(HUMAN, 101, 102, 103, 104, 105)))
        await play(r, 55)
        assert r.raid.phase(clock.now()) is P.WIPE_DEBRIEF
        held = clock.now()
        assert started(r, engaged) == []
        await play(r, 900)
        assert [x for x in started(r, held) if x[0] > held]  # and resumes after the debrief
        await r.engine.end_session()
    run(main())


def test_chattiness_zero_stops_every_unprompted_whisper_and_opener(store, clock, tmp_path):
    """The clock's whispers and openers, and the event draws too: a quiet stretch, the
    evening (his loot), the late hour and the voice service's private-room invite (which
    draws from the same planner)."""
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, chattiness=0, script=numbered("thought {n} here"))
        drawn = []
        maybe = r.engine.whispers.maybe

        def spy(now, trigger, candidates):
            drawn.append(trigger)
            return maybe(now, trigger, candidates)
        r.engine.whispers.maybe = spy
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await play(r, 900, step=10.0)
        await r.event(LootWon(ts=0, guid=HUMAN, item_id=50415, name="Bryntroll", method="need"))
        await play(r, 3600, step=20.0)
        for trigger in ("late", "after_kill"):  # the evening's draw took the late hour's
            for _ in range(20):
                r.engine._maybe_whisper(trigger, clock.now(), about="Lord Marrowgar")
                await play(r, 700, step=20.0)
        assert {"idle", "evening", "late", "after_kill"} <= set(drawn)  # tried, none landed
        assert started(r) == []
        cands = r.engine._whisper_cands(clock.now())
        assert cands and all(r.engine.whispers.maybe(clock.now() + i * 700, "idle", cands) is None
                             for i in range(200))  # the voice invite path's draw
        await r.engine.end_session()
    run(main())


def test_a_silent_start_is_asked_again_once(store, clock, tmp_path):
    hint = "Start the conversation with Zan"
    asked = []

    def script(model, messages):
        text = messages[-1]["content"]
        if START in text or TO_HIM in text:
            asked.append(hint in text)
            return "(nothing)" if hint not in text else f"hey zan {len(asked)}"
        return f"thought {len(asked)} here {len(text)}"

    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=script)
        r.engine.bits.p_fire = 0.0
        await r.session()
        await play(r, 3600)
        await r.engine.end_session()
        assert asked and asked.count(True) == asked.count(False)  # every silence asked once
        assert len(started(r)) >= asked.count(True) > 0
    run(main())


# -- fix round 1 (controller review of c0a7ed7) -----------------------------------------------

def big_guild(store, n=100):
    """The test cast plus `n` invented members, a third of them women who want him."""
    cast_for_initiation(store)
    for g in range(201, 201 + n):
        over = {"player_gender": "female", "stance": "lust"} if g % 3 == 0 else {}
        save_persona(store, make_persona(g, f"Mem{g}", **over), source="seed", ts=1000.0,
                     race="Orc", class_name="Warrior")
    return list(range(201, 201 + n))


def online_all(r, extra):
    members = [Member(guid=g, name=n, race=rc, class_=c, gender=0, level=80, rank=3,
                      online=True, map=0, zone=10, spec_tab=-1)
               for g, (n, rc, c) in TEST_CAST.items()]
    members += [Member(guid=g, name=f"Mem{g}", race=2, class_=1, gender=0, level=80, rank=3,
                       online=True, map=0, zone=10, spec_tab=-1) for g in extra]
    r.world.apply(Cast(ts=0, members=tuple(members)), r.clock.now())


def his_lines(r, since=0.0):
    return [u["ts"] for u in r.store.conn.execute(
        "SELECT ts FROM chat_line WHERE origin = 'human' AND ts >= ? ORDER BY id", (since,))]


def test_party_openers_come_from_his_group_mates_in_a_big_guild(store, clock, tmp_path):
    """Ruling 1: "it can be in party" means the people he plays with. About 100 online, 4 in
    his party: the openers to him come from those 4, in party; anyone else's go to guild."""
    async def main():
        extra = big_guild(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        online_all(r, extra)
        mates = (101, 102, 103, 104)
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, *mates)))
        await r.session()
        await play(r, 3600)
        await r.engine.end_session()
        to_him = [(g, ch) for _, g, ch, why in started(r) if why == "burst:to_him"]
        assert len(to_him) >= 4
        assert all(ch == ("party" if g in mates else "guild") for g, ch in to_him)
        assert sum(g in mates for g, _ in to_him) >= 0.8 * len(to_him)
    run(main())


def test_him_chatting_through_the_hour_still_gets_every_reply(store, clock, tmp_path):
    """Ruling 2: nothing starts a conversation with him within AFTER_BURST_S of his own line
    or an answer to him, and the lines that do start one never take the guild's minute from
    an answer. Bursts are off, so only the two clocks compete with the replies."""
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        r.engine.budget = BurstBudget(4, r.engine.rng, gap_s=(1e9, 1e9), to_him_per_hour=60.0)
        r.engine.whispers.set_rate(60.0)
        await r.session()
        t0 = clock.now()
        for n in range(24):  # a line of his every 150 s for an hour
            await r.human(f"so what is everyone up to {n}")
            await play(r, 150)
        await r.engine.end_session()
        replies = r.store.conn.execute(
            "SELECT dropped FROM utterance WHERE kind = 'reply_human'").fetchall()
        assert replies and all(u["dropped"] is None for u in replies)
        answered = [u["ts"] for u in r.store.conn.execute(
            "SELECT ts FROM utterance WHERE kind = 'reply_human' AND dropped IS NULL")]
        talk = his_lines(r, t0) + answered
        lines = started(r, t0)
        assert lines, "the clocks still start something in his pauses"
        # planned at least AFTER_BURST_S after his talk, so never 10-60 s after a line of it
        assert not [x for x in lines if any(10 <= x[0] - t < AFTER_BURST_S for t in talk)]
    run(main())


def test_afk_holds_both_clocks_and_no_two_whispers_come_within_two_minutes(store, clock,
                                                                           tmp_path):
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        r.engine.whispers.set_rate(60.0)
        r.engine.budget.set_to_him_rate(60.0)
        await r.session()
        await play(r, 1200)
        assert started(r)
        away = clock.now()
        await r.event(AfkEvent(ts=0, guid=HUMAN, on=True))
        await play(r, 1800)
        back = clock.now()
        await r.event(AfkEvent(ts=0, guid=HUMAN, on=False))
        await play(r, 1200)
        await r.engine.end_session()
        lines = started(r)
        assert not [x for x in lines if away + 30 < x[0] < back]  # what was under way aside
        assert [x for x in lines if x[0] > back]
        clocks = [x for x in lines if x[3] in ("whisper:start", "burst:to_him")]
        assert not [x for x in clocks if back <= x[0] < back + 30]  # not the moment he's back
        whispers = [ts for ts, _, ch, _ in lines if ch == "whisper"]
        assert all(b - a >= WHISPERS_APART_S - 30 for a, b in zip(whispers, whispers[1:]))
    run(main())


def test_no_two_unprompted_whispers_within_two_minutes_whatever_draws_them():
    cands = {g: (woman(g, f"W{g}", "lust"), rel(g)) for g in (201, 202, 203)}
    w = WhisperPlanner(R(1), per_hour=4.0)
    w.due(0.0)
    t = 10.0
    while w.maybe(t, "idle", cands) is None:
        t += 1.0
    assert all(w.maybe(t + s, "late", cands) is None for s in range(0, int(WHISPERS_APART_S)))
    assert w.start(t + 30.0, cands) is None  # the clock's own draw is refused too
    assert any(w.maybe(t + WHISPERS_APART_S + s, "late", cands) for s in range(0, 600))


def test_the_planner_says_why_a_whisper_does_not_fit():
    cands = {g: (woman(g, f"W{g}", "lust"), rel(g)) for g in range(201, 211)}
    w = WhisperPlanner(R(1), per_hour=4.0)
    assert w.refusal(0.0) is None
    said, t = [], 0.0
    while len(said) < WHISPERS_HOUR_MAX:
        if w.maybe(t, "idle", cands) is not None:
            said.append(t)
            full = len(said) == WHISPERS_HOUR_MAX
            assert w.refusal(t + 1.0) == ("ceiling" if full else "spacing")
            t += WHISPERS_APART_S
        t += 1.0
    assert w.refusal(t) == "ceiling"
    assert w.refusal(said[0] + HOUR_S) is None


def test_a_due_whisper_the_spacing_refuses_is_logged_as_spacing(store, clock, tmp_path,
                                                                monkeypatch, caplog):
    """Review minor: the clock's slot passing for the spacing or the hour's ceiling says so,
    not no_candidates."""
    monkeypatch.setattr(romance_mod, "GAP_SPREAD", (0.02, 0.03))

    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        w = r.engine.whispers
        w.set_rate(60.0)
        await r.session()
        await r.engine.tick(clock.now())  # the clock's first gap
        now = clock.now()
        while w.maybe(now, "idle", r.engine._whisper_cands(now)) is None:
            pass  # an event's whisper drawn just now: the clock's next is seconds away
        await r.advance(20)
        assert w.next_at <= clock.now()
        with caplog.at_level(logging.INFO, logger="leeroy_mind"):
            await r.engine.tick(clock.now())
        await r.engine.end_session()
        assert "whisper_skipped: trigger=start reason=spacing" in caplog.text
        assert "reason=no_candidates" not in caplog.text
    run(main())


def test_a_member_in_a_whisper_with_him_does_not_start_one(store, clock, tmp_path):
    """Ruling 5: a whisper either way in the last 10 minutes is a conversation going on."""
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, online=(102, 104),
                     script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        r.engine.whispers.set_rate(60.0)
        r.engine.budget.set_to_him_rate(60.0)
        await r.session()
        for n in range(15):  # he whispers Belra every four minutes
            await r.human(f"hey you {n}", channel="whisper", to_guid=102)
            await play(r, 240)
        await r.engine.end_session()
        by = Counter(g for _, g, _, _ in started(r))
        assert by[102] == 0 and by[104] > 0
    run(main())


def test_the_openers_clock_starts_over_each_session(store, clock, tmp_path):
    b = BurstBudget(4, R(3), to_him_per_hour=6.0)
    b.to_him(0.0, raid=P.NONE)
    assert b.to_him_next is not None
    b.reset_to_him()
    assert b.to_him_next is None
    b.to_him(100.0, raid=P.NONE)
    assert b.to_him_next >= 100.0 + 0.6 * 600

    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        await r.session()
        await play(r, 60)
        assert r.engine.budget.to_him_next is not None
        await r.engine.end_session()
        await r.session()
        assert r.engine.budget.to_him_next is None and r.engine.whispers.next_at is None
        await r.engine.end_session()
    run(main())


def test_whatever_the_dials_at_most_8_whispers_and_12_openers_an_hour():
    bold = {g: (tune(woman(g, f"W{g}", "lust"), forward=10, talkative=10), rel(g, 80.0))
            for g in range(201, 221)}
    w = WhisperPlanner(R(2), per_hour=60.0)
    times = []
    now = 0.0
    while now < 6 * 3600:
        if w.due(now) and w.start(now, bold, lift=romance_lift(10)) is not None:
            times.append(now)
        if w.maybe(now, "idle", bold) is not None:
            times.append(now)
        now += 5.0
    assert times
    assert max(sum(1 for t in times if a <= t < a + 3600) for a in times) <= WHISPERS_HOUR_MAX
    assert WHISPERS_HOUR_MAX == 8 and TO_HIM_HOUR_MAX == 12
    b = BurstBudget(10, R(4), to_him_per_hour=60.0)
    planned, now = [], 0.0
    while now < 6 * 3600:
        if b.to_him(now, raid=P.NONE) is not None:
            planned.append(now)
            b.to_him_done(now + 5.0, mult=2.0)
        now += 5.0
    assert max(sum(1 for t in planned if a <= t < a + 3600) for a in planned) <= TO_HIM_HOUR_MAX


def test_an_answer_to_him_never_waits_for_a_line_that_started_a_conversation():
    g = Guards(rng=R(1))
    for n in range(6):
        g.record(f"t{n}", 201 + n, 100.0, TurnKind.OPEN, initiated=True)
    assert g.allow(301, TurnKind.REPLY_HUMAN, 101.0)  # the openers left his answers their minute
    assert not g.allow(302, TurnKind.OPEN, 101.0)  # but the guild's social pace counts them
    for n in range(6):
        g.record(f"r{n}", 401 + n, 100.0, TurnKind.REPLY_HUMAN)
    assert not g.allow(303, TurnKind.REPLY_HUMAN, 101.0)
    own = Guards(rng=R(1))  # nor does her own opener hold back her answer to him
    own.record("o", 201, 100.0, TurnKind.OPEN, initiated=True)
    assert own.allow(201, TurnKind.REPLY_HUMAN, 103.0)
    assert not own.allow(201, TurnKind.OPEN, 103.0)
    own.record("r", 201, 104.0, TurnKind.REPLY_HUMAN)
    assert not own.allow(201, TurnKind.REPLY_HUMAN, 106.0)  # her answer's own interval stands


def test_her_line_after_his_emote_is_an_answer_the_openers_never_crowd_out():
    g = Guards(rng=R(1))
    for n in range(4):
        g.record(f"t{n}", 201 + n, 100.0, TurnKind.OPEN, initiated=True)
    assert g.allow(301, TurnKind.EMOTE_REACT, 101.0)
    assert not g.allow(302, TurnKind.OPEN, 101.0)
    for n in range(4):
        g.record(f"e{n}", 401 + n, 100.0, TurnKind.EMOTE_REACT)
    assert not g.allow(303, TurnKind.EMOTE_REACT, 101.0)  # its own social pace still holds
    own = Guards(rng=R(1))  # her whisper that started it never holds back her line either
    own.record("o", 201, 100.0, TurnKind.WHISPER, initiated=True)
    assert own.allow(201, TurnKind.EMOTE_REACT, 103.0)
    own.record("e", 201, 104.0, TurnKind.EMOTE_REACT)
    assert not own.allow(201, TurnKind.EMOTE_REACT, 110.0)


def test_the_guild_calendar_is_an_opener_topic_within_a_day(store, clock, tmp_path):
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path)
        now = clock.now()
        store.conn.execute(
            "INSERT INTO realm_calendar(kind, creator, at, title, ref, updated, event_id, state) "
            "VALUES ('raid', 102, ?, 'Raid night', 'raid:test', ?, 7, 'posted')",
            (now + 3 * 3600, now))
        store.conn.execute(
            "INSERT INTO realm_calendar(kind, creator, at, title, ref, updated, event_id, state) "
            "VALUES ('meeting', 102, ?, 'Officer meeting', 'meeting:test', ?, 8, 'posted')",
            (now + 3 * 86400, now))
        store.conn.commit()
        assert r.engine._guild_event(now) == "Raid night tonight at 23:00"
        p = r.engine.personas.get(103)
        topics = {r.engine._to_him_topic(p, now) for _ in range(8)}
        assert "bring up what's on the guild calendar: Raid night tonight at 23:00" in topics
        assert not any("Officer meeting" in t for t in topics)  # three days off
        assert r.engine._guild_event(now + 4 * 3600) == ""
    run(main())


def test_a_5man_boss_fight_holds_both_clocks(store, clock, tmp_path):
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        r.engine.whispers.set_rate(60.0)
        r.engine.budget.set_to_him_rate(60.0)
        party = (HUMAN, 102, 103, 104)
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=False, members=party))
        await r.session()
        await play(r, 900)
        assert started(r)
        engaged = clock.now()
        await r.event(Boss(ts=0, map=574, instance_id=9, boss_id=0, boss_name="",
                           state="engaged", members=party))
        assert r.raid.phase(clock.now()) is P.NONE  # not a raid fight: RaidState ignores it
        await play(r, 600)
        killed = clock.now()
        await r.event(Boss(ts=0, map=574, instance_id=9, boss_id=0, boss_name="",
                           state="killed", members=party))
        await play(r, 900)
        await r.engine.end_session()
        lines = started(r)
        assert not [x for x in lines if engaged + 30 < x[0] < killed]
        assert [x for x in lines if x[0] > killed]
    run(main())


def test_a_live_rescale_through_apply_config(store, clock, tmp_path):
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        await r.session()
        await r.engine.tick(clock.now())
        await r.engine.tick(clock.now())
        now = clock.now()
        w_left = r.engine.whispers.next_at - now
        o_left = r.engine.budget.to_him_next - now
        cfg = r.cfg
        r.engine.apply_config(replace(cfg, talk=replace(cfg.talk, chattiness=8)))
        assert r.engine.whispers.next_at - now == pytest.approx(w_left / 2)
        assert r.engine.budget.to_him_next - now == pytest.approx(o_left / 2)
        r.engine.apply_config(replace(cfg, talk=replace(cfg.talk, whispers_per_hour=2,
                                                        openers_per_hour=3)))
        assert r.engine.whispers.next_at - now == pytest.approx(w_left * 2)
        assert r.engine.budget.to_him_next - now == pytest.approx(o_left * 2)
        r.engine.apply_config(replace(cfg, talk=replace(cfg.talk, chattiness=0)))
        assert r.engine.whispers.next_at is None and not r.engine.whispers.events
        await r.engine.tick(clock.now())
        assert r.engine.budget.to_him_next is None
        await r.engine.end_session()
    run(main())


def test_nothing_starts_in_his_login_quiet_even_when_the_clocks_are_due(store, clock, tmp_path,
                                                                        monkeypatch):
    """With gaps of seconds both clocks would be due inside the quiet: the assertion fails
    if either ignored it."""
    monkeypatch.setattr(budget_mod, "GAP_SPREAD", (0.02, 0.03))
    monkeypatch.setattr(romance_mod, "GAP_SPREAD", (0.02, 0.03))

    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        r.engine.whispers.set_rate(60.0)
        r.engine.budget.set_to_him_rate(60.0)
        await r.session()
        t0 = clock.now()
        await r.engine.tick(t0)  # both clocks drawn before his login
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await play(r, 600)
        await r.engine.end_session()
        lines = started(r, t0)
        assert not [x for x in lines if x[0] < t0 + LOGIN_QUIET_S]
        assert [x for x in lines if x[0] < t0 + LOGIN_QUIET_S + 240]
    run(main())


def test_an_opener_due_as_he_emotes_at_a_member_waits_out_the_exchange(store, clock, tmp_path,
                                                                       monkeypatch):
    """His emote exchange is his talk: an opener to him due a second after his /wave at Coros
    waits AFTER_BURST_S after her wave back. No line follows her emote, so the emote alone
    holds it."""
    monkeypatch.setattr(emotes_mod, "LINE_P", 0.0)
    monkeypatch.setattr(emotes_mod, "LINE_P_ROMANTIC", 0.0)

    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"))
        r.engine.bits.p_fire = 0.0
        r.engine.whispers.set_rate(0.0)
        r.engine.whispers.events = False
        r.engine.budget = BurstBudget(4, r.engine.rng, gap_s=(1e9, 1e9), to_him_per_hour=6.0)
        await r.session()
        await r.engine.tick(clock.now())  # the openers' first gap
        due = r.engine.budget.to_him_next
        await r.advance(due - clock.now() - 1.0)
        emoted = clock.now()
        ev = TextEmote(ts=0, guid=HUMAN, emote="wave", target_guid=103)
        r.world.apply(ev, emoted)
        await r.engine.on_text_emote(ev, emoted)
        await play(r, 600)
        await r.engine.end_session()
        (back_at, back), = [(t, c) for t, c in r.sent.log if isinstance(c, Emote)]
        assert back.guid == 103 and back.target_guid == HUMAN
        assert due <= back_at  # the opener was due before her emote back went out
        to_him = [x for x in started(r) if x[3] == "burst:to_him"]
        assert to_him and to_him[0][0] >= back_at + AFTER_BURST_S
        assert to_him[0][0] < back_at + AFTER_BURST_S + 120  # and went once the minute was out
    run(main())
