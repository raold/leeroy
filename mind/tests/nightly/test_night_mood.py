"""PAD temperament, bounded feelings, calendar-night decay, and private emotional recall."""
import json
import math
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from conftest import make_persona, run
from nightly.conftest import no_real_name
from leeroy_mind.nightly import knowledge, mood
from leeroy_mind.nightly.reflect import OCC
from leeroy_mind.nightly.settings import night_bounds


def ev(state, map_id=631, boss=11, **kwargs):
    return {"type": "boss", "payload": json.dumps({"map": map_id, "boss_id": boss, "state": state}), **kwargs}


def feeling(h, owner=101, kind="anger", target=103, intensity=3, why="That pull went badly.",
            *, refs=None, night=None, ts=None):
    if refs is None:
        event = h.boss(h.T, 631, 11, "started", (owner, target))
        refs = [f"event:{event}"]
    return h.store.conn.execute("INSERT INTO emotion(guid,night,type,target,intensity,why,source,evidence,ts) "
        "VALUES (?,?,?,?,?,?,'reflect',?,?)", (owner, night or h.night, kind, target, intensity,
        why, json.dumps(refs), h.T if ts is None else ts)).lastrowid


def pad(h, guid, point, *, updated=None):
    h.store.conn.execute("INSERT OR REPLACE INTO pad_mood(guid,p,a,d,updated) VALUES (?,?,?,?,?)",
                         (guid, *point, h.T if updated is None else updated))


def point(h, guid):
    row = h.store.conn.execute("SELECT p,a,d FROM pad_mood WHERE guid=?", (guid,)).fetchone()
    return tuple(row)


def trait(h, guid, name, value, *, night=None):
    h.store.conn.execute("INSERT OR REPLACE INTO trait_state(night,guid,trait,value) VALUES (?,?,?,?)",
                         (night or h.night, guid, name, value))


def memories(h, guid):
    return [m for m in h.rows(guid) if (m.key or "").startswith("emotion:")]


