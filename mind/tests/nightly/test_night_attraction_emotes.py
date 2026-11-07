"""The night's attraction pass counts the human's text emotes at a woman with an attitude toward
him that she answered live (talk/emotes.py, owner 2026-11-07): each romantic one is a `flirt`
act, with its gain, caps and taste, at most two a night, and each rude one a hurt. Every name
here is invented."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from leeroy_mind.nightly.attraction import (EMOTE_FLIRTS_PER_NIGHT, HALF_LIFE_DAYS, HURT_FACTOR,
                                           decay, stage_attraction)
from leeroy_mind.personas.io import load_persona
from leeroy_mind.talk.emote_names import answered_key

run = asyncio.run
MIND = Path(__file__).resolve().parents[2]


def acts(h, guid):
    return h.store.conn.execute(
        "SELECT act, gain, mult, ref, ts, live FROM attraction_act WHERE guid=? ORDER BY id",
        (guid,)).fetchall()


def answered(h, t, emote, guid, *, by=1):
    """His emote at `guid`, stored, with the receipt the live side writes once her emote back
    went out (talk/emotes.py)."""
    event = h.emote(t, by, emote, guid)
    h.store.set_meta(answered_key(event), str(guid))
    return event


def test_his_kiss_at_her_is_a_flirt(h):
    h.woman(102, "drawn")
    h.rel(102, 1, attraction=40)
    h.seen(102)
    h.session(h.T - 60, h.T + 3600)
    event = answered(h, h.T + 60, "kiss", 102)
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
    for i, emote in enumerate(("love", "hug")):
        answered(h, h.T + 60 * (i + 1), emote, 103)
    ctx = h.ctx()
    run(stage_attraction(ctx))
    assert [(a["act"], a["gain"], a["mult"]) for a in acts(h, 103)] == [("flirt", 40.0, 2.0)] * 2
    assert h.store.get_relationship(103, 1).attraction == 100  # capped
    run(stage_attraction(ctx))  # the night once
    assert len(acts(h, 103)) == 2


def test_at_most_two_emote_flirts_a_night(h):
    """Fix round 1 ruling: his romantic emotes at one woman count twice a night at most."""
    assert EMOTE_FLIRTS_PER_NIGHT == 2
    h.woman(102, "fond")
    h.rel(102, 1, attraction=20)
    h.woman(104, "fond")
    h.rel(104, 1, attraction=20)
    h.seen(102, 104)
    h.session(h.T - 60, h.T + 3600)
    kisses = [answered(h, h.T + 60 * i, "kiss", 102) for i in range(1, 5)]
    answered(h, h.T + 400, "hug", 104)  # another woman's own count
    run(stage_attraction(h.ctx()))
    assert [a["ref"] for a in acts(h, 102)] == [f"event:{e}" for e in kisses[:2]]
    assert h.store.get_relationship(102, 1).attraction == 60
    assert len(acts(h, 104)) == 1
    h.clock.set(h.T + 86400)  # the next night counts two again
    more = [answered(h, h.T + 86400 + 60 * i, "kiss", 102) for i in range(1, 4)]
    run(stage_attraction(h.ctx(night="2026-10-30")))
    assert [a["ref"] for a in acts(h, 102)][2:] == [f"event:{e}" for e in more[:2]]


def test_an_emote_she_never_answered_counts_nothing(h):
    """Fix round 1 ruling: only what the live side answered (no receipt: her cooldown, a fight,
    AFK), and a receipt for someone else is not hers."""
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=45)
    h.woman(102, "lust")
    h.rel(102, 1, attraction=70)
    h.seen(101, 102)
    h.session(h.T - 60, h.T + 3600)
    h.emote(h.T + 10, 1, "kiss", 101)  # no receipt
    h.emote(h.T + 20, 1, "slap", 102)  # no receipt
    other = h.emote(h.T + 30, 1, "kiss", 101)
    h.store.set_meta(answered_key(other), "104")
    run(stage_attraction(h.ctx()))
    assert h.count("attraction_act") == 0
    assert h.store.get_relationship(101, 1).attraction == 45
    assert h.store.get_relationship(102, 1).attraction == 70


def test_his_slap_at_her_hurts(h):
    h.woman(101, "lust")
    h.rel(101, 1, attraction=70)
    h.seen(101)
    h.session(h.T - 60, h.T + 3600)
    event = answered(h, h.T + 120, "slap", 101)
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
    answered(h, h.T + 10, "kiss", 102)
    answered(h, h.T + 20, "slap", 103)
    answered(h, h.T + 30, "kiss", 101, by=104)  # another member's
    h.emote(h.T + 40, 1, "kiss", 0)  # at nobody
    answered(h, h.T + 50, "wave", 101)  # not romantic, not rude
    run(stage_attraction(h.ctx()))
    assert h.count("attraction_act") == 0
    assert h.store.get_relationship(101, 1).attraction == 45


def test_an_emote_outside_the_window_waits_for_its_own_night(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=45)
    h.seen(101)
    ctx = h.ctx()
    answered(h, ctx.window[1] + 60, "kiss", 101)
    run(stage_attraction(ctx))
    assert h.count("attraction_act") == 0


def test_the_night_reads_the_emote_names_without_the_talk_engine(tmp_path):
    """Fix round 1: the sets live in a leaf module, so the night imports no bridge or talk
    pipeline for them."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import leeroy_mind.nightly.attraction\n"
        "assert 'leeroy_mind.talk.emote_names' in sys.modules\n"
        "for name in ('leeroy_mind.talk.emotes', 'leeroy_mind.talk.pipeline',"
        " 'leeroy_mind.bridge'):\n"
        "    assert name not in sys.modules, name\n", encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    subprocess.run([sys.executable, "-I", str(probe), str(MIND)], cwd=tmp_path,
                   env={**os.environ, "LEEROY_MIND_DATA": str(data_dir)}, timeout=60,
                   check=True, capture_output=True, text=True)
