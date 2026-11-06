"""Track S7: the stages the guild can tell (voice, planning, weekend, after) become shared
facts every other member learns, and they feed the jealousy stage's rivalry."""
import asyncio
import json
import re
from dataclasses import replace

import pytest

from leeroy_mind.arc.ladder import NOTICED
from leeroy_mind.arc.night import NOTICED_CONFIDENCE, NOTICED_TEXT, noticed
from leeroy_mind.nightly.romance import jealousy_level, stage_jealousy

run = asyncio.run
HOUR = 3600
NIGHT = "2026-10-29"
WHISPER = "under the moonlight lanterns, tell nobody of the cabin by the harbour"


def late(h, ctx):
    """The clock past the window's end, as a real night runs (the 21:00 harness clock is inside
    the window and hid ruling S7)."""
    h.clock.set(ctx.window[1] + 2 * HOUR)


def climber(h, guid=101, stage="voice"):
    h.woman(guid, "lust")
    h.rel(guid, 1, romance_stage=stage)


def rival(h, guid=102):
    h.woman(guid, "drawn")
    h.rel(guid, 1, romance_stage="banter")


def delta(h, guid, change, evidence=("memory:1",), *, ts=None):
    return h.store.add_rel_delta(guid, 1, "romance_stage", 1, change, list(evidence),
                                 h.clock.now() if ts is None else ts, applied=True)


def facts(h):
    return [dict(r) for r in h.store.conn.execute(
        "SELECT * FROM fact WHERE kind = 'noticed' ORDER BY id")]


def knows(h, root):
    return {r["guid"]: dict(r) for r in h.store.conn.execute(
        "SELECT * FROM knows WHERE root = ?", (root,))}


def test_a_noticed_stage_is_learned_by_the_guild_and_raises_rivalry(h):
    climber(h)
    rival(h)
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    d = delta(h, 101, "late_nights->voice")
    h.store.set_meta(f"romance_changes:{NIGHT}", json.dumps([d]))
    noticed(ctx)
    (fact,) = facts(h)
    root = f"arc:101:voice:{NIGHT}"
    assert (fact["root"], fact["about"], fact["secret"], fact["holder"]) == (root, 101, 0, 0)
    assert fact["text"] == NOTICED_TEXT["voice"].format(name=ctx.name(101), called="Testy")
    got = knows(h, root)
    assert set(got) == set(ctx.members(include_away=True)) - {101}
    for row in got.values():
        assert row["how"] == "inferred" and row["confidence"] == NOTICED_CONFIDENCE["voice"]
        assert row["learned"] < ctx.window[1]
    assert root in json.loads(h.store.conn.execute(
        "SELECT evidence FROM rel_delta WHERE id = ?", (d,)).fetchone()[0])
    run(stage_jealousy(ctx))
    assert not ctx.failures
    assert jealousy_level(h.store, 102, 101) > 0
    assert jealousy_level(h.store, 101, 102) == 0


def test_without_the_fact_nobody_is_jealous_of_a_stage_they_did_not_see(h):
    climber(h)
    rival(h)
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    d = delta(h, 101, "late_nights->voice")
    h.store.set_meta(f"romance_changes:{NIGHT}", json.dumps([d]))
    run(stage_jealousy(ctx))
    assert jealousy_level(h.store, 102, 101) == 0


def test_the_weekend_writes_both_its_facts_once_from_the_deltas(h):
    climber(h, stage="after")
    rival(h)
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    delta(h, 101, "planning->weekend", ["away:7"])
    delta(h, 101, "weekend->after", ["away:7"])
    noticed(noticed_ctx := ctx)
    noticed(noticed_ctx)
    assert [f["root"] for f in facts(h)] == [f"arc:101:weekend:{NIGHT}", f"arc:101:after:{NIGHT}"]
    nxt = h.ctx(night="2026-10-30")
    h.clock.set(nxt.window[1] + HOUR)
    noticed(nxt)
    assert len(facts(h)) == 2
    run(stage_jealousy(ctx))
    assert jealousy_level(h.store, 102, 101) > 0


