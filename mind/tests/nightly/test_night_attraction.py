import asyncio
import json
from dataclasses import replace

import pytest

from conftest import make_persona
from leeroy_mind.nightly.attraction import (HALF_LIFE_DAYS, apply_act, band_for, decay, gain,
                                           night_half_life, reconcile, science, set_stance,
                                           stage_attraction)
from leeroy_mind.nightly.growth import undo
from leeroy_mind.nightly.reactions import laughs_at, reactions
from leeroy_mind.nightly.store import ledger_rows
from leeroy_mind.personas.io import PersonaCache, load_persona

run = asyncio.run


def neglect_night(h, t, guids=(103,)):
    h.session(t - 60, t + 3600)
    for guid in guids:
        for i in range(4):
            h.say(t + i * 600, guid, f"testy look at this {i}")
            h.say(t + i * 600 + 10, 104, "lol")
            h.say(t + i * 600 + 20, 1, "dunna ready?")


def test_reactions_classify_his_answers(h):
    h.woman(101, "drawn")
    h.session(h.T - 60, h.T + 7200)
    first = h.say(h.T, 101, "testy you coming to icc?")
    h.say(h.T + 20, 1, "haha yes")
    h.say(h.T + 600, 101, "you around?", channel="whisper", to=1)
    h.say(h.T + 700, 1, "yep", channel="whisper", to=101)
    h.say(h.T + 1200, 101, "testy did you see that pull")
    h.say(h.T + 1210, 102, "ready when you are")
    h.say(h.T + 1250, 1, "belra you ready?")
    h.say(h.T + 1800, 101, "testy wanna duel?")
    h.say(h.T + 1810, 1, "not now")
    h.say(h.T + 2400, 101, "testy look")
    h.emote(h.T + 2410, 1, "hug", 101)
    rs = reactions(h.store, h.ctx().window, 1, {1: "Testy", 101: "Arkon", 102: "Belra"})
    assert [(r.speaker, r.reaction) for r in rs] == [
        (101, "laugh"), (101, "whisper"), (101, "ignored"), (102, "reply"),
        (101, "rebuff"), (101, "emote")]
    assert rs[0].kind == "open"
    assert laughs_at(h.store, first) == {1}


def test_reactions_ignore_offline_and_unaddressed_silence(h):
    h.say(h.T, 101, "testy hello")
    assert reactions(h.store, h.ctx().window, 1, {1: "Testy", 101: "Arkon"}) == []
    h.session(h.T + 1, h.T + 3600)
    h.say(h.T + 60, 101, "nice weather")
    h.say(h.T + 120, 101, "testy hello")
    assert [r.reaction for r in reactions(h.store, h.ctx().window, 1,
                                         {1: "Testy", 101: "Arkon"})] == ["none", "none"]


def test_other_private_conversations_never_answer_or_laugh_at_a_bid(h):
    h.session(h.T - 1, h.T + 600)
    line = h.say(h.T, 101, "testy hello", channel="whisper", to=1)
    h.say(h.T + 10, 1, "arkon lol", channel="whisper", to=102)
    rs = reactions(h.store, h.ctx().window, 1, {1: "Testy", 101: "Arkon", 102: "Belra"})
    assert rs[0].reaction == "ignored"
    assert laughs_at(h.store, line) == set()
    h.say(h.T + 20, 1, "haha yes", channel="whisper", to=101)
    assert laughs_at(h.store, line) == {1}


def test_band_hysteresis():
    assert [band_for(a, "fond") for a in (39.9, 40, 59.9, 60)] == ["fond", "drawn", "drawn", "lust"]
    assert [band_for(a, "lust") for a in (50, 49.9, 30, 29.9)] == ["lust", "drawn", "drawn", "fond"]
    assert [band_for(a, "drawn") for a in (30, 29.9, 60)] == ["drawn", "fond", "lust"]


