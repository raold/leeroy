from dataclasses import replace
from datetime import date, timedelta

import pytest

from conftest import make_persona
from leeroy_mind.nightly import motives
from leeroy_mind.nightly.motives import (DEFICIT_RATE, TRAITS, WANT, YEE, met, motive_weights,
                                       sample_traits, step_motives, trait_today, wants)
from leeroy_mind.nightly.places import PLACES, place_kind
from leeroy_mind.storage.db import open_db
from leeroy_mind.storage.store import Store


def encounter(h, a=101, b=102, *, night=None, setting=None, private=False, status="done"):
    idx = h.store.conn.execute("SELECT COALESCE(MAX(idx),0)+1 FROM encounter").fetchone()[0]
    with h.store.transaction():
        return h.store.conn.execute(
            "INSERT INTO encounter(night,idx,a,b,setting,status,private) VALUES (?,?,?,?,?,?,?)",
            (night or h.night, idx, a, b, setting or PLACES["idling"][0], status, int(private))).lastrowid


def hours(h, guid, amount, *, night=None):
    with h.store.transaction():
        h.store.conn.execute("INSERT OR REPLACE INTO wow_hours(night,guid,hours) VALUES (?,?,?)",
                             (night or h.night, guid, amount))


def beat(h, guid, mood, *, night=None, owner=None, evidence=None, status="active"):
    with h.store.transaction():
        tid = h.store.conn.execute(
            "INSERT INTO life_thread(guid,title,beats_max,next_beat_at,status,started) "
            "VALUES (?,'a deadline',4,?,'active',?)", (guid, h.T + 86400, h.T)).lastrowid
        bid = h.store.conn.execute("INSERT INTO life_beat(thread_id,night,text,ts) VALUES (?,?,?,?)",
                                   (tid, night or h.night, "Things changed today.", h.T)).lastrowid
    h.mem(guid if owner is None else owner, "Things changed today.", source="offscreen",
          evidence=[f"beat:{bid}"] if evidence is None else evidence, valence=mood, status=status)
    return bid


def erase_traits(h, guid=101):
    with h.store.transaction():
        h.store.conn.execute("DELETE FROM trait_state WHERE night=? AND guid=?", (h.night, guid))


def test_places_are_exact_and_reverse_lookup_does_not_guess():
    assert PLACES == {
        "battlegrounds": ("a Wintergrasp battle", "an Arathi Basin queue"),
        "arena": ("an arena queue",), "gathering": ("herb farming in Sholazar Basin",),
        "crafting": ("the Dalaran crafting district",),
        "dungeons": ("a heroic Utgarde Pinnacle run",),
        "idling": ("the Orgrimmar bank steps", "voice chat late at night"),
        "auction_house": ("the Orgrimmar auction house",), "fishing": ("the docks in Dalaran",),
        "collecting": ("a rare-mount camp in Northrend",),
    }
    for kind, settings in PLACES.items():
        assert all(place_kind(setting) == kind for setting in settings)
    assert place_kind("a made-up fishing spot") is None


def test_motive_weights_from_persona_fields():
    weights = motive_weights(make_persona(101, "Arkon"))
    assert weights["mechanics"] == pytest.approx(0.9)
    assert weights["advancement"] == pytest.approx(0.6)
    assert weights["teamwork"] == pytest.approx(0.4)
    assert weights["escapism"] == pytest.approx(0.3)
    assert set(weights) == set(YEE) and all(0.1 <= value <= 0.9 for value in weights.values())


@pytest.mark.parametrize("motivation,expected", [
    ("achiever", {"advancement": 0.6, "mechanics": 0.9}),
    ("competitor", {"competition": 0.7}),
    ("social", {"socialising": 0.6, "relationship": 0.5}),
    ("explorer", {"discovery": 0.6, "role_play": 0.4}),
])
def test_each_game_motivation(motivation, expected):
    weights = motive_weights(make_persona(101, "Arkon", motivation=motivation, outside_raids=[]))
    assert {name: weights[name] for name in expected} == pytest.approx(expected)


