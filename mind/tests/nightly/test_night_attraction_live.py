import asyncio
from dataclasses import replace

import pytest

from leeroy_mind.nightly.attraction import (COMPLIMENT_RE, FLIRT_RE, HURT_RE, LAUGH_RE,
    TEASE_RE, LiveAttraction, aimed_at, science_for, stage_attraction)
from leeroy_mind.nightly.store import ledger_rows
from leeroy_mind.personas.io import load_persona
from leeroy_mind.protocol import Boss, Group

run = asyncio.run


def act_rows(h, guid=101):
    return h.store.conn.execute("SELECT * FROM attraction_act WHERE guid=? ORDER BY id", (guid,)).fetchall()


def test_first_flirty_whisper_bands_immediately_and_invalidates_cache(h):
    h.woman(101, "drawn")
    live = h.live()
    live.personas.get(101)
    ev = h.his(h.T, "you're so cute arkon", channel="whisper", to=101)
    run(live.on_chat(ev))
    assert h.store.get_relationship(101, 1).attraction == 60
    assert live.personas.get(101).romance.stance == "lust"
    assert act_rows(h)[0]["act"] == "flirt"
    run(live.on_chat(ev))
    assert len(act_rows(h)) == 1
    run(h.live().on_chat(ev))  # a new handler does not count a persisted live act twice
    assert len(act_rows(h)) == 1


def test_imported_stance_and_fields_win_even_with_cached_candidates(h):
    h.woman(101, "fond")
    h.rel(101, 1, attraction=20)
    h.seen(101)
    live = h.live()
    live.women()
    live.personas.get(101)
    h.woman(101, "drawn", likes=["fishing at dawn"])
    run(live.on_chat(h.his(h.T + 1, "you're cute arkon")))
    p = load_persona(h.store, 101)
    assert p.romance.stance == "lust" and p.likes == ("fishing at dawn",)
    assert [r["text"] for r in ledger_rows(h.store, 101)] == [
        "drawn -> lust: act flirt", "fond -> drawn: hand move"]


def test_men_none_import_and_bridge_lines_never_gain(h):
    h.persona(101, stance="drawn", player_gender="male")
    h.woman(102, "none")
    live = h.live()
    run(live.on_chat(h.his(h.T, "arkon belra you're cute")))
    assert not act_rows(h)
    h.woman(101, "drawn")
    live = h.live()
    live.women()
    h.woman(101, "none")
    run(live.on_chat(h.his(h.T + 1, "arkon you're cute")))
    h.woman(102, "drawn")
    run(h.live().on_chat(replace(h.his(h.T + 2, "belra you're cute"), via_bridge=True)))
    assert h.count("attraction_act") == h.count("attraction_state") == 0


def test_import_from_none_is_eligible_on_first_act_within_cache_ttl(h):
    h.woman(101, "none")
    live = h.live()
    assert 101 not in live.women()
    h.woman(101, "drawn")
    run(live.on_chat(h.his(h.T + 1, "arkon you're cute")))
    assert h.store.get_relationship(101, 1).attraction == 60
    assert load_persona(h.store, 101).romance.stance == "lust"


def test_private_mention_never_flirts_with_unrelated_recipient(h):
    h.woman(101, "drawn")
    h.woman(102, "drawn")
    run(h.live().on_chat(h.his(h.T, "arkon you're cute", channel="whisper", to=102)))
    assert not act_rows(h, 101)
    assert len(act_rows(h, 102)) == 1


@pytest.mark.parametrize("text,flirt,compliment,hurt", [
    ("you're so cute", True, False, False), ("hey gorgeous", True, False, False),
    ("i miss you", True, False, False), ("i miss you guys", False, False, False),
    ("you're out of mana", False, False, False), ("nice heals belra <3", False, True, False),
    ("belra pull the boss ;)", False, False, False), ("belra you're on fire", False, False, False),
    ("stfu arkon", False, False, True), ("lol shut up you're hilarious", False, False, False),
    ("shut up cutie", True, False, False),
])
def test_recogniser_edge_cases(text, flirt, compliment, hurt):
    assert bool(FLIRT_RE.search(text)) == flirt
    assert bool(COMPLIMENT_RE.search(text)) == compliment
    assert bool(HURT_RE.search(text) and not (TEASE_RE.search(text) or LAUGH_RE.search(text))) == hurt


def test_aiming_real_first_name_requires_title_case(h):
    p = h.woman(101, "drawn", real_name="May Example")
    row = {"channel": "guild", "to_guid": 0, "text": "may you have nice heals"}
    assert not aimed_at(row, p, 101)
    assert aimed_at(dict(row, text="May nice heals"), p, 101)
    assert aimed_at(dict(row, text="ARKON nice heals"), p, 101)


