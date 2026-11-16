"""He lives far away (2026-11-15): every life prompt says so, a beat that pairs one of his names
with contact off the game is refused before the judge, such a beat never primes the next one,
and a beat that names him or touches her secret is stored private to her and him. Invented
names only."""
import random

import pytest

from conftest import run
from nightly.conftest import IDENT, yes
from leeroy_mind.memory.recall import scope_ok
from leeroy_mind.nightly.life import (FAR_AWAY, ensure_thread, his_names, meets_him,
                                     private_beat, stage_life)

DAY = 86400
BEAT = {"beat": "My supervisor pushed the deadline.", "mood": 0.5,
        "resolved": False, "next_title": None}
NAMES = his_names(IDENT)


def due(h, **persona):
    h.persona(101, life_thread="thesis deadline", **persona)
    return ensure_thread(h.store, 101, "thesis deadline", h.T - 5 * DAY, random.Random(1))


def ctx_for(h, **reply):
    return h.ctx({"life_beat": [{**BEAT, **reply}], "judge": [yes]})


@pytest.mark.parametrize("text", [
    "Testy's text lit up my screen: still at work?",
    "Found a note from Testy on the kitchen table.",
    "Testmage called while I was on the bus.",
    "Testys shadow fell across the doorway.",  # a mangled name
    "Drove to the office park where Testy works.",
    "Testy sent flowers to the library.",
])
def test_contact_with_him_off_the_game_is_caught(text):
    assert meets_him(text, NAMES)


@pytest.mark.parametrize("text", [
    "Stayed up thinking about the raid Testy led.",
    "Laughed at Testy's joke in guild chat on the bus home.",
    "My sister texted me about the wedding.",  # contact, but not his
    "The register drawer jammed again.",
])
def test_other_beats_pass(text):
    assert not meets_him(text, NAMES)


def test_the_prompt_says_he_lives_far_away(h):
    due(h)
    ctx = ctx_for(h)
    run(stage_life(ctx))
    call = next(c for c in ctx.reasoner.llm.calls
                if (c.get("format") or {}).get("title") == "life_beat")
    system = call["messages"][0]["content"]
    assert system.rstrip().endswith(FAR_AWAY.format(human="Testy", name="Arkon", they="he",
                                                    them="him"))


def test_a_beat_meeting_him_is_refused_before_the_judge(h):
    tid = due(h)
    ctx = h.ctx({"life_beat": [{**BEAT, "beat": "Testy's text lit up my screen at work."}]})
    run(stage_life(ctx))
    assert h.count("life_beat") == 0 and h.rows(101) == []
    assert not any((c.get("format") or {}).get("title") != "life_beat"
                   for c in ctx.reasoner.llm.calls)  # no judge call
    row = h.store.conn.execute("SELECT verdict, reason FROM grounding_log").fetchone()
    assert tuple(row) == ("rejected", "meets him offline")
    assert h.store.conn.execute("SELECT beat_n FROM life_thread WHERE id=?",
                                (tid,)).fetchone()[0] == 0
    assert ctx.done("life", "101")


def test_an_earlier_beat_meeting_him_never_primes_the_next(h):
    tid = due(h)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO life_beat(thread_id,night,text,ts) VALUES (?,?,?,?)",
                             (tid, "2026-10-28", "A note from Testy was on my door.", h.T - 9))
        h.store.conn.execute("INSERT INTO life_beat(thread_id,night,text,ts) VALUES (?,?,?,?)",
                             (tid, "2026-10-28", "Printed chapter two.", h.T - 8))
    ctx = ctx_for(h)
    run(stage_life(ctx))
    call = next(c for c in ctx.reasoner.llm.calls
                if (c.get("format") or {}).get("title") == "life_beat")
    user = call["messages"][-1]["content"]
    assert "Printed chapter two." in user and "note from Testy" not in user


def test_a_beat_naming_him_is_private_to_the_two_of_them(h):
    due(h)
    run(stage_life(ctx_for(h, beat="Kept thinking about the raid Testy led last night.")))
    memory, = [m for m in h.rows(101) if m.source == "offscreen"]
    assert (memory.scope, memory.scope_with) == ("private", IDENT.guid)
    assert scope_ok(memory, turn_channel="whisper", to_guid=IDENT.guid)
    assert scope_ok(memory, turn_channel=f"dm:{memory.owner}", to_guid=IDENT.guid)
    assert not scope_ok(memory, turn_channel="guild", to_guid=0)
    assert not scope_ok(memory, turn_channel="discord:general", to_guid=0)


def test_a_beat_touching_her_secret_is_private(h):
    due(h, secret="works a second job at the casino on thursdays")
    run(stage_life(ctx_for(h, beat="Another thursday shift at the casino, feet aching.")))
    memory, = [m for m in h.rows(101) if m.source == "offscreen"]
    assert (memory.scope, memory.scope_with) == ("private", IDENT.guid)


def test_an_ordinary_beat_stays_open(h):
    due(h, secret="works a second job at the casino on thursdays")
    p = h.ctx().persona(101)
    assert not private_beat("My supervisor pushed the deadline.", "thesis deadline", p, NAMES)
    run(stage_life(ctx_for(h)))
    memory, = [m for m in h.rows(101) if m.source == "offscreen"]
    assert (memory.scope, memory.scope_with) == ("open", 0)
