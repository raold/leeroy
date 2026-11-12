"""Optional cadence over fake time and an invented cast; no model or native probes."""
import asyncio
from dataclasses import replace
import random

import pytest

from conftest import HUMAN, make_persona, run
from engine_rig import engine_clock, make_rig
from leeroy_mind.config import load_config
from leeroy_mind.fingerprint import Profile
from leeroy_mind.protocol import Boss, Logout, Zone
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.raid import RaidPhase
from leeroy_mind.talk.realism import (ADJACENT, MAX_MEMBERS, SAFE_TYPOS, Realism,
    duration, split_line, typo, words_per_minute)
from leeroy_mind.talk.style import Styled
from leeroy_mind.talk.turns import Turn, TurnKind


class Always(random.Random):
    def random(self):
        return 0.0

    def uniform(self, a, b):
        return (a + b) / 2


@pytest.fixture
def clock():
    return engine_clock()


@pytest.fixture
def rig(store, clock, tmp_path):
    r = make_rig(store, clock, tmp_path, reply="ready with a flask")
    r.engine.cfg = replace(r.cfg, talk=replace(r.cfg.talk, realism=True))
    yield r
    r.close()


def turn(channel="guild", kind=TurnKind.REPLY_HUMAN):
    return Turn(kind=kind, speaker=101, channel=channel, addressee=HUMAN,
                to_guid=HUMAN if channel == "whisper" else 0, deadline=10**12)


def plan(r, *, channel="guild", text="ready with a flask", baseline=(2,), meta=None):
    e = r.engine
    return e.realism.plan(e, turn(channel), meta or TurnMeta("t1"), e.personas.get(101),
                         [Styled(text, text)], baseline)


def test_cadence_is_repeatable_bounded_and_uses_authored_age_habit_and_measured_profile():
    p = make_persona(101, "Arkon")
    speed = words_per_minute(p)
    assert speed == words_per_minute(p)
    assert speed > words_per_minute(replace(p, identity=replace(p.identity, age=75)))
    assert speed > words_per_minute(replace(p, typing=replace(p.typing, chars_per_sec=3)))
    assert speed == words_per_minute(p, Profile(available=1, seeded=1, p50=40))
    assert speed != words_per_minute(p, Profile(available=1, seeded=0, p50=40))
    for age in (25, 99):
        for cps in (3, 8):
            assert 25 <= words_per_minute(replace(p, identity=replace(p.identity, age=age),
                                               typing=replace(p.typing, chars_per_sec=cps))) <= 100


def test_formula_uses_length_speed_jitter_and_thinking_pause():
    rng = Always()
    assert duration("abcde", 60, rng, first=True) == pytest.approx(2.1)
    assert duration("abcde", 30, rng, first=True) == pytest.approx(3.1)
    long = "word " * 18
    assert duration(long, 60, rng, first=True) - duration(long, 60, rng, first=False) == 3
    assert duration("hi", 60, rng, first=True, busy=True) - duration("hi", 60, rng, first=True) == 3
    assert duration("hello", 60, random.Random(1), first=True) != duration(
        "hello", 60, random.Random(2), first=True)


@pytest.mark.parametrize("channel,cap", [("guild", 30), ("whisper", 12), ("dm:1", 12),
                                        ("discord:general", 30)])
@pytest.mark.parametrize("correction", [False, True])
def test_whole_turn_cap_includes_correction_and_burst_gaps(rig, channel, cap, correction):
    rig.engine.rng = Always()
    rig.engine.realism.member(101).credit = float(correction)
    p = plan(rig, channel=channel, text="word " * 100 + "ready")
    assert sum(p.delays) + p.correction_delay <= p.baseline + cap + 1e-9
    assert 0 <= p.extra <= cap + 1e-9
    assert bool(p.correction) == correction
    if correction:
        assert 5 <= p.correction_delay <= 10


LONG = "ready with the flask. we can head over now and get the other supplies. then we can start the next round."


def test_refused_first_burst_line_records_one_drop(rig,monkeypatch):
    rig.engine.rng = Always()
    monkeypatch.setattr(rig.engine,"_style",lambda *a,**kw:[Styled(LONG,LONG)])
    async def refuse(*a,**kw):
        return "realism_busy"
    monkeypatch.setattr(rig.engine,"_send_line",refuse)
    assert not rig.speak_turn(turn(),seconds=60)
    assert rig.store.conn.execute("SELECT count(*) FROM utterance WHERE dropped IS NOT NULL").fetchone()[0] == 1