def test_verified_personal_callback_counts_once_and_tastes_double(h):
    h.woman(101, "fond", drawn_to=("being remembered",))
    old = h.say(h.T - 86400, 101, "testy my sister had her interview on her birthday")
    mid = h.mem(101, "My sister had her interview on her birthday.", t=h.T - 86400,
                evidence=(f"chat:{old}",), actors=(1,))
    live = h.live()
    run(live.on_chat(h.his(h.T, "arkon how did the birthday interview go")))
    rows = act_rows(h)
    assert len(rows) == 1 and rows[0]["act"] == "remembered"
    assert rows[0]["ref"] == f"memory:{mid}" and rows[0]["gain"] == 10
    run(live.on_chat(h.his(h.T + 60, "arkon how did the birthday interview go")))
    assert len(act_rows(h)) == 1


def test_callback_replay_cannot_fall_through_to_a_second_act(h):
    h.woman(101, "fond")
    line = h.say(h.T - 86400, 101, "testy my interview was on my birthday")
    h.mem(101, "My interview was on my birthday.", t=h.T - 86400, evidence=(f"chat:{line}",))
    ev = h.his(h.T, "arkon nice job on that birthday interview")
    run(h.live().on_chat(ev))
    assert act_rows(h)[0]["act"] == "remembered"
    run(h.live().on_chat(ev))
    assert len(act_rows(h)) == 1


def test_older_replay_does_not_select_a_future_identical_line(h):
    h.woman(101, "fond")
    old = h.his(h.T, "arkon you're cute")
    run(h.live().on_chat(old))
    run(h.live().on_chat(h.his(h.T + 60, "arkon nice job")))
    new = h.his(h.T + 120, "arkon you're cute")
    run(h.live().on_chat(old))
    assert len(act_rows(h)) == 2
    run(h.live().on_chat(new))
    assert len(act_rows(h)) == 3


def test_distinct_human_lines_in_same_millisecond_both_count(h):
    h.woman(101, "fond")
    run(h.live().on_chat(h.his(h.T, "arkon you're cute")))
    run(h.live().on_chat(h.his(h.T, "arkon nice job")))
    assert [r["act"] for r in act_rows(h)] == ["flirt", "public_compliment"]


def test_source_receipt_rolls_back_with_failed_act(h, monkeypatch):
    from leeroy_mind.nightly import attraction
    h.woman(101, "fond")
    ev = h.his(h.T, "arkon you're cute")
    original = attraction.apply_act

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic rollback")

    monkeypatch.setattr(attraction, "apply_act", fail)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        run(h.live().on_chat(ev))
    assert not act_rows(h)
    assert not h.store.conn.execute("SELECT 1 FROM meta WHERE key LIKE 'attraction_chat%'").fetchall()
    monkeypatch.setattr(attraction, "apply_act", original)
    run(h.live().on_chat(ev))
    assert len(act_rows(h)) == 1


@pytest.mark.parametrize("remembered,expected", [(True, 5), (False, 3)])
def test_extraction_cannot_recount_his_callback_or_answer(h, remembered, expected):
    h.woman(101, "fond")
    if remembered:
        line = h.say(h.T - 86400, 101, "testy my interview was on my birthday")
        h.mem(101, "My interview was on my birthday.", t=h.T - 86400, evidence=(f"chat:{line}",))
        text = "arkon how was the birthday interview"
    else:
        h.say(h.T - 10, 101, "testy are you coming")
        text = "yes arkon"
    ev = h.his(h.T, text)
    source = h.store.conn.execute("SELECT max(id) FROM chat_line").fetchone()[0]
    run(h.live().on_chat(ev))
    # A newer act exists before extraction of the old thread; a high-watermark alone cannot
    # prove that the old human line was already counted.
    run(h.live().on_chat(h.his(h.T + 60, "arkon nice job")))
    h.store.add_rel_delta(101, 1, "attraction", 2, "old thread", [f"chat:{source}"], h.T, applied=False)
    run(stage_attraction(h.ctx()))
    assert h.store.get_relationship(101, 1).attraction == 20 + expected + 5
    assert h.store.conn.execute("SELECT reason FROM rel_delta WHERE reason LIKE 'old thread%'").fetchone()[0].endswith(" (already counted live)")


@pytest.mark.parametrize("private_other,game_only", [(True, False), (False, True)])
def test_callback_requires_visible_personal_source(h, private_other, game_only):
    h.woman(101, "fond")
    text = "testy heroic dungeon healer" if game_only else "my sister finished her interview"
    line = h.say(h.T - 86400, 101, text, channel="whisper" if private_other else "guild", to=102)
    h.mem(101, text, t=h.T - 86400, evidence=(f"chat:{line}",))
    run(h.live().on_chat(h.his(h.T, "arkon " + text)))
    assert not act_rows(h)


def test_generic_chat_is_not_a_stream_of_answered_gains(h):
    h.woman(101, "fond")
    live = h.live()
    for i in range(40):
        h.say(h.T + i * 120, 101, "nice weather")
        run(live.on_chat(h.his(h.T + i * 120 + 10, "yep")))
    assert not act_rows(h)