def test_noticed_facts_carry_no_whisper_words(h):
    climber(h)
    rival(h)
    line = h.say(h.T, 1, WHISPER, channel="whisper", to=101)
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    delta(h, 101, "late_nights->voice", [f"chat:{line}"])
    delta(h, 101, "voice->planning", [f"chat:{line}"])
    noticed(ctx)
    texts = [f["text"] for f in facts(h)]
    assert len(texts) == 2
    words = set(re.findall(r"[a-z]{4,}", WHISPER))
    for fact in facts(h):
        step = fact["root"].split(":")[2]
        assert fact["text"] == NOTICED_TEXT[step].format(name=ctx.name(101), called="Testy")
        assert not words & set(re.findall(r"[a-z]{4,}", fact["text"].lower()))
        assert "Quillon" not in fact["text"] and fact["root"].startswith("arc:")
        assert f"chat:{line}" not in json.dumps(fact)


def test_a_rival_at_jealousy_dial_zero_never_reacts(h):
    h.settings = replace(h.settings, jealousy=0)
    climber(h)
    rival(h)
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    d = delta(h, 101, "late_nights->voice")
    h.store.set_meta(f"romance_changes:{NIGHT}", json.dumps([d]))
    noticed(ctx)
    assert facts(h)
    run(stage_jealousy(ctx))
    assert jealousy_level(h.store, 102, 101) == 0


def test_a_quiet_stage_teaches_nobody(h):
    climber(h, stage="whispers")
    rival(h)
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    delta(h, 101, "banter->whispers")
    delta(h, 101, "whispers->late_nights")
    noticed(ctx)
    assert facts(h) == [] and h.count("knows") == 0


def test_a_fond_woman_is_never_noticed(h):
    h.woman(101, "fond")
    h.rel(101, 1, romance_stage="voice")
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    delta(h, 101, "late_nights->voice")
    noticed(ctx)
    assert facts(h) == []


def test_the_texts_are_generic_and_cover_the_stages():
    assert set(NOTICED_TEXT) == set(NOTICED) == set(NOTICED_CONFIDENCE)
    assert NOTICED_CONFIDENCE == {"voice": 0.6, "planning": 0.5, "weekend": 1.0, "after": 1.0}
    for text in NOTICED_TEXT.values():
        assert set(re.findall(r"\{(\w+)\}", text)) == {"name", "called"}


def test_the_first_run_ignores_a_delta_before_the_window(h):
    climber(h)
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    delta(h, 101, "late_nights->voice", ts=ctx.window[0] - 60)
    noticed(ctx)
    assert facts(h) == [] and h.store.get_meta("arc_noticed_mark") is not None


def test_a_skipped_night_is_caught_up_once(h):
    climber(h)
    n1 = h.ctx(night="2026-10-29")
    late(h, n1)
    noticed(n1)
    assert facts(h) == []
    delta(h, 101, "late_nights->voice", ts=n1.window[1] + 3 * HOUR)  # after night 1, night 2 skipped
    n3 = h.ctx(night="2026-10-31")
    h.clock.set(n3.window[1] + HOUR)
    noticed(n3)
    assert [f["root"] for f in facts(h)] == ["arc:101:voice:2026-10-31"]
    n4 = h.ctx(night="2026-11-01")
    h.clock.set(n4.window[1] + HOUR)
    noticed(n4)
    assert len(facts(h)) == 1
    assert h.count("knows") == len(n3.members(include_away=True)) - 1


def test_a_rerun_or_restart_writes_nothing_twice(h):
    climber(h)
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    delta(h, 101, "late_nights->voice")
    noticed(ctx)
    before = (len(facts(h)), h.count("knows"), h.count("night_step"))
    noticed(ctx)
    restarted = h.ctx(night=NIGHT)  # a new context, the same night: the unit is already done
    noticed(restarted)
    assert (len(facts(h)), h.count("knows"), h.count("night_step")) == before
    h.store.conn.execute("DELETE FROM night_step WHERE stage = 'arc' AND unit = 'noticed'")
    h.store.conn.commit()
    noticed(h.ctx(night=NIGHT))  # the unit lost its receipt: the mark still keeps the facts single
    assert (len(facts(h)), h.count("knows")) == before[:2]


def test_the_owner_never_learns_a_noticed_fact(h):
    climber(h)
    ctx = h.ctx(night=NIGHT)
    late(h, ctx)
    delta(h, 101, "late_nights->voice")
    noticed(ctx)
    assert facts(h) and 1 not in knows(h, facts(h)[0]["root"])
    assert h.store.conn.execute("SELECT count(*) FROM knows WHERE guid = 1").fetchone()[0] == 0