@pytest.mark.parametrize("channel",["guild","whisper","discord:general","dm:101"])
def test_burst_history_is_one_turn_and_visible_as_it_arrives(rig,monkeypatch,channel):
    e=rig.engine
    e.rng=Always()
    monkeypatch.setattr(e,"_style",lambda *a,**kw:[Styled(LONG,LONG)])
    history=[]
    original=e._record
    def record(*a,**kw):
        result=original(*a,**kw)
        history.append(list(e._own_of(101)))
        return result
    monkeypatch.setattr(e,"_record",record)
    assert len(rig.speak_turn(turn(channel),seconds=80)) >= 2
    assert len(history) >= 2
    assert all(len(rows)==1 for rows in history)
    assert history[0][0][2] != LONG
    assert history[-1][0][2] == LONG
    assert history[-1][0][0] == channel


@pytest.mark.parametrize("phase", list(RaidPhase))
def test_natural_bursts_have_cooldown_and_never_split_during_combat_or_wipe(rig, monkeypatch, phase):
    rig.engine.rng = Always()
    monkeypatch.setattr(rig.engine.raid, "phase", lambda _: phase)
    p = plan(rig, text=LONG)
    assert p.burst == (phase in (RaidPhase.NONE, RaidPhase.TRASH, RaidPhase.KILL))
    assert 1 <= len(p.lines) <= 3
    assert " ".join(s.plain for s in p.lines) == LONG
    if p.burst:
        assert not plan(rig, text=LONG).burst
        rig.sync(rig.advance(91))
        assert plan(rig, text=LONG).burst


def test_split_requires_matching_natural_boundaries():
    assert len(split_line(Styled("a. b. c. d.", "a. b. c. d."))) == 3
    assert split_line(Styled("one clause", "one clause")) == [Styled("one clause", "one clause")]
    assert split_line(Styled("a. b.", "joined")) == [Styled("a. b.", "joined")]


@pytest.mark.parametrize("item", ["[Mr. Pinchy]", "[One; Two]", "{item:123}", "|Hitem:123|h[Thing]|h"])
def test_splitting_cannot_fragment_item_markup(item):
    text = "I brought " + item + ". Here is the rest of the rather long reply to your question."
    assert split_line(Styled(text,text)) == [Styled(text,text)]


@pytest.mark.parametrize("busy", ["dungeon", "zone"])
def test_busy_whisper_never_enters_new_single_line_burst(rig,busy):
    rig.engine.rng = Always()
    if busy == "dungeon":
        rig.engine.world.members[101] = replace(rig.engine.world.members[101],map=574)
    else:
        rig.engine.realism.member(101).zone_at = rig.clock.now()
    p = plan(rig,channel="whisper",text=LONG)
    assert len(p.lines) == 1 and not p.burst and not p.correction


def test_reviewed_typos_are_single_adjacent_substitutions_and_not_names_or_links():
    for word, wrong in SAFE_TYPOS.items():
        diff = [(a, b) for a, b in zip(word, wrong) if a != b]
        assert len(word) == len(wrong) and len(diff) == 1 and diff[0][1] in ADJACENT[diff[0][0]]
        assert wrong not in SAFE_TYPOS
        assert typo(Styled(word, word), {word}, Always()) is None
        assert typo(Styled(word, word), {"Other " + word}, Always()) is None
        assert typo(Styled(word, word), {wrong}, Always()) is None
        altered, correction = typo(Styled(word, word), (), Always())
        assert altered == Styled(wrong, word) and correction == "*" + word
    for text in ("Ready", "Arkon", "[water]", "@ready", "http://water.test", "{water}"):
        assert typo(Styled(text, text), (), Always()) is None


def test_corrections_cannot_exceed_two_percent_even_with_adversarial_random(rig):
    rig.engine.rng = Always()
    n = 0
    for sent in range(1, 1001):
        p = plan(rig)
        n += bool(p.correction)
        assert n <= (sent - 1) * .02
        rig.engine.realism.record_line(101)
    assert 0 < n <= 20
    rig.engine.realism.member(101).credit = 1
    assert plan(rig).correction
    assert not plan(rig).correction  # reserved before delivery, even if cancelled


def test_member_cache_is_bounded_and_new_members_have_no_typo_credit():
    r = Realism()
    for guid in range(MAX_MEMBERS + 5):
        r.record_line(guid)
    assert len(r.members) == MAX_MEMBERS
    assert r.member(0).credit == 0