@pytest.mark.parametrize("thinks,expected", [
    ("theorycrafter", {"mechanics": 0.9}),
    ("lore", {"discovery": 0.5, "role_play": 0.6}),
    ("collector", {"customisation": 0.7, "discovery": 0.4}),
    ("competitor", {"competition": 0.6}),
    ("social", {"socialising": 0.5, "teamwork": 0.5}),
])
def test_each_way_of_thinking(thinks, expected):
    weights = motive_weights(make_persona(101, "Arkon", thinks_as=thinks, outside_raids=[]))
    assert {name: weights[name] for name in expected} == pytest.approx(expected)


@pytest.mark.parametrize("interest,motive,delta", [
    ("battlegrounds", "competition", 0.1), ("arena", "competition", 0.1),
    ("collecting", "customisation", 0.1), ("fishing", "escapism", 0.1),
    ("idling", "escapism", 0.1), ("crafting", "advancement", 0.1),
    ("auction_house", "advancement", 0.1), ("dungeons", "teamwork", 0.1),
    ("gathering", "escapism", 0.05),
])
def test_each_outside_interest(interest, motive, delta):
    plain = make_persona(101, "Arkon", outside_raids=[])
    interested = replace(plain, game=replace(plain.game, outside_raids=(interest,)))
    assert motive_weights(interested)[motive] - motive_weights(plain)[motive] == pytest.approx(delta)


def test_officer_habits_neuroticism_and_weight_cap():
    p = make_persona(101, "Arkon", guild_role="officer", outside_raids=["fishing", "idling"],
                     habits=[dict(substance=s, when="daily", odds=0.5, max_state=state)
                             for s, state in (("alcohol", "tipsy"), ("weed", "high"), ("caffeine", "wired"))])
    p = replace(p, big_five=replace(p.big_five, neuroticism=replace(p.big_five.neuroticism, score=7)))
    assert motive_weights(p)["escapism"] == pytest.approx(0.8)
    assert motive_weights(p)["teamwork"] == pytest.approx(0.5)
    p = replace(p, game=replace(p.game, motivation="competitor", thinks_as="competitor",
                               outside_raids=("arena", "battlegrounds")))
    assert motive_weights(p)["competition"] == 0.9


def test_events_count_only_participants_and_raid_fights_in_window(h):
    ctx = h.ctx()
    h.loot(h.T, 101, "A test trinket")
    h.boss(h.T, 631, 0, "wiped", (101, 102))
    assert met(ctx, 101) == {"advancement", "mechanics", "teamwork"}
    assert met(ctx, 103) == set()
    h.boss(h.T + 1, 631, 0, "killed", (101,))
    assert "competition" in met(ctx, 101)
    for t, map_id, boss_id, state in ((ctx.window[0] - 1, 631, 0, "killed"),
                                     (ctx.window[1], 631, 0, "killed"),
                                     (h.T, 578, 0, "killed"), (h.T, 631, 13, "killed"),
                                     (h.T, 631, 0, "started")):
        h.boss(t, map_id, boss_id, state, (103,))
    h.loot(ctx.window[1], 103, "A late trinket")
    assert met(ctx, 103) == set()


def test_bg_win_requires_membership_and_known_matching_faction(h):
    h.bg(h.T, (101, 102), winner="horde")
    assert "competition" in met(h.ctx(), 101)
    assert "competition" not in met(h.ctx(), 102)
    assert "competition" not in met(h.ctx(), 103)
    h.bg(h.T, (102,), winner="alliance")
    assert "competition" in met(h.ctx(), 102)
    member = h.store.cast()[104]
    h.store.upsert_cast([replace(member, race=0)], h.T)
    h.bg(h.T, (104,), winner="alliance")
    assert "competition" not in met(h.ctx(), 104)


