"""The night's scenes stage (P4 Task 7): situational photos planned from a member's day and the
guild photo planned from the raid night. Fakes only; every person and scene here is invented."""
import logging
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.nightly import budget
from leeroy_mind.selfies import files
from leeroy_mind.selfies.config import SelfieCfg
from leeroy_mind.selfies.scenes import (GUILD_GAP_S, GUILD_MAX, GUILD_MIN, STAGE, candidates,
                                        make_scenes_stage)
from leeroy_mind.selfies.snaps import last_sent, mark_sent, planned, plan_snap
from leeroy_mind.selfies.store import pick as pick_photo
from leeroy_mind.selfies.scene import PHASES
from nightly.test_night_enrich import APPEARANCE
from selfies.fakes import JPEG, ready, ready_snap, renderable_cast, planned_snap, sent_snap

SNAPS_CFG = SelfieCfg(enabled=True, snaps=True)
SCENE_REPLY = {"photo": True, "kind": "snapshot", "framing": "upper_body",
               "setting": "on a wooden pier by the harbour",
               "activity": "throwing a ball for a scruffy terrier",
               "outfit": "a yellow raincoat over a grey jumper", "expression": "laughing",
               "when": "day", "audience": "anyone",
               "shows": "Them on a wooden pier throwing a ball for their dog."}
DAY = 86400.0


def chosen_photo(h, guid):
    ready(h.store, h.settings.data_dir, guid, 1, now=1.0)
    pick_photo(h.store, guid, 1, 0, source="console", now=2.0)


def life_beat(h, guid, text="Her sister is visiting this weekend.", night=None):
    with h.store.transaction():
        tid = h.store.conn.execute(
            "INSERT INTO life_thread(guid, title, status, started) VALUES (?, 'a visit', "
            "'active', 1.0)", (guid,)).lastrowid
        return h.store.conn.execute(
            "INSERT INTO life_beat(thread_id, night, text, ts) VALUES (?, ?, ?, 1.0)",
            (tid, night or h.night, text)).lastrowid


def stocked(h, guid, **over):
    """A planned snap that the sweep keeps: the member's own pick and looks."""
    from leeroy_mind.selfies.snaps import looks_hash
    from leeroy_mind.selfies.store import current
    return planned_snap(h.store, guid, pick=current(h.store, guid).pick,
                        looks=looks_hash(APPEARANCE), **over)


def stage(dial=3, cfg=SNAPS_CFG):
    return make_scenes_stage(cfg, dial=lambda: dial)


def cast(h, *guids):
    renderable_cast(h.store, *guids)
    for g in guids:
        chosen_photo(h, g)


def raid(h, *, ended, members, kills=1, offline=0, lead=101, tank=102, loot=103, status="raided"):
    with h.store.transaction():
        rid = h.store.conn.execute(
            "INSERT INTO guild_raid(started, ended, raid_key, size, heroic, offline, lead_guid, "
            "tank_guid, loot_guid, kills) VALUES (?, ?, 'naxx', 10, 0, ?, ?, ?, ?, ?)",
            (ended - 3600, ended, offline, lead, tank, loot, kills)).lastrowid
        for g in members:
            h.store.conn.execute("INSERT INTO guild_attendance(raid_id, guid, status) "
                                 "VALUES (?, ?, ?)", (rid, g, status))
    return rid


def test_a_member_with_a_photo_and_a_beat_gets_a_planned_snap(h):
    renderable_cast(h.store, 101); chosen_photo(h, 101); beat = life_beat(h, 101)
    ctx = h.ctx({"scene": [{**SCENE_REPLY, "when": "late"}]})
    run(stage(3)(ctx))
    (row,) = planned(h.store, 10)
    assert (row.guid, row.source, row.ref, row.phases) == (101, "life", f"beat:{beat}",
                                                          ("late", "dead"))
    assert (row.not_before, row.expires) == (ctx.window[1], ctx.window[1] + 3 * DAY)
    assert row.kind == "situation" and row.pick is not None and row.looks and row.audience == "anyone"


def test_the_stock_follows_the_dial_and_dial_zero_asks_nobody(h):
    renderable_cast(h.store, 101, 102, 103, 104)
    [chosen_photo(h, g) for g in (101, 102, 103, 104)]
    for g in (101, 102): stocked(h, g)
    ctx = h.ctx({"scene": [SCENE_REPLY] * 4})
    run(stage(1)(ctx))     # stock_target(1) == 2: full already
    assert ctx.reasoner.calls == 0, ctx.reasoner.calls
    run(stage(0)(h.ctx({}, night="2026-10-30")))
    assert len(planned(h.store, 10)) == 2


def test_members_without_a_chosen_photo_or_with_full_stock_or_a_recent_photo_are_not_asked(h):
    renderable_cast(h.store, 101, 102, 103, 104, 105)
    for g in (102, 103, 104, 105): chosen_photo(h, g)       # 101 has no chosen photo
    stocked(h, 102); stocked(h, 102, source="everyday", ref="e:1")  # full
    sent_snap(h.store, h.settings.data_dir, 103)             # sent just now
    with h.store.transaction():
        h.store.conn.execute("UPDATE selfie_snap SET sent=? WHERE status='sent'", (h.T - DAY,))
    ctx = h.ctx({"scene": [SCENE_REPLY]})
    assert sorted(g for g, _, _ in candidates(ctx, 10)) == [104, 105]
    with h.store.transaction():
        h.store.conn.execute("UPDATE selfie_snap SET sent=? WHERE status='sent'", (h.T - 3 * DAY,))
    assert sorted(g for g, _, _ in candidates(ctx, 10)) == [103, 104, 105]
    # an away member is left out
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid, reason, left_at, returns_at) "
                             "VALUES (104, 'trip', 1.0, 2.0)")
    assert sorted(g for g, _, _ in candidates(ctx, 10)) == [103, 105]