@pytest.mark.parametrize("kind", [TurnKind.CALLOUT, TurnKind.URGENT])
def test_raid_callouts_are_ineligible(rig, kind):
    assert not rig.engine.realism.eligible(rig.engine, turn(kind=kind), TurnMeta("t1"))


@pytest.mark.parametrize("meta", [TurnMeta("t1", elsewhere=True), TurnMeta("t1", why="hotmic"),
                                  TurnMeta("t1", before_transport=lambda: None)])
def test_exact_text_and_external_transports_are_ineligible(rig, meta):
    assert plan(rig, meta=meta) is None


def test_reported_zone_dungeon_and_boss_state_shorten_game_whispers_only(rig):
    e, now = rig.engine, rig.clock.now()
    t = turn("whisper")
    e.realism.observe(Zone(ts=int(now * 1000), guid=101, zone_id=11, zone_name="Invented"), now)
    assert e.realism.busy(e, t)
    e.realism.observe(Logout(ts=0, guid=101, human=False), now)
    assert not e.realism.busy(e, t)
    e.world.members[101] = replace(e.world.members[101], map=574)
    assert e.realism.busy(e, t)
    e.realism.observe(Boss(ts=0, map=574, instance_id=1, boss_id=2,
                         boss_name="Invented", state="engaged", members=(101,)), now)
    assert e.realism.combat(e, t)
    lines = [Styled("first point", "first point"), Styled("second point", "second point")]
    p = e.realism.plan(e, t, TurnMeta("t1"), e.personas.get(101), lines, [2, 2])
    assert len(p.lines) == 1 and p.lines[0] == lines[0] and not p.correction
    for channel in ("discord:general", "dm:1"):
        dt = turn(channel)
        assert not e.realism.busy(e, dt) and not e.realism.combat(e, dt)
        assert e.realism.safe(e, dt)
    e.realism.observe(Boss(ts=0, map=574, instance_id=1, boss_id=2,
                         boss_name="Invented", state="wiped", members=(101,)), now)
    assert not e.realism.combat(e, t)


def test_real_app_hello_dispatch_clears_old_fight_and_travel_state(tmp_path,clock):
    from test_app_ingress import application, deliver
    async def main():
        async with application(tmp_path,clock) as app:
            e = app.engine
            (tmp_path / "mind.toml").write_text("[talk]\nchattiness=0\nrealism=true\n",encoding="utf-8")
            app._reload_config()
            e.realism.observe(Boss(ts=0,map=574,instance_id=1,boss_id=2,
                boss_name="Invented",state="engaged",members=(101,)),clock.now())
            e.realism.member(101).zone_at = clock.now()
            e.realism.member(101).credit = .5
            assert e.realism.fights
            await deliver(app,dict(type="hello",server="mod-leeroy",version="0.1.0",guild_id=21,human_guid=0))
            assert app.hello is not None
            assert e.cfg.talk.realism
            assert not e.realism.fights
            assert e.realism.member(101).zone_at == float("-inf")
            assert e.realism.member(101).credit == .5
    run(main())


def test_fake_clock_correction_lands_within_five_to_ten_seconds(rig, monkeypatch):
    e = rig.engine
    e.rng = Always()
    e.realism.member(101).credit = 1
    monkeypatch.setattr(e, "_style", lambda *a, **kw: [Styled("ready with a flask", "ready with a flask")])
    assert len(rig.speak_turn(turn(), seconds=50)) == 2
    sent = rig.sent.timed_says()
    assert sent[0][1].text == "reqdy with a flask"
    assert sent[1][1].text == "*ready"
    assert 5 <= sent[1][0] - sent[0][0] <= 10
    assert e.realism.member(101).credit == .02  # correction earns no credit
    assert e.typing_now() == []