def test_answered_bid_once_and_three_per_mind_day(h):
    h.woman(101, "fond")
    live = h.live()
    for i in range(4):
        h.say(h.T + i * 600, 101, "testy are you coming")
        ev = h.his(h.T + i * 600 + 10, "yep")
        run(live.on_chat(ev))
        run(live.on_chat(ev))
    assert len(act_rows(h)) == 3
    assert h.store.get_relationship(101, 1).attraction == 29
    next_day = h.T + 9 * 3600  # 06:00 local, the new mind day
    h.say(next_day, 101, "testy are you coming")
    run(live.on_chat(h.his(next_day + 10, "yep")))
    assert len(act_rows(h)) == 4


def test_answer_window_and_private_conversations_are_separate(h):
    h.woman(101, "fond")
    live = h.live()
    h.say(h.T, 101, "testy are you coming")
    run(live.on_chat(h.his(h.T + 75, "yep")))
    assert not act_rows(h)
    h.say(h.T + 600, 101, "you there", channel="whisper", to=1)
    run(live.on_chat(h.his(h.T + 610, "arkon yes", channel="whisper", to=102)))
    assert not act_rows(h)
    run(live.on_chat(h.his(h.T + 800, "yep", channel="whisper", to=101)))
    assert act_rows(h)[0]["act"] == "answered"


@pytest.mark.parametrize("minutes,expected", [(8, 0), (25, 2), (45, 2), (65, 4)])
@pytest.mark.parametrize("raid", [False, True])
def test_party_and_raid_time_gain_on_departure(h, minutes, expected, raid):
    h.woman(101, "fond")
    h.store.start_session(h.T)
    live = h.live()
    run(live.on_group(Group(0, 1, raid, (1, 101))))
    h.clock.advance(minutes * 60)
    run(live.on_group(Group(0, 1, raid, (1,))))
    assert sum(r["gain"] for r in act_rows(h)) == expected
    assert h.store.get_relationship(101, 1).attraction == 20 + expected
    run(live.on_logout(h.logout()))
    assert sum(r["gain"] for r in act_rows(h)) == expected


def test_resync_clears_timers_without_credit_and_logout_closes_them(h):
    h.woman(101, "fond")
    live = h.live()
    run(live.on_group(Group(0, 1, False, (1, 101))))
    h.clock.advance(3600)
    run(live.on_hello(h.hello(1)))
    assert not act_rows(h)
    run(live.on_group(Group(0, 1, False, (1, 101))))
    h.clock.advance(1800)
    run(live.on_logout(h.logout()))
    assert sum(r["gain"] for r in act_rows(h)) == 2


@pytest.mark.parametrize("final", [False, True])
def test_hard_won_kill_rush_expires(h, final):
    h.woman(101, "fond")
    boss = 12 if final else 0
    if not final:
        for i in range(3):
            h.boss(h.T - 300 + i * 60, 631, boss, "wiped", (1, 101))
    live = h.live()
    run(live.on_boss(Boss(0, 631, 1, boss, "", "killed", (1, 101))))
    run(live.on_chat(h.his(h.T + 60, "arkon you're cute")))
    assert act_rows(h)[0]["gain"] == 30
    run(live.on_chat(h.his(h.T + 3600, "arkon you're cute")))
    assert act_rows(h)[1]["gain"] == 20


def test_science_helper_is_used_for_group_gains(h):
    h.woman(101, "fond")
    h.seen(101)
    with h.store.transaction():
        h.store.conn.execute("UPDATE attraction_state SET rush_until=? WHERE guid=101", (h.T + 7200,))
    assert science_for(h.store, 101, human=1, now=h.T, local=h.clock.local()) == 1.5
    live = h.live()
    run(live.on_group(Group(0, 1, False, (1, 101))))
    h.clock.advance(1800)
    run(live.on_logout(h.logout()))
    assert act_rows(h)[0]["gain"] == 3


def test_hurt_marks_window_only_then_night_decays_privately(h):
    h.woman(101, "drawn", turned_off_by=("rudeness",))
    h.rel(101, 1, attraction=45)
    h.seen(101)
    live = h.live()
    run(live.on_chat(h.his(h.T, "stfu", channel="whisper", to=101)))
    assert h.store.get_relationship(101, 1).attraction == 45
    assert act_rows(h)[0]["act"] == "hurt"
    run(stage_attraction(h.ctx()))
    assert h.store.get_relationship(101, 1).attraction < 45
    row = next(m for m in h.rows(101) if m.key == "hurt:2026-10-29")
    assert row.text == "Testy was harsh with me tonight." and row.scope == "private" and row.scope_with == 1
    assert h.store.conn.execute("SELECT half_life FROM attraction_log WHERE guid=101").fetchone()[0] == 53.25