def test_gain_sizes_taste_and_caps():
    plain = make_persona(102, "Belra", player_gender="female", stance="drawn")
    keen = replace(plain, romance=replace(plain.romance, drawn_to=("flirting and teasing back",)))
    assert gain("flirt", plain, science=1.0) == (20.0, 1.0)
    assert gain("flirt", keen, science=1.5) == (40.0, 2.0)
    assert gain("answered", plain, science=1.5) == (4.5, 1.5)
    assert science(rush=True, similarity=1, social=True, scarcity=True) == 1.5
    assert science(rush=False, similarity=1 / 3, social=False, scarcity=False) == pytest.approx(1 + .25 / 3)


def test_decay_timeline_and_floor():
    score = 70.
    for n in range(1, 496):
        score = decay(score, 1, HALF_LIFE_DAYS)
        if n == 156:
            assert band_for(score, "lust") == "lust"
        if n == 157:
            assert band_for(score, "lust") == "drawn"
        if n == 494:
            assert band_for(score, "drawn") == "drawn"
    assert band_for(score, "drawn") == "fond"
    assert band_for(decay(40, 212, HALF_LIFE_DAYS), "drawn") == "drawn"
    assert band_for(decay(40, 214, HALF_LIFE_DAYS), "drawn") == "fond"
    assert decay(25, 10000, 1) == pytest.approx(20)
    assert decay(15, 100, 1) == 15


def test_half_life_deep_hurt_persists_without_current_dislike(h):
    p = h.woman(103, "lust", dislikes=["being ignored"])
    assert night_half_life(p, neglected=True, hurt_tonight=False,
                           in_hurt_window=False, hurt_deep=False) == 106.5
    assert night_half_life(p, neglected=False, hurt_tonight=False,
                           in_hurt_window=True, hurt_deep=True) == 53.25


def test_reconcile_first_sight_hand_move_and_other_import(h):
    h.woman(101, "drawn")
    with h.store.transaction():
        assert reconcile(h.store, 101, now=h.T) == ("drawn", True)
    assert h.store.get_relationship(101, 1).attraction == 40
    assert ledger_rows(h.store, 101)[0]["text"] == "drawn: first sight at 40"
    h.rel(101, 1, attraction=25)
    h.woman(101, "lust")
    with h.store.transaction():
        assert reconcile(h.store, 101, now=h.T + 1) == ("lust", True)
    assert h.store.get_relationship(101, 1).attraction == 60
    h.rel(101, 1, attraction=65)
    h.woman(101, "lust", likes=["fishing at dawn"])
    with h.store.transaction():
        assert reconcile(h.store, 101, now=h.T + 2) == ("lust", False)
    assert h.store.get_relationship(101, 1).attraction == 65


def test_first_sight_equal_score_has_no_ledger_and_men_none_skip(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=40)
    h.persona(102, stance="lust", player_gender="male")
    h.woman(103, "none")
    with h.store.transaction():
        assert reconcile(h.store, 101, now=h.T) == ("drawn", True)
        assert reconcile(h.store, 102, now=h.T) is None
        assert reconcile(h.store, 103, now=h.T) is None
    assert h.count("attraction_state") == 1 and not ledger_rows(h.store)


def test_act_bands_and_preserves_imported_fields(h):
    h.woman(101, "drawn", likes=["fishing at dawn"])
    with h.store.transaction():
        stance, _ = reconcile(h.store, 101, now=h.T)
        assert apply_act(h.store, human=1, guid=101, act="flirt", amount=20, mult=1,
                         ref="chat:1", ts=h.T, live=True, stance=stance) == "lust"
    assert load_persona(h.store, 101).likes == ("fishing at dawn",)
    assert ledger_rows(h.store, 101)[0]["score_before"] == 40
    assert h.store.pending_rel_deltas() == []


def test_stance_write_rolls_back_with_its_score_and_ledger(h):
    h.woman(101, "drawn")
    h.seen(101)
    version = h.store.persona_versions()[101]
    with pytest.raises(RuntimeError), h.store.transaction():
        set_stance(h.store, 101, "lust", now=h.T, reason="act flirt", evidence=[], score_before=45)
        raise RuntimeError("rollback")
    assert load_persona(h.store, 101).romance.stance == "drawn"
    assert h.store.persona_versions()[101] == version
    assert not ledger_rows(h.store)