@pytest.mark.parametrize("channel", ["discord:general", "dm:101"])
def test_discord_correction_ignores_game_afk_and_keeps_game_activity_untouched(rig, monkeypatch, channel):
    e = rig.engine
    e.rng = Always()
    e.world.human_online = False
    e.world.afk.add(101)
    e.world.members[101] = replace(e.world.members[101], online=False, map=574)
    e.realism.member(101).zone_at = rig.clock.now()
    e.realism.member(101).credit = 1
    before = (e._last_talk_at, e.world.human_last_active, dict(e._last_line_id))
    monkeypatch.setattr(e, "_style", lambda *a, **kw: [Styled("ready with a flask", "ready with a flask")])
    assert len(rig.speak_turn(turn(channel), seconds=60)) == 2
    rows = rig.store.conn.execute("SELECT channel, session_id, to_guid FROM chat_line").fetchall()
    assert len(rows) == 2 and all(r[0] == channel and r[1] is None for r in rows)
    if channel == "dm:101":
        assert all(r[2] == HUMAN for r in rows)
    assert not rig.sent.log
    assert (e._last_talk_at, e.world.human_last_active, dict(e._last_line_id)) == before


def test_dots_stay_fresh_for_a_long_wait(rig):
    async def main():
        task = asyncio.create_task(rig.engine._realistic_sleep(turn(), 51))
        await asyncio.sleep(0)
        for _ in range(10):
            await rig.advance(5)
            phases = rig.engine.turn_phases()
            assert phases and rig.clock.now() - phases[0][2] <= 10
        assert not task.done()
        await rig.advance(1)
        await task
    run(main())


@pytest.mark.parametrize("change", ["late", "combat"])
def test_correction_rechecks_safety_and_ten_second_limit_under_send_lock(rig, monkeypatch, change):
    async def main():
        e = rig.engine
        e.rng = Always()
        e.realism.member(101).credit = 1
        monkeypatch.setattr(e, "_style", lambda *a, **kw: [Styled("ready with a flask", "ready with a flask")])
        task = asyncio.create_task(e.speak(turn()))
        for _ in range(100):
            await rig.advance(.5, .5)
            if rig.sent.says():
                break
        assert len(rig.sent.says()) == 1
        async with e._send_lock:
            await rig.advance(8)
            assert not task.done()
            if change == "late":
                await rig.advance(3)
            else:
                monkeypatch.setattr(e.raid, "phase", lambda _: RaidPhase.COMBAT)
        await rig.advance(1)
        await task
        assert len(rig.sent.says()) == 1
    run(main())


@pytest.mark.parametrize("unsafe", ["afk", "combat", "cancel"])
def test_correction_is_dropped_if_state_changes_during_its_wait(rig, monkeypatch, unsafe):
    async def main():
        e = rig.engine
        e.rng = Always()
        e.realism.member(101).credit = 1
        monkeypatch.setattr(e, "_style", lambda *a, **kw: [Styled("ready with a flask", "ready with a flask")])
        task = asyncio.create_task(e.speak(turn()))
        for _ in range(100):
            await rig.advance(.5, .5)
            if rig.sent.says():
                break
        assert len(rig.sent.says()) == 1
        if unsafe == "afk":
            e.world.afk.add(101)
        elif unsafe == "combat":
            monkeypatch.setattr(e.raid, "phase", lambda _: RaidPhase.COMBAT)
        else:
            task.cancel()
        await rig.advance(20)
        if unsafe == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert len(rig.sent.says()) == 1 and not e.typing_now()
    run(main())


@pytest.mark.parametrize("value", [None, "false", "true", "42", '"true"'])
def test_config_defaults_off_and_invalid_flag_is_off(tmp_path, value):
    path = tmp_path / "mind.toml"
    if value is not None:
        path.write_text("[talk]\nrealism = " + value, encoding="utf-8")
    assert load_config(path).talk.realism is (value == "true")
    path.write_text("[talk]\nrealism = true\nmisspelled = true", encoding="utf-8")
    assert load_config(path).talk.realism


def test_disabled_realism_uses_original_delay_and_no_new_planner(rig, monkeypatch):
    import leeroy_mind.talk.pipeline as pipeline
    e = rig.engine
    e.cfg = replace(e.cfg, talk=replace(e.cfg.talk, realism=False))
    monkeypatch.setattr(e.realism, "plan", lambda *a: pytest.fail("disabled planner"))
    waits = []
    original_sleep = rig.clock.sleep
    async def sleep(seconds):
        waits.append(seconds)
        await original_sleep(seconds)
    monkeypatch.setattr(rig.clock, "sleep", sleep)
    monkeypatch.setattr(pipeline, "typing_delay", lambda *a: 7.25)
    before = rig.clock.now()
    assert rig.speak_turn(turn(), seconds=30)
    assert rig.sent.timed_says()[0][0] - before == pytest.approx(8, abs=1)
    assert [s for s in waits if s > 0] == [7.25]
    assert not e.realism.members