def test_baseline_signs_coefficients_and_clamping():
    calm = make_persona(101, "Arkon")
    tense = replace(calm, big_five=replace(calm.big_five,
                    neuroticism=replace(calm.big_five.neuroticism, score=9)))
    p1, a1, _ = mood.baseline(calm)
    p2, a2, _ = mood.baseline(tense)
    assert p2 < p1 and a2 > a1
    extremes = replace(calm.big_five, **{name: replace(getattr(calm.big_five, name), score=10)
        for name in ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism")})
    assert mood.baseline(replace(calm, big_five=extremes)) == pytest.approx((0.61, 1.0, 0.7))


@pytest.mark.parametrize("point,name", [
    ((1, 1, 1), "exuberant"), ((-1, -1, -1), "bored"), ((1, 1, -1), "dependent"),
    ((-1, -1, 1), "disdainful"), ((1, -1, 1), "relaxed"), ((-1, 1, -1), "anxious"),
    ((1, -1, -1), "docile"), ((-1, 1, 1), "hostile"), ((0, 0, 0), "exuberant"),
])
def test_octants(point, name):
    assert mood.octant(*point) == name


def test_pull_push_decay_unknown_types_and_bounds():
    base = (0.1, 0.0, 0.1)
    pulled = mood.step_mood(base, base, [("anger", 3)], nights=1)
    assert pulled[0] < base[0] and pulled[1] > base[1]
    assert math.dist(base, pulled) == pytest.approx(0.3)
    assert max(abs(a - b) for a, b in zip(pulled, base)) <= mood.MOOD_CAP
    far = (-0.8, 0.9, 0.5)
    pushed = mood.step_mood(far, far, [("anger", 2)], nights=0)
    assert pushed[0] < far[0] and pushed[1] > far[1] and pushed[2] > far[2]
    assert mood.step_mood((0.6, 0.6, 0.6), base, [], nights=2) == pytest.approx((0.35, 0.3, 0.35))
    assert mood.step_mood(base, base, [("awe", 3)], nights=1) == base
    assert all(-1 <= x <= 1 for x in mood.step_mood((-1, 1, 1), (-1, 1, 1), [("anger", 300)], nights=0))


def test_emotion_centre_is_intensity_weighted():
    start = (0, 0, 0)
    c = tuple((3 * x + y) / 4 for x, y in zip(mood.OCC_PAD["joy"], mood.OCC_PAD["fear"]))
    expected = tuple(x / math.dist(c, start) * 0.3 for x in c)
    assert mood.step_mood(start, start, [("joy", 3), ("fear", 1)], nights=0) == pytest.approx(expected)


def test_raid_mood_hard_kill_rough_night_final_and_nonfight_filter():
    hard = [ev("wiped")] * 6 + [ev("killed")]
    value, reasons = mood.night_mood(hard)
    assert value == pytest.approx(0.1) and "hard kill: Sindragosa" in reasons
    assert mood.night_mood([ev("wiped", boss=12)] * 8)[0] == -0.5
    assert mood.night_mood([ev("killed", boss=12)])[0] == pytest.approx(0.4)
    assert mood.night_mood([ev("killed", map_id=1), ev("wiped", boss=13),
                            ev("killed", map_id=724, boss=4)])[0] == 0
    assert mood.night_mood([ev("killed") for _ in range(10)])[0] == pytest.approx(0.3)
    assert mood.night_mood([ev("killed", boss=12)] * 10)[0] == 0.5


def test_hard_kill_wipes_must_precede_kill_and_match_boss():
    assert mood.night_mood([ev("killed")] + [ev("wiped")] * 6)[0] == pytest.approx(-0.2)
    assert mood.night_mood([ev("wiped", boss=10)] * 6 + [ev("killed")])[0] == pytest.approx(-0.2)
    unordered = [ev("killed", ts=20, id=2)] + [ev("wiped", ts=n, id=n + 3) for n in range(6)]
    assert mood.night_mood(unordered)[0] == pytest.approx(0.1)


def test_guild_mood_carries_by_elapsed_nights_and_emotion_strength_uses_today(h):
    for n in range(8):
        h.boss(h.T + n, 631, 11, "wiped", (101, 102))
    trait(h, 101, "neuroticism", 2)
    trait(h, 102, "neuroticism", 5)
    trait(h, 103, "neuroticism", 8)
    ctx = h.ctx()
    run(mood.stage_mood(ctx))
    row = h.store.conn.execute("SELECT * FROM guild_mood WHERE night=?", (h.night,)).fetchone()
    assert row["value"] == -0.5
    strengths = dict(h.store.conn.execute("SELECT guid,intensity FROM emotion WHERE source='guild' AND night=?", (h.night,)))
    assert strengths[101] == 2 and strengths[102] == strengths[103] == 3
    run(mood.stage_mood(h.ctx(night="2026-10-30")))
    assert h.store.conn.execute("SELECT value FROM guild_mood WHERE night='2026-10-30'").fetchone()[0] == -0.25
    run(mood.stage_mood(h.ctx(night="2026-11-02")))
    assert h.store.conn.execute("SELECT value FROM guild_mood WHERE night='2026-11-02'").fetchone()[0] == pytest.approx(-0.25 * 0.5 ** 3)
    assert ctx.reasoner.llm.calls == []


@pytest.mark.parametrize("openness,rate", [(6, 0.2), (3, 0.1)])
def test_contagion_uses_incoming_affinity_and_one_simultaneous_snapshot(h, openness, rate):
    start = {101: (-0.8, -0.6, -0.5), 102: (0.8, 0.6, 0.5), 103: (0.6, 0.4, 0.3)}
    for guid, value in start.items():
        pad(h, guid, value)
    h.rel(102, 101, affinity=50)
    h.rel(103, 101, affinity=50)
    h.rel(101, 102, affinity=50)  # 102 must read 101's original decayed point
    h.rel(101, 104, affinity=50)  # outgoing toward 104 must not make 104 influence 101
    trait(h, 101, "openness", openness)
    trait(h, 102, "openness", 6)
    ctx = h.ctx()
    base = mood.baseline(ctx.persona(101))
    before = {g: tuple(b + (x - b) * 2 ** -0.5 for x, b in zip(value, base)) for g, value in start.items()}
    expected = tuple(x + rate * ((y + z) / 2 - x) for x, y, z in zip(before[101], before[102], before[103]))
    expected102 = tuple(x + 0.2 * (y - x) for x, y in zip(before[102], before[101]))
    run(mood.stage_mood(ctx))
    assert point(h, 101) == pytest.approx(expected)
    assert point(h, 102) == pytest.approx(expected102)
    assert point(h, 103) == pytest.approx(before[103])
    assert h.store.get_state(101).mood == pytest.approx(expected[0])
    assert h.store.get_state(101).mood_baseline == base[0]


def test_away_members_included_state_fields_preserved_and_one_write_each(h, monkeypatch):
    h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (105,'rest',?,?)", (h.T - 10, h.T + 86400))
    h.store.put_state(replace(h.store.get_state(101), goals=("Finish dailies",), life_thread="A busy week"))
    writes, put = [], h.store.put_state

    def counted(row):
        writes.append(row.guid)
        return put(row)

    monkeypatch.setattr(h.store, "put_state", counted)
    ctx = h.ctx()
    run(mood.stage_mood(ctx))
    run(mood.stage_mood(ctx))
    assert writes == [101, 102, 103, 104, 105]
    assert h.store.get_state(101).goals == ("Finish dailies",)
    assert h.store.get_state(101).life_thread == "A busy week"
    assert ctx.reasoner.llm.calls == [] and h.rows(0) == h.rows(h.human) == []
    assert h.store.conn.execute("SELECT 1 FROM pad_mood WHERE guid IN (0,1)").fetchone() is None


def test_strongest_per_target_newest_tie_and_seven_night_expiry(h):
    feeling(h, kind="gratitude", target=102, intensity=2, why="Helped me.")
    feeling(h, kind="anger", target=103, intensity=3, why="Old reason.")
    feeling(h, kind="anger", target=103, intensity=3, why="Newest reason.", ts=h.T + 1)
    feeling(h, kind="joy", target=103, intensity=2, why="Weaker feeling.", ts=h.T + 2)
    feeling(h, target=104, intensity=1)
    run(mood.stage_mood(h.ctx()))
    rows = {m.key: m for m in memories(h, 101)}
    assert set(rows) == {"emotion:102", "emotion:103"}
    assert rows["emotion:103"].text == "Still angry at Coros. Newest reason."
    assert rows["emotion:103"].importance == 4 and rows["emotion:103"].valence == -0.51
    run(mood.stage_mood(h.ctx(night="2026-11-04")))  # six nights: retained
    assert h.store.keyed_memory(101, "emotion:103").status == "active"
    run(mood.stage_mood(h.ctx(night="2026-11-05")))
    assert h.store.keyed_memory(101, "emotion:103").status == "archived"


def test_renewed_emotion_expiry_uses_last_renewal_not_original_creation(h):
    feeling(h)
    run(mood.stage_mood(h.ctx()))
    original = h.store.keyed_memory(101, "emotion:103")
    feeling(h, kind="admiration", intensity=2, why="Much better.", night="2026-11-03")
    run(mood.stage_mood(h.ctx(night="2026-11-03")))
    updated = h.store.keyed_memory(101, "emotion:103")
    assert updated.id == original.id and updated.t_created == original.t_created
    assert updated.text == "Impressed by Coros. Much better."
    run(mood.stage_mood(h.ctx(night="2026-11-06")))
    assert h.store.keyed_memory(101, "emotion:103").status == "active"


def test_private_sources_and_empty_why_keep_their_scopes(h):
    whispered = h.say(h.T, 1, "Private news.", channel="whisper", to=101)
    source = h.mem(101, "A private conversation.", scope="private", scope_with=1,
                   evidence=(f"chat:{whispered}",))
    feeling(h, refs=[f"memory:{source}"])
    public = h.say(h.T + 1, 104, "A good pull.")
    feeling(h, owner=104, kind="admiration", target=105, refs=[f"chat:{public}"])
    feeling(h, owner=105, kind="happy_for", target=1, intensity=2, why="", refs=[f"chat:{public}"])
    run(mood.stage_mood(h.ctx()))
    private = h.store.keyed_memory(101, "emotion:103")
    assert (private.scope, private.scope_with) == ("private", 1)
    assert h.store.keyed_memory(104, "emotion:105").scope == "open"
    assert h.store.keyed_memory(105, "emotion:1").text == "Happy for Testy."
    assert knowledge.scope_leaks(h.store) == []


def test_legacy_open_wrapper_is_promoted_using_complete_private_ancestry(h):
    whispered = h.say(h.T, 1, "Private news.", channel="whisper", to=101)
    ident = h.mem(101, "Legacy open copy.", evidence=(f"chat:{whispered}",))
    for _ in range(6):
        ident = h.mem(101, "Another legacy wrapper.", evidence=(f"memory:{ident}",))
    feeling(h, refs=[f"memory:{ident}"])
    run(mood.stage_mood(h.ctx()))
    made = h.store.keyed_memory(101, "emotion:103")
    assert (made.scope, made.scope_with) == ("private", 1)
    assert made.id not in knowledge.scope_leaks(h.store)


def test_confession_root_and_keyed_rewrite_never_weaken_secrecy(h):
    h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate) VALUES (?,'live',36,0,0)",
                         (h.clock.now(),))
    enc = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,private) "
        "VALUES (?,0,101,102,'camp','done',1)", (h.night,)).lastrowid
    feeling(h, target=102, refs=[f"encounter:{enc}"])
    run(mood.stage_mood(h.ctx()))
    first = h.store.keyed_memory(101, "emotion:102")
    assert (first.scope, first.scope_with) == ("secret", 102)
    feeling(h, target=102, kind="joy", night="2026-10-30")
    run(mood.stage_mood(h.ctx(night="2026-10-30")))
    second = h.store.keyed_memory(101, "emotion:102")
    assert second.id == first.id and (second.scope, second.scope_with) == ("secret", 102)