def test_neglect_only_decays_and_stage_is_idempotent(h):
    h.woman(103, "lust")
    h.rel(103, 1, attraction=70)
    h.seen(103)
    neglect_night(h, h.T)
    ctx = h.ctx()
    run(stage_attraction(ctx))
    expected = decay(70, 1, HALF_LIFE_DAYS)
    assert h.store.get_relationship(103, 1).attraction == pytest.approx(expected)
    memories = [m for m in h.rows(103) if m.key == "hurt:2026-10-29"]
    assert len(memories) == 1 and memories[0].text == "Testy didn't answer me tonight."
    assert memories[0].scope == "open"
    state = h.store.conn.execute("SELECT * FROM attraction_state WHERE guid=103").fetchone()
    assert state["answer_rate"] == 0 and state["bids_7d"] == 4
    run(stage_attraction(ctx))
    assert h.store.get_relationship(103, 1).attraction == expected
    assert h.count("emotion") == 1 and h.count("attraction_log") == 1
    h.clock.set(h.T + 86400)
    run(stage_attraction(h.ctx(night="2026-10-30")))  # offline, no bids
    assert h.store.get_relationship(103, 1).attraction == expected


def test_one_answer_cancels_neglect_for_the_whole_night(h):
    h.woman(103, "lust")
    h.rel(103, 1, attraction=70)
    h.seen(103)
    neglect_night(h, h.T)
    h.say(h.T + 2400, 103, "testy are you there")
    h.say(h.T + 2410, 1, "yes coros")
    run(stage_attraction(h.ctx()))
    assert h.store.get_relationship(103, 1).attraction == 70
    assert not h.rows(103) and h.count("emotion") == 0


def test_a_bid_without_keyboard_activity_or_a_bid_outside_session_never_decays(h):
    h.woman(103, "lust")
    h.rel(103, 1, attraction=70)
    h.seen(103)
    h.say(h.T, 103, "testy are you there")  # no human session
    h.say(h.T + 10, 1, "dunna hi")
    h.session(h.T + 60, h.T + 3600)
    h.say(h.T + 600, 103, "testy are you there")  # online is not demonstrable attention
    run(stage_attraction(h.ctx()))
    assert h.store.get_relationship(103, 1).attraction == 70


def test_epoch_window_without_observations_does_not_walk_calendar(h, monkeypatch):
    from leeroy_mind.nightly import attraction
    h.woman(101, "lust")
    h.rel(101, 1, attraction=70)
    h.seen(101)

    def no_calendar(*args):
        raise AssertionError("an empty history has no nights to decay")

    monkeypatch.setattr(attraction, "night_bounds", no_calendar)
    ctx = h.ctx(window=(0, h.T))
    run(stage_attraction(ctx))
    assert not ctx.failures
    assert h.store.get_relationship(101, 1).attraction == 70


def test_sparse_history_decays_only_two_observed_nights(h, monkeypatch):
    from leeroy_mind.nightly import attraction
    h.woman(101, "lust")
    h.rel(101, 1, attraction=70)
    h.seen(101)
    neglect_night(h, h.T, guids=(101,))
    neglect_night(h, h.T + 180 * 86400, guids=(101,))
    real_bounds, dates = attraction.night_bounds, []

    def observed_only(night, tz):
        dates.append(night)
        assert len(dates) <= 2, "offline dates must not be iterated"
        return real_bounds(night, tz)

    monkeypatch.setattr(attraction, "night_bounds", observed_only)
    ctx = h.ctx(window=(0, h.clock.now() + 1))
    run(stage_attraction(ctx))
    assert not ctx.failures and len(dates) == 2
    assert h.store.get_relationship(101, 1).attraction == pytest.approx(decay(70, 2, HALF_LIFE_DAYS))
    assert len([m for m in h.rows(101) if m.key.startswith("hurt:")]) == 2


