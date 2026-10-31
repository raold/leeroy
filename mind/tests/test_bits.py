import random
from datetime import datetime, timedelta, timezone

from conftest import _base_persona_dict
from leeroy_mind.clock import night_of
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.talk.bits import BitScheduler
from leeroy_mind.talk.raid import RaidPhase

DAY = 86400.0
SEEDS = range(200)


def persona(guid=101, name="Arkon", **bit):
    d = _base_persona_dict(guid, name)
    b = {"id": "kettle", "instruction": "complain that the break-room kettle is broken again.",
         "min_days_between": 14.0, "voice_bit": False, "contexts": ["any"], "lines": 1,
         "dates": []}
    b.update(bit)
    d["bits"] = [b]
    return persona_from_dict(d)


def pick(store, clock, p, seed=1, *, context=("org",), raid=RaidPhase.NONE, mood=0.0,
         local=None, **kw):
    s = BitScheduler(store, clock, random.Random(seed), **kw)
    return s.pick(p, context=set(context), raid=raid, mood=mood, local=local or clock.local())


def fired(store, clock, p, n=400, **kw) -> int:
    return sum(pick(store, clock, p, seed, **kw) is not None for seed in range(n))


def use(store, guid, bit_id, ts, *, voice=False, local=None):
    when = local or datetime.fromtimestamp(ts, tz=timezone.utc)
    store.record_bit(guid, bit_id, ts, night_of(when), voice, None)


def test_never_in_combat_or_wipe(store, clock):
    p = persona()
    for phase in (RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF):
        assert all(pick(store, clock, p, s, raid=phase, p_fire=1.0) is None for s in SEEDS)
    for phase in (RaidPhase.NONE, RaidPhase.TRASH, RaidPhase.KILL):
        assert pick(store, clock, p, raid=phase, p_fire=1.0) == p.bits[0]


def test_cooldown_respected(store, clock):
    p = persona(min_days_between=14.0)
    use(store, 101, "kettle", clock.now() - 3 * DAY)
    assert all(pick(store, clock, p, s, p_fire=1.0) is None for s in SEEDS)


def test_cooldown_jitter_is_seeded_by_the_last_use(store, clock):
    p = persona(min_days_between=10.0)
    use(store, 101, "kettle", clock.now() - 7.9 * DAY)  # under the 0.8 floor: always cooling
    assert all(pick(store, clock, p, s, p_fire=1.0) is None for s in SEEDS)
    q = persona(guid=102, name="Belra", min_days_between=10.0)
    use(store, 102, "kettle", clock.now() - 12.1 * DAY)  # past the 1.2 ceiling: always due
    assert all(pick(store, clock, q, s, p_fire=1.0) == q.bits[0] for s in SEEDS)
    # inside the jitter band the answer depends on (guid, bit, last use), never on the rng
    for guid in range(103, 123):
        r = persona(guid=guid, name=f"M{guid}", min_days_between=10.0)
        use(store, guid, "kettle", clock.now() - 10.0 * DAY)
        got = {pick(store, clock, r, s, p_fire=1.0) is None for s in range(30)}
        assert len(got) == 1


def test_one_voice_bit_per_night(store, clock):
    voice = persona(voice_bit=True, id="anthem")
    plain = persona(voice_bit=False, id="kettle")
    s = BitScheduler(store, clock, random.Random(1))
    other = persona(guid=102, name="Belra", voice_bit=True, id="opera")
    s.record(102, other.bits[0], clock.now(), None)  # Belra's voice bit, tonight
    assert all(pick(store, clock, voice, sd, p_fire=1.0) is None for sd in SEEDS)
    assert pick(store, clock, plain, p_fire=1.0) == plain.bits[0]
    # 01:00 is still the same night; 07:00 is the next one
    late = clock.local() + timedelta(hours=5)
    morning = clock.local() + timedelta(hours=11)
    assert pick(store, clock, voice, local=late, p_fire=1.0) is None
    assert pick(store, clock, voice, local=morning, p_fire=1.0) == voice.bits[0]


