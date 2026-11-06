"""The move log and pair notes (conversation variety, task A2): invented lines, the test cast."""
from leeroy_mind.talk.movelog import MOVES_KEEP_S, NOTES_KEEP_S, MoveLog, PairNotes


def test_a_turn_is_stored_with_its_moves_topics_and_final_clause(store):
    log = MoveLog(store)
    log.record(guid=102, counterpart=1, channel="guild", to_guid=0,
               text="love the new table. i want you", ts=10.0)
    [r] = log.recent(102, 1, turn_channel="guild", to_guid=0, now=10.0)
    assert r.main == "declare" and "table" in r.topics and r.clause == "i want you"
    assert "declare" in r.moves and r.ts == 10.0 and r.channel == "guild"


def test_whisper_rows_reach_only_a_whisper_to_the_same_person(store):
    log = MoveLog(store)
    log.record(guid=102, counterpart=1, channel="whisper", to_guid=1, text="i want you", ts=1.0)
    assert log.recent(102, 1, turn_channel="guild", to_guid=0, now=1.0) == []
    assert log.recent(102, 1, turn_channel="whisper", to_guid=2, now=1.0) == []
    assert len(log.recent(102, 1, turn_channel="whisper", to_guid=1, now=1.0)) == 1


def test_rows_survive_a_new_movelog_and_old_ones_are_pruned(store, clock):
    now = 10 * 86400.0
    MoveLog(store).record(guid=102, counterpart=1, channel="guild", to_guid=0,
                          text="fresh news about the table", ts=now - 10)
    MoveLog(store).record(guid=102, counterpart=1, channel="guild", to_guid=0,
                          text="stale chatter about the bank", ts=now - MOVES_KEEP_S - 100)
    again = MoveLog(store)
    then = now - 200  # when both were fresh: what goes below is the prune, not the age rule
    assert len(again.recent(102, 1, turn_channel="guild", to_guid=0, now=then)) == 2
    again.record(guid=103, counterpart=1, channel="guild", to_guid=0, text="hello there", ts=now)
    rows = again.recent(102, 1, turn_channel="guild", to_guid=0, now=then)
    assert [r.ts for r in rows] == [now - 10]


def test_recent_leaves_out_rows_older_than_the_keep_without_a_prune(store):
    """A mind that runs for days prunes once, at its first write: older rows still never
    reach a prompt."""
    log = MoveLog(store)
    log.record(guid=102, counterpart=1, channel="guild", to_guid=0, text="old news", ts=0.0)
    log.record(guid=102, counterpart=1, channel="guild", to_guid=0, text="new news",
               ts=MOVES_KEEP_S)
    rows = log.recent(102, 1, turn_channel="guild", to_guid=0, now=MOVES_KEEP_S + 5)
    assert [r.ts for r in rows] == [MOVES_KEEP_S]
    assert store.conn.execute("SELECT count(*) FROM talk_move").fetchone()[0] == 2
    notes = PairNotes(store)
    notes.add(102, 1, "scene", "an old scene", channel="guild", to_guid=0, ts=0.0)
    notes.add(102, 1, "scene", "a new scene", channel="guild", to_guid=0, ts=NOTES_KEEP_S)
    assert notes.recent(102, 1, turn_channel="guild", to_guid=0,
                        now=NOTES_KEEP_S + 5) == ["a new scene"]


def test_recent_is_oldest_first_and_capped(store):
    log = MoveLog(store)
    for i in range(12):
        log.record(guid=102, counterpart=1, channel="guild", to_guid=0, text=f"line {i}", ts=float(i))
    rows = log.recent(102, 1, turn_channel="guild", to_guid=0, now=12.0)
    assert [r.ts for r in rows] == [float(i) for i in range(2, 12)]


def test_pair_notes_skip_a_repeat_and_keep_whisper_scope(store):
    notes = PairNotes(store)
    notes.add(102, 1, "scene", "we talked about the table", channel="guild", to_guid=0, ts=1.0)
    notes.add(102, 1, "scene", "we talked about the table", channel="guild", to_guid=0, ts=2.0)
    notes.add(102, 1, "scene", "a secret", channel="whisper", to_guid=1, ts=3.0)
    assert notes.recent(102, 1, turn_channel="guild", to_guid=0, now=3.0) == [
        "we talked about the table"]
    assert notes.recent(102, 1, turn_channel="whisper", to_guid=1, now=3.0) == [
        "we talked about the table", "a secret"]
    assert notes.recent(102, 1, turn_channel="whisper", to_guid=2, now=3.0) == [
        "we talked about the table"]


def test_notes_are_pruned_and_long_text_is_clipped(store):
    now = 100 * 86400.0
    PairNotes(store).add(102, 1, "scene", "old", channel="guild", to_guid=0,
                         ts=now - NOTES_KEEP_S - 5)
    notes = PairNotes(store)
    notes.add(102, 1, "scene", "y" * 200, channel="guild", to_guid=0, ts=now)
    [t] = notes.recent(102, 1, turn_channel="guild", to_guid=0, now=now)
    assert len(t) == 80
