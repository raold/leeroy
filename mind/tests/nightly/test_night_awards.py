"""Season trophies count actual eligible activity and retain the winner's own voice."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import run
from leeroy_mind.nightly.awards import AWARDS, stage_awards
from leeroy_mind.nightly.settings import season_bounds, season_of
from leeroy_mind.protocol import Death

UTC = timezone.utc
NIGHT = "2026-10-04"
SEASON = "2026-S5"


def bounds(h, night=NIGHT):
    ctx = h.ctx(night=night)
    return season_bounds(season_of(night, ctx.settings.season_weeks), ctx.settings.season_weeks, ctx.tz)


def death(h, at, guid, map_id=631):
    return h.store.insert_game_event(Death(ts=int(at * 1000), guid=guid, map=map_id, killer_entry=0),
                                     session_id=None, ts=at)


def finish(h, night=NIGHT):
    ctx = h.ctx(night=night)
    _, end = bounds(h, night)
    h.clock.set(end)
    run(stage_awards(ctx))
    return ctx


def awarded(h):
    return {row["title"]: dict(row) for row in h.store.conn.execute("SELECT * FROM award ORDER BY id")}


def test_exact_titles_and_all_six_counters_with_participant_copies(h):
    assert tuple(AWARDS) == ("Most Likely to Stand in Fire", "Most Whispered", "Loot Goblin",
                             "Class Clown", "Night Owl", "Never Missed a Pull")
    start, end = bounds(h)
    at = start + 6 * 3600
    h.session(start, end)
    death(h, at, 103)
    death(h, at + 1, 103)
    death(h, at + 2, 101)
    for i in range(3):
        h.say(at + 10 + i, 1, "a private detail never copied into trophies", channel="whisper", to=101)
        h.loot(at + 20 + i, 102, "An ordinary item")
    h.loot(at + 24, 101, "Another item")
    for i in range(2):
        h.say(at + 100 + i * 120, 104, "That was a funny pull.")
        h.say(at + 110 + i * 120, 1, "haha yes")
    late = datetime(2026, 9, 5, 1, tzinfo=UTC).timestamp()
    h.utter(late, 105, "Still awake.")
    h.utter(late + 10, 105, "One more pull.")
    h.boss(at + 400, 631, 11, "wipe", (101, 102, 103))
    h.boss(at + 410, 631, 11, "wipe", (101, 104))
    h.boss(at + 420, 631, 11, "killed", (101, 105))
    ctx = finish(h)
    rows = awarded(h)
    assert {title: (row["guid"], row["value"]) for title, row in rows.items()} == {
        "Most Likely to Stand in Fire": (103, 2), "Most Whispered": (101, 3), "Loot Goblin": (102, 3),
        "Class Clown": (104, 2), "Night Owl": (105, 2), "Never Missed a Pull": (101, 3)}
    assert ctx.done("stories", f"awards:{SEASON}")
    assert ctx.reasoner.llm.calls == []
    for n, title in enumerate(AWARDS):
        row = rows[title]
        winner = row["guid"]
        key = f"award:{SEASON}:{n}"
        for owner in (101, 102, 103, 104, 105):
            memory = h.store.keyed_memory(owner, key)
            assert memory is not None and memory.evidence == (f"award:{row['id']}",)
            assert (memory.kind, memory.source, memory.scope, memory.scope_with) == ("event", "game", "open", 0)
            if owner == winner:
                assert memory.text == f"Won '{title}' this season." and memory.importance == 6 and memory.valence == 0.3
            else:
                assert memory.text == f"{ctx.name(winner)} won '{title}' this season." and memory.importance == 4
    assert h.count("memory") == 30 and not h.rows(0) and not h.rows(1)
    assert all("private detail" not in m.text for owner in (101, 102, 103, 104, 105) for m in h.rows(owner))


def test_zero_activity_finishes_without_awards_or_memories(h):
    ctx = finish(h)
    assert awarded(h) == {} and h.count("memory") == 0
    assert ctx.done("stories", f"awards:{SEASON}")


@pytest.mark.parametrize("night", ["2026-10-03", "2026-10-11"])
def test_only_the_season_ending_sunday_runs(h, night):
    start, _ = bounds(h)
    death(h, start + 1, 103)
    ctx = h.ctx(night=night)
    run(stage_awards(ctx))
    assert h.count("award") == h.count("memory") == 0
    assert h.count("night_step") == 0


def test_ties_choose_lower_guid_and_nonraider_winner_still_remembers(h):
    start, _ = bounds(h)
    h.loot(start + 1, 104, "first")
    h.loot(start + 2, 103, "second")
    h.boss(start + 3, 631, 11, "killed", (101, 102))
    finish(h)
    assert awarded(h)["Loot Goblin"]["guid"] == 103
    key = f"award:{SEASON}:2"
    assert h.store.keyed_memory(103, key).text == "Won 'Loot Goblin' this season."
    assert h.store.keyed_memory(101, key) and h.store.keyed_memory(102, key)
    assert h.store.keyed_memory(104, key) is None and h.store.keyed_memory(105, key) is None


def test_human_noncast_and_owner_zero_never_win_or_receive_copies(h):
    start, _ = bounds(h)
    for guid in (0, 1, 999):
        for i in range(4):
            death(h, start + 1 + i, guid)
            h.loot(start + 10 + i, guid, "a thing")
            h.say(start + 20 + i, 1, "hello", channel="whisper", to=guid)
    death(h, start + 40, 102)
    h.boss(start + 50, 631, 11, "killed", (1, 0, 999, 101, 102))
    finish(h)
    assert awarded(h)["Most Likely to Stand in Fire"]["guid"] == 102
    assert "Loot Goblin" not in awarded(h) and "Most Whispered" not in awarded(h)
    assert all(row["guid"] not in {0, 1, 999} for row in awarded(h).values())
    assert not h.rows(0) and not h.rows(1) and not h.rows(999)


def test_away_members_keep_earned_awards_and_historical_audience(h):
    start, end = bounds(h)
    for i in range(2):
        h.say(start + i, 1, "hello", channel="whisper", to=105)
    h.boss(start + 100, 631, 11, "killed", (101, 103, 105))
    h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (105,'away',?,?)",
                        (end - 1, end + 86400))
    ctx = finish(h)
    assert 105 not in ctx.members() and 105 in ctx.members(include_away=True)
    assert awarded(h)["Most Whispered"]["guid"] == 105
    assert h.store.keyed_memory(105, f"award:{SEASON}:1").text == "Won 'Most Whispered' this season."
    assert h.store.keyed_memory(105, f"award:{SEASON}:5").text.startswith("Arkon won")


def test_boss_counts_deduplicate_participants_and_exclude_nonraid_nonfight(h):
    start, _ = bounds(h)
    h.boss(start + 1, 631, 11, "wipe", (102, 102, 101, 1, 999))
    h.boss(start + 2, 631, 11, "killed", (101,))
    for i in range(5):
        h.boss(start + 10 + i, 631, 13, "killed", (103,))
        h.boss(start + 20 + i, 575, 0, "killed", (104,))
    finish(h)
    assert awarded(h)["Never Missed a Pull"]["guid"] == 101
    assert awarded(h)["Never Missed a Pull"]["value"] == 2
    assert h.store.keyed_memory(102, f"award:{SEASON}:5") is not None
    assert not h.rows(103) and not h.rows(104)


def test_season_is_half_open_and_uses_database_seconds_not_server_milliseconds(h):
    start, end = bounds(h)
    death(h, start - 0.001, 103)
    death(h, end, 103)
    death(h, end + 0.001, 103)
    h.store.insert_game_event(Death(ts=int((end + 999) * 1000), guid=102, map=631, killer_entry=0),
                              session_id=None, ts=start)
    death(h, end - 0.001, 102)
    death(h, start + 2, 101, map_id=1)
    finish(h)
    assert awarded(h)["Most Likely to Stand in Fire"]["guid"] == 102
    assert awarded(h)["Most Likely to Stand in Fire"]["value"] == 2


def test_night_owl_local_hours_dropped_lines_and_dst_fold(h, monkeypatch):
    zone = ZoneInfo("America/New_York")
    monkeypatch.setattr(h.clock, "local", lambda: datetime.fromtimestamp(h.clock.now(), zone))
    night = "2026-11-29"
    for fold in (0, 1):
        at = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=fold).timestamp()
        h.say(at, 105, "Still awake.")  # paired chat row must not count a second time
    for hour, minute, dropped in ((5, 59, None), (0, 59, None), (6, 0, None), (2, 0, "expired")):
        at = datetime(2026, 11, 1, hour, minute, tzinfo=zone).timestamp()
        h.utter(at, 105, "Another late line.", dropped=dropped)
    finish(h, night)
    row = awarded(h)["Night Owl"]
    assert (row["guid"], row["value"], row["season"]) == (105, 3, "2026-S6")


def test_human_laughs_only_and_postseason_answers_do_not_change_class_clown(h):
    start, end = bounds(h)
    h.session(start, end + 600)
    h.say(start + 10, 103, "A member-only joke.")
    h.say(start + 20, 104, "lol")
    h.say(end - 30, 101, "A joke in time.")
    h.say(end - 20, 1, "haha")
    h.say(end - 10, 102, "A joke too late.")
    h.say(end, 1, "haha")
    finish(h)
    assert awarded(h)["Class Clown"]["guid"] == 101
    assert awarded(h)["Class Clown"]["value"] == 1


def test_unrelated_private_human_laugh_is_not_a_members_reaction(h):
    start, end = bounds(h)
    h.session(start, end)
    h.say(start + 10, 101, "A whispered joke.", channel="whisper", to=1)
    h.say(start + 20, 1, "arkon lol", channel="whisper", to=102)
    finish(h)
    assert "Class Clown" not in awarded(h)


def test_replay_preserves_award_rows_and_first_person_memory(h):
    start, _ = bounds(h)
    h.say(start + 10, 1, "hello", channel="whisper", to=101)
    h.boss(start + 20, 631, 11, "killed", (101, 102))
    ctx = finish(h)
    before = (awarded(h), h.rows(101), h.rows(102), h.count("night_step"))
    # Even late-arriving historical data cannot rewrite an already awarded season.
    for i in range(3):
        h.say(start + 100 + i, 1, "late replay", channel="whisper", to=102)
    run(stage_awards(ctx))
    run(stage_awards(h.ctx(night=NIGHT)))
    assert before == (awarded(h), h.rows(101), h.rows(102), h.count("night_step"))
    assert h.store.keyed_memory(101, f"award:{SEASON}:1").text == "Won 'Most Whispered' this season."


def test_all_awards_and_copies_rollback_together_on_failure_then_retry(h, monkeypatch):
    start, _ = bounds(h)
    death(h, start + 1, 103)
    h.boss(start + 2, 631, 11, "killed", (101, 102, 103))
    ctx = h.ctx(night=NIGHT)
    original = ctx.writer.put
    writes = 0
    def fail(*args, **kwargs):
        nonlocal writes
        writes += 1
        result = original(*args, **kwargs)
        if writes == 2:
            raise RuntimeError("synthetic award copy failure")
        return result
    monkeypatch.setattr(ctx.writer, "put", fail)
    run(stage_awards(ctx))
    assert h.count("award") == h.count("memory") == h.count("night_step") == 0
    assert ctx.failures["stories"] == 1
    monkeypatch.setattr(ctx.writer, "put", original)
    run(stage_awards(ctx))
    assert h.count("award") == 2 and h.count("memory") == 6


@pytest.mark.parametrize("payload", ["not json", "[]", '{"guid":101,"map":[]}',
                                     '{"guid":"101","map":631}', '{"guid":102,"map":631}'])
def test_malformed_or_conflicting_death_payload_is_not_counted(h, payload):
    start, _ = bounds(h)
    h.store.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,'death',101,?)", (start + 1, payload))
    finish(h)
    assert "Most Likely to Stand in Fire" not in awarded(h)
