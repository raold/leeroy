"""The night's attraction pass counts the human's text emotes at a woman with an attitude toward
him (talk/emotes.py, owner 2026-11-07): each romantic one is a `flirt` act, with its gain, caps
and taste, and each rude one a hurt. Every name here is invented."""
import asyncio

import pytest

from leeroy_mind.nightly.attraction import HALF_LIFE_DAYS, HURT_FACTOR, decay, stage_attraction
from leeroy_mind.personas.io import load_persona

run = asyncio.run


def acts(h, guid):
    return h.store.conn.execute(
        "SELECT act, gain, mult, ref, ts, live FROM attraction_act WHERE guid=? ORDER BY id",
        (guid,)).fetchall()


def test_his_kiss_at_her_is_a_flirt(h):
    h.woman(102, "drawn")
    h.rel(102, 1, attraction=40)
    h.seen(102)
    h.session(h.T - 60, h.T + 3600)
    event = h.emote(h.T + 60, 1, "kiss", 102)
    run(stage_attraction(h.ctx()))
    (act,) = acts(h, 102)
    assert (act["act"], act["gain"], act["mult"], act["ref"], act["live"]) == (
        "flirt", 20.0, 1.0, f"event:{event}", 0)
    assert act["ts"] == h.T + 60
    assert h.store.get_relationship(102, 1).attraction == 60
    assert load_persona(h.store, 102).romance.stance == "lust"  # the existing band move
    row = h.store.conn.execute("SELECT gained FROM attraction_log WHERE guid=102").fetchone()
    assert row["gained"] == 20


def test_one_act_per_emote_with_her_taste_and_the_cap(h):
    h.woman(103, "lust", drawn_to=("flirting and teasing back",))
    h.rel(103, 1, attraction=70)
    h.seen(103)
    h.session(h.T - 60, h.T + 3600)
    for i, emote in enumerate(("love", "hug", "wink")):
        h.emote(h.T + 60 * (i + 1), 1, emote, 103)
    ctx = h.ctx()
    run(stage_attraction(ctx))
    assert [(a["act"], a["gain"], a["mult"]) for a in acts(h, 103)] == [("flirt", 40.0, 2.0)] * 3
    assert h.store.get_relationship(103, 1).attraction == 100  # capped
    run(stage_attraction(ctx))  # the night once
    assert len(acts(h, 103)) == 3


def test_his_slap_at_her_hurts(h):
    h.woman(101, "lust")
    h.rel(101, 1, attraction=70)
    h.seen(101)
    h.session(h.T - 60, h.T + 3600)
    event = h.emote(h.T + 120, 1, "slap", 101)
    run(stage_attraction(h.ctx()))
    (act,) = acts(h, 101)
    assert (act["act"], act["gain"], act["ref"]) == ("hurt", 0, f"event:{event}")
    score = decay(70, 1, HALF_LIFE_DAYS / HURT_FACTOR)
    assert h.store.get_relationship(101, 1).attraction == pytest.approx(score)
    memory, = [m for m in h.rows(101) if m.key == "hurt:2026-10-29"]
    assert memory.text == "Testy was harsh with me tonight."
    log = h.store.conn.execute("SELECT hurt FROM attraction_log WHERE guid=101").fetchone()
    assert log["hurt"] == 1


def test_only_his_emotes_at_a_woman_with_an_attitude_count(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=45)
    h.persona(102, stance="lust", player_gender="male")  # men stay friends
    h.woman(103, "none")
    h.seen(101)
    h.session(h.T - 60, h.T + 3600)
    h.emote(h.T + 10, 1, "kiss", 102)
    h.emote(h.T + 20, 1, "slap", 103)
    h.emote(h.T + 30, 104, "kiss", 101)  # another member's
    h.emote(h.T + 40, 1, "kiss", 0)  # at nobody
    h.emote(h.T + 50, 1, "wave", 101)  # not romantic, not rude
    run(stage_attraction(h.ctx()))
    assert h.count("attraction_act") == 0
    assert h.store.get_relationship(101, 1).attraction == 45


def test_an_emote_outside_the_window_waits_for_its_own_night(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=45)
    h.seen(101)
    ctx = h.ctx()
    h.emote(ctx.window[1] + 60, 1, "kiss", 101)
    run(stage_attraction(ctx))
    assert h.count("attraction_act") == 0