@pytest.mark.parametrize("bad", ["missing", "cycle", "wrong_partner"])
def test_incomplete_and_unrepresentable_private_evidence_writes_no_copy(h, bad):
    first = h.mem(101, "A private detail.", scope="private", scope_with=1, evidence=())
    second = h.mem(101, "Another detail.", scope="private", scope_with=1, evidence=(f"memory:{first}",))
    if bad == "missing":
        h.store.conn.execute("DELETE FROM memory WHERE id=?", (first,))
    elif bad == "cycle":
        h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?", (json.dumps([f"memory:{second}"]), first))
    else:
        h.store.conn.execute("UPDATE memory SET scope_with=102 WHERE id=?", (first,))
    feeling(h, refs=[f"memory:{second}"])
    ctx = h.ctx()
    run(mood.stage_mood(ctx))
    assert memories(h, 101) == [] and ctx.done("mood", "all")


def test_every_occ_type_has_a_line_unknown_types_log_once(h, caplog):
    assert set(mood.OCC_PAD) == set(mood.EMOTION_LINES) == set(OCC)
    pairs = [(g, t) for g in (101, 102, 103, 104, 105) for t in (1, 101, 102, 103, 104, 105) if g != t]
    expected = []
    for kind, (guid, target) in zip(OCC, pairs):
        feeling(h, owner=guid, kind=kind, target=target, intensity=2, why="")
        expected.append((guid, target, kind))
    feeling(h, owner=105, kind="awe", target=104)
    feeling(h, owner=105, kind="surprise", target=103)
    ctx = h.ctx()
    run(mood.stage_mood(ctx))
    run(mood.stage_mood(ctx))
    assert sum(len(memories(h, guid)) for guid in (101, 102, 103, 104, 105)) == 14
    for guid, target, kind in expected:
        assert h.store.keyed_memory(guid, f"emotion:{target}").text == mood.EMOTION_LINES[kind].format(target=ctx.name(target))
    assert sum("emotion_type_unknown" in r.message for r in caplog.records) == 1
    assert ctx.done("mood", "all")