def test_special_date_ignores_cooldown(store, clock):
    p = persona(dates=["09-19"], min_days_between=14.0, voice_bit=True, id="anniversary")
    local = datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc)
    use(store, 101, "anniversary", (local - timedelta(days=1)).timestamp())
    use(store, 102, "opera", local.timestamp() - 600, voice=True)  # a voice bit tonight
    n = fired(store, clock, p, local=local)
    assert 0.4 <= n / 400 <= 0.6
    # 02:00 on the 20th is still the night of the 19th
    assert fired(store, clock, p, local=local + timedelta(hours=5)) > 0


def test_special_date_bit_fires_once_a_night(store, clock):
    p = persona(dates=["09-19"], id="anniversary")
    local = datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc)
    use(store, 101, "anniversary", local.timestamp() - 3600)
    assert fired(store, clock, p, local=local) == 0
    next_year = datetime(2027, 9, 19, 21, 0, tzinfo=timezone.utc)
    assert fired(store, clock, p, local=next_year) > 0


def test_every_bit_plays_at_most_once_a_night(store, clock):
    p = persona(min_days_between=0.0)  # a seed bit with no cooldown at all
    use(store, 101, "kettle", clock.now() - 2 * 3600)  # earlier tonight
    assert all(pick(store, clock, p, s, p_fire=1.0) is None for s in SEEDS)
    tomorrow = clock.local() + timedelta(days=1)
    assert pick(store, clock, p, local=tomorrow, p_fire=1.0) == p.bits[0]


def test_context_must_match(store, clock):
    p = persona(contexts=["after_wipe"])
    assert all(pick(store, clock, p, s, context=("org",), p_fire=1.0) is None for s in SEEDS)
    assert pick(store, clock, p, context=("trash", "after_wipe"), p_fire=1.0) == p.bits[0]
    anywhere = persona(contexts=["any"])
    assert pick(store, clock, anywhere, context=("loot",), p_fire=1.0) == anywhere.bits[0]
    unset = persona(contexts=[])
    assert pick(store, clock, unset, context=("late_night",), p_fire=1.0) == unset.bits[0]


def test_bad_mood_blocks_a_bit(store, clock):
    p = persona()
    assert all(pick(store, clock, p, s, mood=-0.6, p_fire=1.0) is None for s in SEEDS)
    assert pick(store, clock, p, mood=-0.5, p_fire=1.0) == p.bits[0]


def test_fires_at_p_fire(store, clock):
    p = persona()
    n = fired(store, clock, p, n=2000)
    assert 0.12 <= n / 2000 <= 0.18
    assert fired(store, clock, p, p_fire=0.0) == 0


def test_record_stores_the_night_and_the_voice_flag(store, clock):
    s = BitScheduler(store, clock, random.Random(1))
    p = persona(voice_bit=True, id="anthem")
    sid = store.start_session(clock.now())
    s.record(101, p.bits[0], clock.now(), sid)
    assert store.last_bit_use(101, "anthem") == clock.now()
    assert store.voice_bit_used_on(night_of(clock.local()))
    assert not store.voice_bit_used_on("2026-10-28")


def test_turn_instruction_mentions_lines(store, clock):
    s = BitScheduler(store, clock, random.Random(1))
    one = s.turn_instruction(persona(lines=1).bits[0])
    three = s.turn_instruction(persona(lines=3).bits[0])
    assert "one short line" in one and "3 short lines" in three
    # the rules say one short line, so a longer bit says it is the exception
    assert "past one line" in three and "past one line" not in one
    for text in (one, three):
        assert "break-room kettle is broken again" in text and ".." not in text
        assert "{" not in text and not any(c in text for c in ";—–")


def test_a_bit_reads_as_her_habit_brought_out_now(store, clock):
    """Prompts minor 9: generated bits are written as a habit or a condition ("If someone
    mentions fish, ...", "Once in a while, ..."), which read oddly as a do-it-now instruction;
    and a model that put blank lines between a bit's lines stopped after the first one (the
    live preset stops on a blank line), with the bit counted as played."""
    s = BitScheduler(store, clock, random.Random(1))
    text = s.turn_instruction(persona(
        instruction="If someone mentions fish, complain that nobody eats the feasts.").bits[0])
    assert text.startswith("Your running bit, the way you usually do it: If someone mentions")
    assert "Do it now, in one short line." in text
    three = s.turn_instruction(persona(lines=3).bits[0])
    assert "one line per row, with no blank lines between them" in three