def test_socialising_requires_five_sent_lines_or_two_completed_encounters(h):
    ctx = h.ctx()
    for i in range(4):
        h.utter(h.T + i, 101, "hello")
    h.utter(h.T + 5, 101, "never sent", dropped="offline")
    h.utter(ctx.window[1], 101, "tomorrow")
    assert "socialising" not in met(ctx, 101)
    h.utter(h.T + 6, 101, "fifth line")
    assert "socialising" in met(ctx, 101)
    encounter(h, 102, 103)
    encounter(h, 102, 104, status="rejected")
    encounter(h, 102, 104, status="planned")
    encounter(h, 102, 104, night="2026-10-28")
    assert "socialising" not in met(ctx, 102)
    encounter(h, 102, 105)
    assert "socialising" in met(ctx, 102)
    assert "socialising" not in met(ctx, 103)


@pytest.mark.parametrize("speaker,target", [(1, 101), (101, 1)])
def test_either_direction_of_human_whisper_meets_relationship(h, speaker, target):
    h.say(h.T, speaker, "hello", channel="whisper", to=target)
    assert "relationship" in met(h.ctx(), 101)
    assert "relationship" not in met(h.ctx(), 102)


def test_other_private_conversations_never_satisfy_relationship(h):
    ctx = h.ctx()
    h.say(h.T, 1, "hello", channel="whisper", to=102)
    h.say(h.T, 101, "hello", channel="whisper", to=103)
    h.say(h.T, 1, "hello", channel="guild", to=101)
    h.say(ctx.window[1], 101, "late", channel="whisper", to=1)
    encounter(h, 103, 104, private=True)
    encounter(h, 101, 102, private=True, status="rejected")
    assert "relationship" not in met(ctx, 101)
    encounter(h, 101, 103, private=True)
    assert "relationship" in met(ctx, 101)


@pytest.mark.parametrize("kind", ["collecting", "fishing"])
def test_discovery_customisation_from_participated_completed_settings(h, kind):
    encounter(h, 101, 102, setting=PLACES[kind][0])
    assert {"discovery", "customisation"} <= met(h.ctx(), 101)
    assert {"discovery", "customisation"}.isdisjoint(met(h.ctx(), 103))
    encounter(h, 103, 104, setting=PLACES[kind][0], status="planned")
    assert {"discovery", "customisation"}.isdisjoint(met(h.ctx(), 103))


def test_role_play_uses_owned_active_dated_memory_and_exact_ref_kind(h):
    ctx = h.ctx()
    for owner, t, status, ref in ((102, h.T, "active", "story:1"),
                                 (101, ctx.window[0] - 1, "active", "story:2"),
                                 (101, h.T, "rejected", "chronicle:3"),
                                 (101, h.T, "active", "notstory:4"),
                                 (101, h.T, "active", "story:")):
        h.mem(owner, "A story", t=t, status=status, evidence=[ref])
    assert "role_play" not in met(ctx, 101)
    h.mem(101, "Today's story, extracted later", t=h.T, t_created=ctx.window[1] + 1,
          evidence=["chronicle:1"])
    assert "role_play" in met(ctx, 101)


def test_escapism_requires_four_hours_tonight(h):
    hours(h, 101, 16, night="2026-10-28")
    hours(h, 101, 3.99)
    assert "escapism" not in met(h.ctx(), 101)
    hours(h, 101, 4)
    assert "escapism" in met(h.ctx(), 101)


def test_deficits_fill_unmet_and_reset_when_met(h):
    for day in range(3):
        step_motives(h.ctx(night=str(date(2026, 10, 26) + timedelta(days=day))))
    row = h.store.conn.execute("SELECT deficit FROM motive WHERE guid=101 AND motive='mechanics'").fetchone()
    assert row[0] == pytest.approx(0.9 * DEFICIT_RATE * 3)
    assert wants(h.store, 101) == []
    ctx = h.ctx()
    step_motives(ctx)
    assert wants(h.store, 101)[0] == "mechanics" and WANT == 1.0
    before = [tuple(row) for row in h.store.conn.execute("SELECT * FROM motive ORDER BY guid,motive")]
    step_motives(h.ctx())
    assert before == [tuple(row) for row in h.store.conn.execute("SELECT * FROM motive ORDER BY guid,motive")]
    h.boss(h.T + 86400, 631, 0, "killed", (101,))
    step_motives(h.ctx(night="2026-10-30"))
    deficits = dict(h.store.conn.execute("SELECT motive,deficit FROM motive WHERE guid=101"))
    assert all(deficits[m] == 0 for m in ("mechanics", "teamwork", "competition"))
    assert h.store.conn.execute("SELECT COUNT(*) FROM motive WHERE guid=1").fetchone()[0] == 0