def test_why_is_cleaned_redacted_and_bounded(h):
    feeling(h, why="|cffffffffQuillon|r helped " + "a lot " * 60)
    run(mood.stage_mood(h.ctx()))
    text = h.store.keyed_memory(101, "emotion:103").text
    assert "Quillon" not in text and "Testy helped" in text and len(text) <= 200
    # Existing raw emotion is an injected legacy fixture; only the new memory is screened.
    assert not any(table == "memory" for table, _ in no_real_name(h.store))


def test_all_unit_rolls_back_guild_emotions_pad_state_memory_and_checkpoint(h, monkeypatch):
    h.boss(h.T, 631, 12, "killed", (101, 102))
    feeling(h)
    before = h.count("emotion"), h.count("persona_state")
    ctx = h.ctx()
    put = ctx.writer.put

    def fail(row, **kwargs):
        put(row, **kwargs)
        raise RuntimeError("synthetic emotional memory failure")

    monkeypatch.setattr(ctx.writer, "put", fail)
    run(mood.stage_mood(ctx))
    assert not ctx.done("mood", "all") and ctx.failures["mood"] == 1
    assert h.count("guild_mood") == h.count("pad_mood") == 0
    assert (h.count("emotion"), h.count("persona_state")) == before
    assert memories(h, 101) == [] and h.store.get_meta("nightly:mood-night:101") is None