def test_private_neglect_memory_keeps_its_partner(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=45)
    h.seen(101)
    h.session(h.T - 1, h.T + 3600)
    h.say(h.T, 101, "you around", channel="whisper", to=1)
    h.say(h.T + 10, 1, "dunna hi")
    run(stage_attraction(h.ctx()))
    memory = h.rows(101)[0]
    assert (memory.scope, memory.scope_with) == ("private", 1)
    assert memory.importance == 4 and memory.valence == -.4


def test_private_mention_of_human_to_another_member_is_not_a_bid(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=45)
    h.seen(101)
    h.session(h.T - 1, h.T + 3600)
    h.say(h.T, 101, "testy hasn't replied yet", channel="whisper", to=102)
    h.say(h.T + 10, 1, "dunna hi")
    run(stage_attraction(h.ctx()))
    assert h.store.get_relationship(101, 1).attraction == 45
    assert not h.rows(101)


def test_hurt_deep_lasts_two_weeks_but_never_decays_no_bid_nights(h):
    h.woman(101, "lust", turned_off_by=("rudeness",))
    h.rel(101, 1, attraction=70)
    h.seen(101)
    run(h.live().on_chat(h.his(h.T, "stfu arkon")))
    run(stage_attraction(h.ctx()))
    score = decay(70, 1, 53.25)
    assert h.store.get_relationship(101, 1).attraction == score
    h.clock.set(h.T + 5 * 86400)
    run(stage_attraction(h.ctx(night="2026-11-03")))
    assert h.store.get_relationship(101, 1).attraction == score
    h.woman(101, "lust")  # removal of the dislike does not erase the existing deep hurt window
    neglect_night(h, h.T + 10 * 86400, guids=(101,))
    run(stage_attraction(h.ctx(night="2026-11-08")))
    score = decay(score, 1, 53.25)
    assert h.store.get_relationship(101, 1).attraction == pytest.approx(score)
    neglect_night(h, h.T + 15 * 86400, guids=(101,))
    run(stage_attraction(h.ctx(night="2026-11-13")))
    assert h.store.get_relationship(101, 1).attraction == pytest.approx(decay(score, 1, 213))


def test_deep_neglect_and_hurt_memory_precedence(h):
    h.woman(101, "lust", dislikes=["being ignored"])
    h.rel(101, 1, attraction=70)
    h.seen(101)
    neglect_night(h, h.T, guids=(101,))
    run(stage_attraction(h.ctx()))
    assert h.store.get_relationship(101, 1).attraction == pytest.approx(decay(70, 1, 106.5))
    neglect_night(h, h.T + 86400, guids=(101,))
    run(h.live().on_chat(h.his(h.T + 86400 + 3000, "stfu arkon")))
    run(stage_attraction(h.ctx(night="2026-10-30")))
    memories = [m for m in h.rows(101) if m.key == "hurt:2026-10-30"]
    assert len(memories) == 1 and memories[0].text == "Testy was harsh with me tonight."
    row = h.store.conn.execute("SELECT * FROM attraction_log WHERE night='2026-10-30'").fetchone()
    assert row["hurt"] == 1 and row["neglected"] == 0 and row["half_life"] == 106.5


def test_nightly_first_sight_and_hand_move_never_reband_stale_scores(h):
    h.woman(101, "drawn")
    h.woman(102, "fond")
    h.woman(103, "lust")
    h.rel(103, 1, attraction=60)
    run(stage_attraction(h.ctx()))
    assert [h.store.get_relationship(g, 1).attraction for g in (101, 102, 103)] == [40, 20, 60]
    assert ledger_rows(h.store, 103) == []
    h.rel(101, 1, attraction=25)
    h.woman(101, "lust")
    h.clock.set(h.T + 86400)
    run(stage_attraction(h.ctx(night="2026-10-30")))
    assert h.store.get_relationship(101, 1).attraction == 60
    assert load_persona(h.store, 101).romance.stance == "lust"
    assert ledger_rows(h.store, 101)[0]["text"] == "drawn -> lust: hand move"


