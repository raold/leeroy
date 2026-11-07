"""Members start conversations with him (owner, 2026-11-07: "they only respond, never
actually initiate"): the WhisperPlanner's own clock (romantic and personal openers by whisper)
and the burst budget's clock of openers addressed to him in party or guild, at mind.toml's
[talk] whispers_per_hour and openers_per_hour, scaled by the world and member dials, and never
in a fight, the wipe's silence, its debrief or his login's quiet."""
import random
import re
from collections import Counter

import pytest

from conftest import HUMAN, make_persona, run, seed_test_cast
from engine_rig import engine_clock, make_rig, numbered, save_member
from leeroy_mind.config import ConfigError, load_config
from leeroy_mind.protocol import Boss, Group, Login, Zone
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.budget import BurstBudget, DayPhase
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.reactions import LOGIN_QUIET_S
from leeroy_mind.talk.romance import (INVARIANTS, WhisperPlanner, romance_lift,
                                      whisper_kind, whisper_weight)
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
    assert w[201] > w[202] > w[203] > 0 and w[203] <= w[201] / 4  # fond: rarely
    assert w[204] == 0 and w[301] == 0  # no attitude and no warmth: no reason to whisper
    assert 0 < whisper_weight(friend, rel(301, 50.0)) <= w[203] * 2  # a friend, now and then
    assert [whisper_kind(p) for p in (lust, drawn, fond, none, friend)] == [
        "romantic", "romantic", "personal", "casual", "casual"]
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
    from leeroy_mind.talk.turns import TurnKind
    text = render_instruction(TurnKind.OPEN, addressee="Zan", topic="ask him how his day went",
                              forms=("he", "him", "his"), to_him=True)
    assert text.startswith(TO_HIM) and "say his name and ask him how his day went" in text
    assert "{" not in text and "(nothing)" not in text
    plain = render_instruction(TurnKind.OPEN, addressee="Zan")  # any other OPEN, as before
    assert TO_HIM not in plain and "(nothing)" in plain
    assert render_instruction(TurnKind.CONTINUE, addressee="Zan", to_him=True) ==         render_instruction(TurnKind.CONTINUE, addressee="Zan")


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
        r.engine.whispers.set_rate(8.0)  # more draws, under the members' own pacing
        await r.session()
        await play(r, 4 * 3600, step=6.0)
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


def test_chattiness_zero_starts_nothing(store, clock, tmp_path):
    async def main():
        cast_for_initiation(store)
        r = make_rig(store, clock, tmp_path, chattiness=0, script=numbered("thought {n} here"))
        await r.session()
        await play(r, 3600)
        await r.engine.end_session()
        assert [x for x in started(r) if x[3] in ("whisper:start", "burst:to_him")] == []
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