def test_weights_refresh_and_deficits_cap_for_away_members(h):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (101,'rest',?,?)",
                             (h.T - 100, h.T + 86400))
        h.store.conn.execute("INSERT INTO motive VALUES (101,'competition',0.1,1.99,0)")
    h.persona(101, motivation="competitor", thinks_as="competitor")
    step_motives(h.ctx())
    row = h.store.conn.execute("SELECT weight,deficit FROM motive WHERE guid=101 AND motive='competition'").fetchone()
    assert tuple(row) == (0.9, 2.0)


def test_motives_roll_back_the_entire_unit(h, monkeypatch):
    original = motives.motive_weights
    def failed(p):
        if p.guid == 102:
            raise ValueError("synthetic failure after first member")
        return original(p)
    monkeypatch.setattr(motives, "motive_weights", failed)
    ctx = h.ctx()
    step_motives(ctx)
    assert h.count("motive") == 0 and not ctx.done("wowlife", "motives")
    assert ctx.failures["wowlife"] == 1


def test_daily_traits_seeded_centred_and_persisted(h):
    ctx = h.ctx()
    first = sample_traits(ctx, 101)
    assert first == sample_traits(h.ctx(), 101)
    assert h.count("trait_state") == 5 and not ctx.done("wowlife", "traits")
    h.store.put_state(replace(h.store.get_state(101), altered_state="drunk"))
    assert first == sample_traits(h.ctx(), 101)  # one day's sample never changes on retry
    assert trait_today(h.store, h.night, 101, ctx.persona(101)) == first
    h.store.put_state(replace(h.store.get_state(101), altered_state="sober"))
    values = [sample_traits(h.ctx(night=str(date(2025, 1, 1) + timedelta(days=i))), 101)["extraversion"]
              for i in range(200)]
    assert sum(values) / len(values) == pytest.approx(7, abs=0.2)
    assert all(1 <= value <= 10 for value in values)
    assert trait_today(h.store, "2024-01-01", 101, ctx.persona(101)) == {
        "openness": 5, "conscientiousness": 6, "extraversion": 7, "agreeableness": 5, "neuroticism": 2}


def test_raid_drink_and_long_night_trait_shifts(h):
    base = sample_traits(h.ctx(), 101)
    erase_traits(h)
    h.boss(h.T, 631, 0, "killed", (101,))
    h.store.put_state(replace(h.store.get_state(101), altered_state="drunk"))
    hours(h, 101, 10)
    changed = sample_traits(h.ctx(), 101)
    assert changed["extraversion"] == pytest.approx(min(10, base["extraversion"] + 1.5))
    assert changed["conscientiousness"] == pytest.approx(max(1, base["conscientiousness"] - 2))
    assert changed["agreeableness"] == pytest.approx(max(1, base["agreeableness"] - 0.5))
    assert changed["openness"] == base["openness"]


@pytest.mark.parametrize("mood", [-0.1, 0.1, -0.8, 0.8])
def test_life_beat_traits_use_owned_memory_valence_even_without_emotion(h, mood):
    h.persona(101, big_five={trait: {"score": 5, "behaviour": "ordinary today"} for trait in TRAITS})
    base = sample_traits(h.ctx(), 101)
    erase_traits(h)
    beat(h, 101, mood)
    assert h.count("emotion") == 0
    changed = sample_traits(h.ctx(), 101)
    assert changed["neuroticism"] == pytest.approx(max(1, min(10, base["neuroticism"] - mood)))
    assert changed["extraversion"] == pytest.approx(max(1, min(10, base["extraversion"] + 0.5 * mood)))