def test_turn_instruction_keeps_the_instructions_own_ending(store, clock):
    s = BitScheduler(store, clock, random.Random(1))
    for given, kept in (("yell that the kettle is broken!", "broken!"),
                        ("ask who keeps breaking the kettle?", "kettle?"),
                        ("moan about the kettle!.", "kettle!"),
                        ("sigh about the kettle...", "kettle.")):
        text = s.turn_instruction(persona(instruction=given).bits[0])
        assert kept + " Do it" in text
        assert not any(x in text for x in ("!.", "?.", ".."))


def test_a_dated_bit_plays_only_on_its_dates(store, clock):
    """Rehearsal 2 (judge B 1): dates only raised the odds, so a dated bit joined the ordinary
    pool every night."""
    p = persona(dates=["09-19"], id="anniversary", min_days_between=0.0)
    off = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)
    assert fired(store, clock, p, local=off, p_fire=1.0) == 0
    assert fired(store, clock, p, local=datetime(2026, 9, 19, 21, 0, tzinfo=timezone.utc)) > 0


def test_a_birthday_bit_needs_a_birthday_tonight_and_names_it(store, clock):
    """Rehearsal 2: a member announced a birthday nobody had. A dateless birthday bit plays
    only on a night that is a cast member's birthday, and its instruction names them."""
    from leeroy_mind.talk.bits import birthday_line
    p = persona(id="birthday_announce", instruction="announce a member's birthday to the guild")
    night = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)
    s = BitScheduler(store, clock, random.Random(1))
    assert all(BitScheduler(store, clock, random.Random(i)).pick(
        p, context={"org"}, raid=RaidPhase.NONE, mood=0.0, local=night) is None
        for i in range(100))
    got = [BitScheduler(store, clock, random.Random(i)).pick(
        p, context={"org"}, raid=RaidPhase.NONE, mood=0.0, local=night, birthday="Dunna")
        for i in range(100)]
    assert 30 <= sum(b is not None for b in got) <= 70  # SPECIAL_P, on a cast birthday
    line = birthday_line(["Dunna"], 1)
    text = s.turn_instruction(p.bits[0], line)
    assert "Tonight is Dunna's birthday: it's Dunna you wish a happy birthday." in text
    assert "Dunna" not in s.turn_instruction(persona().bits[0], line)


def test_a_dated_birthday_bit_plays_only_on_its_dates_and_a_cast_birthday(store, clock):
    """Fix round 1 (review I6): the birthday bit ignored its own dates. A bit with dates plays
    only on them, a birthday bit only on a cast birthday too; a dateless one on any cast
    birthday."""
    dated = persona(id="birthday_announce", instruction="announce a member's birthday",
                    dates=["06-03"])
    plain = persona(id="birthday_announce", instruction="announce a member's birthday")
    off = datetime(2026, 10, 31, 21, 0, tzinfo=timezone.utc)
    on = datetime(2026, 6, 3, 21, 0, tzinfo=timezone.utc)

    def picks(p, local, birthday):
        return sum(BitScheduler(store, clock, random.Random(i)).pick(
            p, context={"org"}, raid=RaidPhase.NONE, mood=0.0, local=local,
            birthday=birthday) is not None for i in range(100))
    assert picks(dated, off, "Dunna") == 0  # not its date
    assert picks(dated, on, None) == 0  # its date, nobody's birthday
    assert 30 <= picks(dated, on, "Dunna") <= 70
    assert picks(plain, off, None) == 0 and 30 <= picks(plain, off, "Dunna") <= 70


def test_a_shared_birthday_names_one_or_two_and_counts_the_rest():
    """When several members share a birthday, one or two are named and the rest
    counted."""
    from leeroy_mind.talk.bits import birthday_line
    assert birthday_line(["Dunna"], 1) == ("Tonight is Dunna's birthday: it's Dunna you wish a "
                                           "happy birthday.")
    assert birthday_line(["Dunna", "Coros"], 2) == ("Tonight is the birthday of Dunna and "
                                                    "Coros: wish them a happy birthday.")
    assert birthday_line(["Dunna", "Coros"], 30) == (
        "Tonight is the birthday of Dunna, Coros and 28 others in the guild: wish them all a "
        "happy birthday.")