def test_band_drop_saves_version_and_logs_exact_decay(h):
    h.woman(103, "lust")
    h.rel(103, 1, attraction=50.05)
    h.seen(103)
    version = h.store.persona_versions()[103]
    neglect_night(h, h.T)
    run(stage_attraction(h.ctx()))
    assert load_persona(h.store, 103).romance.stance == "drawn"
    assert h.store.persona_versions()[103] == version + 1
    row = h.store.conn.execute("SELECT * FROM attraction_log WHERE guid=103").fetchone()
    assert row["before"] == 50.05 and row["decayed"] == pytest.approx(row["before"] - row["after"])
    assert (row["stance_before"], row["stance_after"]) == ("lust", "drawn")


def test_extraction_positive_counts_once_and_negatives_are_not_hurt(h):
    h.woman(101, "drawn")
    live = h.live()
    ev = h.his(h.T, "arkon you're cute")
    line = h.store.conn.execute("SELECT max(id) FROM chat_line").fetchone()[0]
    run(live.on_chat(ev))
    h.store.add_rel_delta(101, 1, "attraction", 3, "noticed the compliment", [f"chat:{line}"], h.T, applied=False)
    other = h.say(h.T + 1, 1, "arkon that was kind")
    h.store.add_rel_delta(101, 1, "attraction", 2, "thoughtful", [f"chat:{other}"], h.T + 1, applied=False)
    h.store.add_rel_delta(101, 1, "attraction", -5, "model dislike", [f"chat:{other}"], h.T + 1, applied=False)
    run(stage_attraction(h.ctx()))
    assert h.store.get_relationship(101, 1).attraction == 62
    assert h.store.pending_rel_deltas() == []
    assert h.store.conn.execute("SELECT count(*) FROM attraction_act WHERE act='hurt'").fetchone()[0] == 0
    assert h.store.conn.execute("SELECT reason FROM rel_delta WHERE reason LIKE 'noticed the%'").fetchone()[0].endswith(" (already counted live)")
    assert h.store.conn.execute("SELECT gained FROM attraction_log WHERE guid=101").fetchone()[0] == 22


def test_late_extraction_uses_source_night_and_current_persona_write_time(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=59)
    h.seen(101)
    neglect_night(h, h.T, guids=(101,))
    source_time = h.T + 3000
    source = h.say(source_time, 1, "arkon that was thoughtful")
    ctx = h.ctx()
    processed = ctx.window[1] + 300  # extractor finishes after the night's window closed
    h.clock.set(processed)
    h.store.add_rel_delta(101, 1, "attraction", 2, "a kind gesture", [f"chat:{source}"], processed, applied=False)
    run(stage_attraction(ctx))
    assert h.store.get_relationship(101, 1).attraction == 61
    assert not h.rows(101)  # the real positive act means this was not a neglected night
    act = h.store.conn.execute("SELECT * FROM attraction_act WHERE guid=101").fetchone()
    assert act["ts"] == source_time and act["gain"] == 2
    row = h.store.conn.execute("SELECT * FROM attraction_log WHERE guid=101").fetchone()
    assert row["gained"] == 2 and row["neglected"] == 0
    ledger = ledger_rows(h.store, 101)[0]
    assert ledger["ts"] == processed
    assert json.loads(h.store.get_meta("attraction_band_changes:2026-10-29")) == [ledger["id"]]
    assert h.store.conn.execute("SELECT updated FROM persona_core WHERE guid=101").fetchone()[0] == processed