def test_other_or_old_life_beats_do_not_shift_traits(h):
    base = sample_traits(h.ctx(), 101)
    erase_traits(h)
    beat(h, 102, 0.8)
    beat(h, 101, 0.8, owner=102)
    beat(h, 101, 0.8, night="2026-10-28")
    beat(h, 101, 0.8, status="rejected")
    beat(h, 101, 0.8, evidence=["beat:99999"])
    assert sample_traits(h.ctx(), 101) == base


def test_partial_daily_rows_are_preserved_and_completed(h):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO trait_state VALUES (?,?,?,?)", (h.night, 101, "openness", 9.25))
    sample = sample_traits(h.ctx(), 101)
    assert sample["openness"] == 9.25 and set(sample) == set(TRAITS)
    assert h.count("trait_state") == 5


@pytest.mark.parametrize("noise,expected", [(-20, 1.0), (20, 10.0)])
def test_daily_traits_respect_score_bounds(h, monkeypatch, noise, expected):
    class Extreme:
        def gauss(self, mean, spread):
            return noise
    ctx = h.ctx()
    monkeypatch.setattr(ctx, "rng", lambda *parts: Extreme())
    assert sample_traits(ctx, 101) == dict.fromkeys(TRAITS, expected)


def test_one_life_beat_is_not_counted_twice_for_duplicate_memory_refs(h):
    h.persona(101, big_five={trait: {"score": 5, "behaviour": "ordinary today"} for trait in TRAITS})
    base = sample_traits(h.ctx(), 101)
    erase_traits(h)
    bid = beat(h, 101, 0.1)
    h.mem(101, "An extra record of that beat", source="offscreen", valence=0.1,
          evidence=[f"beat:{bid}", f"beat:{bid}"])
    sample = sample_traits(h.ctx(), 101)
    assert sample["extraversion"] == pytest.approx(base["extraversion"] + 0.05)
    assert sample["neuroticism"] == pytest.approx(base["neuroticism"] - 0.1)


@pytest.mark.parametrize("reader", ["met", "wants", "traits"])
def test_public_reads_release_snapshot_and_preserve_callers_transaction(h, reader):
    ctx = h.ctx()
    p = ctx.persona(101)
    with h.store.transaction():
        path = h.store.conn.execute("PRAGMA database_list").fetchone()[2]
        h.store.conn.execute("INSERT INTO motive VALUES (101,'competition',0.3,0,0)")
        h.store.conn.execute("INSERT INTO trait_state VALUES (?,?,?,?)", (h.night, 101, "openness", 5))
        h.store.conn.execute("INSERT INTO wow_hours VALUES (?,?,?)", (h.night, 101, 0))
    read = {"met": lambda: met(ctx, 101), "wants": lambda: wants(h.store, 101),
            "traits": lambda: trait_today(h.store, h.night, 101, p)["openness"]}[reader]
    initial = {"met": set(), "wants": [], "traits": 5}[reader]
    changed = {"met": {"escapism"}, "wants": ["competition"], "traits": 6}[reader]
    other = Store(open_db(path), clock=h.clock)
    try:
        assert read() == initial
        with other.transaction():
            other.conn.execute("UPDATE motive SET deficit=2 WHERE guid=101")
            other.conn.execute("UPDATE trait_state SET value=6 WHERE guid=101")
            other.conn.execute("UPDATE wow_hours SET hours=4 WHERE guid=101")
        # A standalone read must not retain the old WAL snapshot across calls.
        assert read() == changed
        with pytest.raises(ValueError), h.store.transaction():
            h.store.set_meta("motives_read_rollback", "pending")
            assert read() == changed
            raise ValueError("roll back caller")
        assert h.store.get_meta("motives_read_rollback") is None
    finally:
        other.close()