def test_pad_decay_uses_calendar_nights_across_dst(h, monkeypatch):
    tz = ZoneInfo("America/New_York")
    previous, current = night_bounds("2026-10-31", tz)[0], night_bounds("2026-11-01", tz)[0]
    assert current - previous == 25 * 3600
    h.clock.set(current)
    monkeypatch.setattr(h.clock, "local", lambda: datetime.fromtimestamp(h.clock.now(), tz))
    pad(h, 101, (0.8, 0.6, 0.4), updated=previous)
    ctx = h.ctx(night="2026-11-01", window=night_bounds("2026-11-01", tz))
    base = mood.baseline(ctx.persona(101))
    expected = tuple(b + (x - b) * 2 ** -0.5 for x, b in zip((0.8, 0.6, 0.4), base))
    run(mood.stage_mood(ctx))
    assert point(h, 101) == pytest.approx(expected)


def test_delayed_runs_decay_by_logical_nights_not_same_completion_timestamp(h):
    h.clock.set(h.T + 7 * 86400)
    pad(h, 101, (0.8, 0.6, 0.4), updated=h.clock.now())
    first = h.ctx(night="2026-10-30")
    base = mood.baseline(first.persona(101))
    run(mood.stage_mood(first))
    run(mood.stage_mood(h.ctx(night="2026-10-31")))
    expected = tuple(b + (x - b) * 0.5 for x, b in zip((0.8, 0.6, 0.4), base))
    assert point(h, 101) == pytest.approx(expected)


def test_older_night_cannot_read_or_overwrite_a_later_mood_snapshot(h):
    run(mood.stage_mood(h.ctx(night="2026-11-02")))
    before = {guid: point(h, guid) for guid in (101, 102, 103, 104, 105)}
    feeling(h, kind="anger", night="2026-11-01")
    ctx = h.ctx(night="2026-11-01")
    run(mood.stage_mood(ctx))
    assert not ctx.done("mood", "all") and ctx.failures["mood"] == 1
    assert h.store.conn.execute("SELECT 1 FROM guild_mood WHERE night='2026-11-01'").fetchone() is None
    assert {guid: point(h, guid) for guid in before} == before
    assert h.store.get_meta("nightly:mood-night:101") == "2026-11-02"
    assert memories(h, 101) == []


def test_cross_owner_evidence_cannot_make_unshareable_memory_shareable(h):
    source = h.mem(102, "A confidence with no permissible audience.", scope="private",
                   scope_with=0, evidence=())
    feeling(h, refs=[f"memory:{source}"])
    assert mood.evidence_scope(h.store, 101, [f"memory:{source}"]) == ("private", 0)
    ctx = h.ctx()
    run(mood.stage_mood(ctx))
    assert ctx.done("mood", "all") and memories(h, 101) == []


@pytest.mark.parametrize("dropped", [None, "policy"])
def test_unpaired_or_dropped_mind_output_is_not_emotional_evidence(h, dropped):
    line = h.say(h.T, 102, "A synthetic public remark.", origin="mind")
    if dropped is None:
        h.store.conn.execute("DELETE FROM utterance WHERE guid=102")
    else:
        h.store.conn.execute("UPDATE utterance SET dropped=? WHERE guid=102", (dropped,))
    feeling(h, refs=[f"chat:{line}"])
    ctx = h.ctx()
    run(mood.stage_mood(ctx))
    assert ctx.done("mood", "all") and memories(h, 101) == []


def test_owner_zero_and_human_are_excluded_even_from_an_invalid_member_list(h, monkeypatch):
    ctx = h.ctx()
    members = ctx.members
    monkeypatch.setattr(ctx, "members", lambda include_away=False: [0, h.human, *members(include_away)])
    run(mood.stage_mood(ctx))
    assert ctx.done("mood", "all")
    assert h.store.conn.execute("SELECT 1 FROM pad_mood WHERE guid IN (0,1)").fetchone() is None
    assert h.rows(0) == h.rows(h.human) == []