def test_a_birthday_comes_before_a_beat_and_a_beat_before_an_ordinary_day(h):
    h.persona(102, birthday="10-30")                         # the night is 2026-10-29
    h.persona(103, birthday="10-29")                         # today, not tomorrow
    cast(h, 101, 102, 103, 104)
    beat = life_beat(h, 104)
    ctx = h.ctx({})
    got = candidates(ctx, 10)
    assert got[0] == (102, "birthday", "birthday:2026-10-29")
    assert got[1] == (104, "life", f"beat:{beat}")
    assert {(g, s) for g, s, _ in got[2:]} == {(101, "everyday"), (103, "everyday")}
    assert all(r == "everyday:2026-10-29" for _, s, r in got if s == "everyday")
    assert candidates(ctx, 10) == got                        # the same on every run of the night
    assert len(candidates(h.ctx({}, night="2026-10-29"), 1)) == 2   # stock_target(1) == 2


def test_the_scene_cap_limits_the_night(h):
    cast(h, 101, 102, 103)
    ctx = h.ctx({})
    ctx.settings = replace(ctx.settings, scenes_per_night=2)
    assert len(candidates(ctx, 10)) == 2


def test_a_declined_or_refused_scene_is_stored_nowhere_and_not_asked_again_tonight(h, caplog):
    cast(h, 101, 102, 103)
    refused = {**SCENE_REPLY, "activity": "stripping off her clothes"}
    ctx = h.ctx({"scene": [{**SCENE_REPLY, "photo": False}, refused, SCENE_REPLY]})
    with caplog.at_level(logging.INFO, logger="leeroy_mind"):
        run(stage(3)(ctx))
    assert len(planned(h.store, 10)) == 1
    assert ctx.reasoner.calls == 3 and all(ctx.done(STAGE, str(g)) for g in (101, 102, 103))
    assert "selfies_scene_declined" in caplog.text and "selfies_scene_refused" in caplog.text
    calls = ctx.reasoner.calls
    run(stage(3)(ctx))
    assert ctx.reasoner.calls == calls


def test_a_raid_with_a_kill_plans_one_guild_photo_a_week_without_him(h):
    members = [1, 101, 102, 103, 104, 105]
    cast(h, 101, 102, 103, 104, 105)
    first = raid(h, ended=h.T - 3600, members=members)
    second = raid(h, ended=h.T - 3000, members=members)
    ctx = h.ctx({"scene": [SCENE_REPLY] * 5})
    run(stage(3)(ctx))
    (row,) = [r for r in planned(h.store, 20) if r.kind == "guild"]
    assert (row.kind, row.guid, row.source, row.ref, row.audience) == (
        "guild", 0, "raid", f"raid:{second}", "anyone")
    assert row.phases == PHASES["evening"] + PHASES["late"]
    assert row.scene == {"shows": "The raid on a video call, 5 of them"}
    assert [g for g, _ in row.members][:3] == [101, 102, 103] and 1 not in [g for g, _ in row.members]
    assert sorted(g for g, _ in row.members) == [101, 102, 103, 104, 105]
    third = raid(h, ended=h.T - 100, members=members)
    run(stage(3)(h.ctx({"scene": [SCENE_REPLY] * 5}, night="2026-10-30")))             # a week's gap
    assert len([r for r in planned(h.store, 20) if r.kind == "guild"]) == 1 and first != third


def test_a_raid_without_a_kill_offline_or_with_two_members_plans_nothing(h):
    cast(h, 101, 102, 103)
    raid(h, ended=h.T - 3600, members=[101, 102, 103], kills=0)
    raid(h, ended=h.T - 3500, members=[101, 102, 103], offline=1)
    raid(h, ended=h.T - 3400, members=[101, 102], kills=2)
    run(stage(3)(h.ctx({"scene": [{**SCENE_REPLY, "photo": False}] * 3})))
    assert planned(h.store, 10) == []
    assert GUILD_MIN == 3 and GUILD_MAX == 16 and GUILD_GAP_S == 604800.0


def test_the_sweep_expires_but_keeps_every_file_and_sent_photos(h):
    cast(h, 101, 102)
    data = h.settings.data_dir
    stale = ready_snap(h.store, data, 101, pick=999)         # the member's pick moved on
    kept = sent_snap(h.store, data, 102)
    ctx = h.ctx({})
    run(stage(0)(ctx))
    ended = dict(h.store.conn.execute("SELECT id, status FROM selfie_snap").fetchall())
    assert ended == {stale: "expired", kept: "sent"}
    assert files.snap(data, 101, stale).exists() and files.snap(data, 102, kept).exists()
    assert files.snap(data, 101, stale, thumb=True).exists()


def test_the_counter_counts_only_when_installed(h):
    cast(h, 101, 102)
    ctx = h.ctx({})
    make_scenes_stage(SNAPS_CFG, dial=lambda: 3)
    assert budget._counts(ctx, set(), frozenset())  .get(STAGE) is None
    assert budget._counts(ctx, set(), frozenset({STAGE}))[STAGE] == 2
    ctx.settings = replace(ctx.settings, scenes_per_night=1)
    assert budget._counts(ctx, set(), frozenset({STAGE}))[STAGE] == 1


def test_logs_carry_no_scene_text(h, caplog):
    cast(h, 101, 102)
    ctx = h.ctx({"scene": [SCENE_REPLY, {**SCENE_REPLY, "photo": False,
                                         "shows": "A hidden tale of the pier."}]})
    with caplog.at_level(logging.DEBUG):
        run(stage(3)(ctx))
    for text in (*(v for v in SCENE_REPLY.values() if isinstance(v, str)), "hidden tale"):
        assert text not in caplog.text