def test_known_old_source_time_is_not_clipped_into_new_night(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=45)
    h.seen(101)
    source = h.say(h.T, 1, "arkon that was thoughtful")
    h.clock.set(h.T + 86400)
    h.store.add_rel_delta(101, 1, "attraction", 2, "late old thread", [f"chat:{source}"], h.clock.now(), applied=False)
    run(stage_attraction(h.ctx(night="2026-10-30")))
    assert h.store.get_relationship(101, 1).attraction == 47
    assert h.store.conn.execute("SELECT ts FROM attraction_act WHERE guid=101").fetchone()[0] == h.T
    assert h.store.conn.execute("SELECT gained FROM attraction_log WHERE guid=101").fetchone()[0] == 0


def test_undated_nudge_has_logical_time_inside_closed_window(h):
    h.woman(101, "fond")
    ctx = h.ctx()
    h.clock.set(ctx.window[1] + 300)
    h.store.add_rel_delta(101, 1, "attraction", 2, "undated", [], h.clock.now(), applied=False)
    run(stage_attraction(ctx))
    event_time = h.store.conn.execute("SELECT ts FROM attraction_act WHERE guid=101").fetchone()[0]
    assert ctx.window[0] <= event_time < ctx.window[1]
    assert h.store.conn.execute("SELECT gained FROM attraction_log WHERE guid=101").fetchone()[0] == 2


def test_mixed_rivalry_refs_win_over_own_line_without_damage(h):
    h.woman(101, "drawn")
    h.woman(102, "drawn")
    h.rel(101, 1, attraction=45)
    h.seen(101, 102)
    own = h.say(h.T, 1, "arkon you're cute")
    rival = h.say(h.T + 1, 1, "belra you're cute")
    h.store.add_rel_delta(101, 1, "attraction", -3, "mixed thread", [f"chat:{own}", f"chat:{rival}"], h.T, applied=False)
    run(stage_attraction(h.ctx()))
    assert h.store.get_relationship(101, 1).attraction == 45
    assert h.store.conn.execute("SELECT reason FROM rel_delta WHERE reason LIKE 'mixed%'").fetchone()[0] == f"mixed thread (rivalry, not hurt: chat:{rival})"
    assert h.count("attraction_act") == 0 and h.count("emotion") == 0


def test_pending_nudges_for_men_and_no_attitude_are_zeroed(h):
    h.persona(101, stance="lust", player_gender="male")
    h.woman(102, "none")
    for guid in (101, 102):
        h.store.add_rel_delta(guid, 1, "attraction", 4, "no attitude", [], h.T, applied=False)
    run(stage_attraction(h.ctx()))
    assert h.count("attraction_act") == h.count("attraction_state") == h.count("attraction_log") == 0
    rows = h.store.conn.execute("SELECT * FROM rel_delta").fetchall()
    assert all(r["delta"] == 0 and r["applied"] and r["reason"].endswith(" (not applied: no attitude)") for r in rows)


def test_undo_stance_restores_exact_pre_act_score_and_survives_reconcile(h):
    h.woman(101, "drawn")
    h.rel(101, 1, attraction=45)
    h.seen(101)
    run(h.live().on_chat(h.his(h.T, "arkon you're cute")))
    row = ledger_rows(h.store, 101)[0]
    cache = PersonaCache(h.store)
    assert cache.get(101).romance.stance == "lust"
    assert undo(h.store, row["id"], now=h.T + 1, personas=cache)
    assert cache.get(101).romance.stance == "drawn"
    assert h.store.get_relationship(101, 1).attraction == 45
    with h.store.transaction():
        assert reconcile(h.store, 101, now=h.T + 2) == ("drawn", False)
    assert not undo(h.store, row["id"], now=h.T + 3)
    assert h.store.get_relationship(101, 1).attraction == 45


def test_undo_refuses_seed_and_hand_move_rows(h):
    h.woman(101, "drawn")
    with h.store.transaction():
        reconcile(h.store, 101, now=h.T)
    first = ledger_rows(h.store, 101)[0]
    assert not undo(h.store, first["id"], now=h.T + 1)
    h.woman(101, "lust")
    with h.store.transaction():
        reconcile(h.store, 101, now=h.T + 2)
    hand = ledger_rows(h.store, 101)[0]
    assert not undo(h.store, hand["id"], now=h.T + 3)
