"""Profile photo sets and picks in mind.db (P2 Task 8): at most one queued set a member, "4 more"
before first sets, four candidates a ready set, and an append-only pick log whose undo works only
while the photo is still what that change left. Every person here is invented."""
import sqlite3

import pytest

from leeroy_mind.selfies.store import (Candidate, Current, SelfieError, current,
                                       ensure_first_sets, mark_ready, pick, picks, queue_more,
                                       queued_sets, ready_set, undo)

from .fakes import ready


def _cands(prompt="photo of an invented test person"):
    return [Candidate(idx=i, seed=100 + i, tries=1 + i % 3, nsfw=0.01 * i, prompt=prompt)
            for i in range(4)]


def _decided(store, guid):
    return {r[0]: r[1] for r in store.conn.execute(
        "SELECT set_no, decided FROM selfie_set WHERE guid=? ORDER BY set_no", (guid,))}


def test_a_member_has_at_most_one_queued_set(store):
    first = queue_more(store, 101, now=1.0)
    assert (first.guid, first.set_no, first.reason, first.status) == (101, 1, "more", "queued")
    with pytest.raises(SelfieError, match="^four more are already queued for tonight$"):
        queue_more(store, 101, now=2.0)
    assert ensure_first_sets(store, [101], now=3.0) == 0
    with pytest.raises(sqlite3.IntegrityError):  # the database holds the rule too
        store.conn.execute("INSERT INTO selfie_set(guid,set_no,reason,status,requested) "
                           "VALUES (101,2,'first','queued',4)")
    store.conn.rollback()
    assert [(s.guid, s.set_no) for s in queued_sets(store)] == [(101, 1)]


def test_first_sets_skip_members_with_a_photo_a_queued_set_or_a_waiting_set(store, data):
    ready(store, data, 101, 1)
    pick(store, 101, 1, 0, source="console", now=2.0)  # 101 has a photo
    queue_more(store, 102, now=2.0)  # 102 has a queued set
    ready(store, data, 103, 1)  # 103 has a set waiting for his choice
    assert ensure_first_sets(store, [105, 101, 102, 103, 104, 104], now=5.0) == 2
    assert [(s.guid, s.set_no, s.reason, s.requested) for s in queued_sets(store)] == [
        (102, 1, "more", 2.0), (104, 1, "first", 5.0), (105, 1, "first", 5.0)]
    assert ensure_first_sets(store, [101, 102, 103, 104, 105], now=6.0) == 0


def test_a_member_whose_sets_were_all_decided_and_who_has_no_photo_gets_a_new_first_set(store,
                                                                                       data):
    ready(store, data, 101, 1)
    n = pick(store, 101, 1, 1, source="console", now=2.0)
    store.conn.execute("UPDATE selfie_set SET decided='passed' WHERE guid=101")  # as if passed
    store.conn.execute("DELETE FROM selfie_current WHERE pick=?", (n,))
    store.conn.commit()
    assert ensure_first_sets(store, [101], now=3.0) == 1
    assert [(s.set_no, s.reason) for s in queued_sets(store)] == [(2, "first")]


def test_more_requests_come_first(store, data):
    assert ensure_first_sets(store, [103, 101], now=1.0) == 2
    ready(store, data, 104, 1)
    more = queue_more(store, 104, now=2.0)
    assert (more.set_no, more.reason, more.requested) == (2, "more", 2.0)
    assert ready_set(store, 104, 1).decided == "passed"  # "4 more" passes the waiting set
    queue_more(store, 102, now=3.0)
    assert [(s.guid, s.set_no, s.reason) for s in queued_sets(store)] == [
        (102, 1, "more"), (104, 2, "more"), (101, 1, "first"), (103, 1, "first")]


def test_more_passes_every_waiting_set(store, data):
    ready(store, data, 101, 1)
    n = pick(store, 101, 1, 0, source="console", now=2.0)
    ready(store, data, 101, 2)  # queued as "4 more": set 1 was picked, so nothing was passed
    assert _decided(store, 101) == {1: "picked", 2: None}
    undo(store, n, source="console", now=3.0)  # set 1 waits again beside set 2
    assert _decided(store, 101) == {1: None, 2: None}
    queue_more(store, 101, now=4.0)
    assert _decided(store, 101) == {1: "passed", 2: "passed", 3: None}
    ready(store, data, 101, 3)
    pick(store, 101, 3, 1, source="console", now=5.0)
    assert _decided(store, 101) == {1: "passed", 2: "passed", 3: "picked"}


def test_after_an_undo_and_four_more_no_older_set_is_left_waiting(store, data):
    """The review's sequence: pick from set 1, "4 more", undo the pick (sets 1 and 2 both
    wait), "4 more" again, pick from set 3. Nothing may still wait for his choice."""
    ready(store, data, 101, 1)
    a = pick(store, 101, 1, 0, source="console", now=1.0)
    queue_more(store, 101, now=2.0)
    ready(store, data, 101, 2)
    undo(store, a, source="console", now=3.0)
    queue_more(store, 101, now=4.0)
    ready(store, data, 101, 3)
    c = pick(store, 101, 3, 1, source="console", now=5.0)
    assert current(store, 101) == Current(101, 3, 1, c)
    assert _decided(store, 101) == {1: "passed", 2: "passed", 3: "picked"}
    assert store.conn.execute("SELECT count(*) FROM selfie_set WHERE guid=101 AND "
                              "status='ready' AND decided IS NULL").fetchone()[0] == 0


def test_mark_ready_keeps_four_candidates_and_the_persona_version(store):
    queue_more(store, 101, now=1.0)
    assert ready_set(store, 101, 1) is None  # queued, not ready
    mark_ready(store, 101, 1, _cands(), now=9.5, night="2026-10-29", persona_version=3)
    s = ready_set(store, 101, 1)
    assert (s.guid, s.set_no, s.reason, s.status, s.requested, s.rendered, s.night,
            s.persona_version, s.decided) == (101, 1, "more", "ready", 1.0, 9.5, "2026-10-29",
                                              3, None)
    assert [tuple(r) for r in store.conn.execute(
        "SELECT idx, seed, tries, nsfw, prompt FROM selfie_candidate WHERE guid=101 AND set_no=1 "
        "ORDER BY idx")] == [(c.idx, c.seed, c.tries, c.nsfw, c.prompt) for c in _cands()]
    assert queued_sets(store) == []
    with pytest.raises(SelfieError, match="not queued"):  # never twice
        mark_ready(store, 101, 1, _cands(), now=10.0, night="2026-10-30", persona_version=4)
    assert ready_set(store, 101, 1).persona_version == 3


@pytest.mark.parametrize("cands", [
    lambda: _cands()[:3],
    lambda: _cands()[:3] + [_cands()[0]],
    lambda: [Candidate(idx=i + 1, seed=1, tries=1, nsfw=0.0, prompt="p") for i in range(4)],
])
def test_mark_ready_refuses_anything_but_one_candidate_per_slot(store, cands):
    queue_more(store, 101, now=1.0)
    with pytest.raises(SelfieError, match="four candidates"):
        mark_ready(store, 101, 1, cands(), now=2.0, night="2026-10-29", persona_version=None)
    assert [(s.guid, s.set_no) for s in queued_sets(store)] == [(101, 1)]
    assert store.conn.execute("SELECT count(*) FROM selfie_candidate").fetchone()[0] == 0


def test_a_pick_needs_a_ready_set_of_that_member(store, data):
    ready(store, data, 101, 1)
    queue_more(store, 102, now=1.0)
    for guid, set_no in ((101, 2), (102, 1), (103, 1)):
        with pytest.raises(SelfieError, match=f"^set {set_no} is not ready for this member$"):
            pick(store, guid, set_no, 0, source="console", now=2.0)
    with pytest.raises(SelfieError, match="source"):
        pick(store, 101, 1, 0, source="web", now=2.0)
    with pytest.raises(SelfieError, match="no photo 4"):
        pick(store, 101, 1, 4, source="cli", now=2.0)
    assert current(store, 101) is None and picks(store.conn) == []


def test_old_candidates_can_be_covers_without_reopening_their_queue_on_undo(store, data):
    ready(store, data, 101, 1)
    queue_more(store, 101, now=1.0)  # set 1 passed
    ready(store, data, 101, 2)
    old = pick(store, 101, 1, 0, source="console", now=2.0)
    back = undo(store, old, source="console", now=2.1)
    assert current(store, 101) is None and _decided(store, 101)[1] == "passed"
    again = undo(store, back, source="console", now=2.2)
    assert current(store, 101) == Current(101, 1, 0, again)
    undo(store, again, source="console", now=2.3)
    assert _decided(store, 101)[1] == "passed"
    pick(store, 101, 2, 0, source="console", now=3.0)
    queue_more(store, 101, now=4.0)  # set 2 holds the photo: it stays picked
    ready(store, data, 101, 3)
    c = pick(store, 101, 3, 1, source="console", now=5.0)
    old = pick(store, 101, 2, 1, source="console", now=6.0)
    back = undo(store, old, source="console", now=6.1)
    assert current(store, 101) == Current(101, 3, 1, back)
    assert _decided(store, 101)[2] == "picked"
    d = pick(store, 101, 3, 2, source="console", now=7.0)  # another of the photo's own set
    u = undo(store, d, source="console", now=8.0)
    assert current(store, 101) == Current(101, 3, 1, u)
    undo(store, c, source="console", now=9.0)  # set 2's photo again; set 3 waits
    assert _decided(store, 101) == {1: "passed", 2: "picked", 3: None}
    assert pick(store, 101, 2, 3, source="console", now=10.0) is not None  # its own set now
    assert pick(store, 101, 3, 0, source="console", now=11.0) is not None  # and a waiting one


def test_pick_undo_and_undo_of_an_undo(store, data):
    ready(store, data, 101, 1)
    a = pick(store, 101, 1, 0, source="console", now=2.0)
    assert current(store, 101) == Current(guid=101, set_no=1, idx=0, pick=a)
    assert pick(store, 101, 1, 0, source="console", now=2.5) is None  # already the photo
    ready(store, data, 101, 2)
    b = pick(store, 101, 2, 3, source="cli", now=3.0)
    assert current(store, 101) == Current(101, 2, 3, b)
    assert _decided(store, 101) == {1: "picked", 2: "picked"}

    u = undo(store, b, source="console", now=4.0)  # back to set 1's photo; set 2 waits again
    assert current(store, 101) == Current(101, 1, 0, u)
    assert _decided(store, 101) == {1: "picked", 2: None}

    r = undo(store, u, source="console", now=5.0)  # the undo undone: set 2's photo again
    assert current(store, 101) == Current(101, 2, 3, r)
    assert _decided(store, 101) == {1: "picked", 2: "picked"}

    u2 = undo(store, r, source="console", now=6.0)  # and once more, as the first undo did
    assert current(store, 101) == Current(101, 1, 0, u2)
    assert _decided(store, 101) == {1: "picked", 2: None}

    with pytest.raises(SelfieError, match="^there is no change #99$"):
        undo(store, 99, source="console", now=7.0)
    assert [tuple(r) for r in store.conn.execute(
        "SELECT id, source, undoes, before_set, before_idx, after_set, after_idx "
        "FROM selfie_pick ORDER BY id")] == [
        (a, "console", None, None, None, 1, 0), (b, "cli", None, 1, 0, 2, 3),
        (u, "console", b, 2, 3, 1, 0), (r, "console", u, 1, 0, 2, 3),
        (u2, "console", r, 2, 3, 1, 0)]


def test_undoing_the_first_pick_leaves_no_photo_and_the_set_waiting(store, data):
    ready(store, data, 101, 1)
    a = pick(store, 101, 1, 2, source="console", now=2.0)
    u = undo(store, a, source="console", now=3.0)
    assert current(store, 101) is None and _decided(store, 101) == {1: None}
    assert ensure_first_sets(store, [101], now=4.0) == 0  # the set waits for his choice
    r = undo(store, u, source="cli", now=5.0)
    assert current(store, 101) == Current(101, 1, 2, r) and _decided(store, 101) == {1: "picked"}


def test_a_pick_within_the_same_set_keeps_it_picked_when_undone(store, data):
    ready(store, data, 101, 1)
    pick(store, 101, 1, 0, source="console", now=2.0)
    b = pick(store, 101, 1, 1, source="console", now=3.0)
    u = undo(store, b, source="console", now=4.0)
    assert current(store, 101) == Current(101, 1, 0, u) and _decided(store, 101) == {1: "picked"}


def test_an_undo_is_refused_once_the_photo_has_changed_naming_the_later_changes(store, data):
    ready(store, data, 101, 1)
    ready(store, data, 102, 1)
    a = pick(store, 101, 1, 0, source="console", now=2.0)
    other = pick(store, 102, 1, 0, source="console", now=2.5)  # another member's: not named
    b = pick(store, 101, 1, 1, source="console", now=3.0)
    c = pick(store, 101, 1, 2, source="console", now=4.0)
    with pytest.raises(SelfieError, match=f"^changed since by #{b}, #{c}: undo that first$"):
        undo(store, a, source="console", now=5.0)
    assert current(store, 101) == Current(101, 1, 2, c)
    u = undo(store, c, source="console", now=6.0)
    undo(store, b, source="console", now=7.0)  # the photo is what b left again
    undo(store, a, source="console", now=8.0)
    assert current(store, 101) is None and current(store, 102) == Current(102, 1, 0, other)
    assert u > c


def test_picks_label_undos_and_say_which_can_be_undone(store, data):
    ready(store, data, 101, 1)
    ready(store, data, 102, 1)
    a = pick(store, 101, 1, 0, source="console", now=2.0)
    b = pick(store, 102, 1, 1, source="cli", now=3.0)
    c = pick(store, 101, 1, 3, source="console", now=4.0)
    u = undo(store, c, source="console", now=5.0)
    assert picks(store.conn) == [
        {"id": u, "ts": 5.0, "guid": 101, "label": f"undo #{c}", "undoable": True},
        {"id": c, "ts": 4.0, "guid": 101, "label": "pick", "undoable": False},
        {"id": b, "ts": 3.0, "guid": 102, "label": "pick", "undoable": True},
        {"id": a, "ts": 2.0, "guid": 101, "label": "pick", "undoable": True}]
    assert [p["id"] for p in picks(store.conn, 101)] == [u, c, a]
    assert [p["id"] for p in picks(store.conn, 101, limit=1)] == [u]
    assert picks(store.conn, 103) == []


def test_picks_reads_a_plain_connection(store, data, tmp_path):
    ready(store, data, 101, 1)
    a = pick(store, 101, 1, 0, source="console", now=2.0)
    plain = sqlite3.connect(f"file:{tmp_path / 'mind.db'}?mode=ro", uri=True)  # as the console
    try:
        assert picks(plain) == [{"id": a, "ts": 2.0, "guid": 101, "label": "pick",
                                 "undoable": True}]
    finally:
        plain.close()


@pytest.mark.parametrize("sql", [
    "INSERT INTO selfie_set(guid,set_no,reason,status,requested) VALUES (0,1,'first','queued',1)",
    "INSERT INTO selfie_set(guid,set_no,reason,status,requested) VALUES ('x',1,'first','queued',1)",
    "INSERT INTO selfie_set(guid,set_no,reason,status,requested) VALUES (102.5,1,'first','queued',1)",
    "INSERT INTO selfie_set(guid,set_no,reason,status,requested) VALUES (102,0,'first','queued',1)",
    "INSERT INTO selfie_set(guid,set_no,reason,status,requested) VALUES (102,1,'again','queued',1)",
    "INSERT INTO selfie_set(guid,set_no,reason,status,requested) VALUES (102,1,'first','ready',1)",
    "INSERT INTO selfie_set(guid,set_no,reason,status,requested,rendered) VALUES (102,1,'first','queued',1,2)",
    "INSERT INTO selfie_set(guid,set_no,reason,status,requested,decided) VALUES (102,1,'first','queued',1,'kept')",
    "INSERT INTO selfie_candidate(guid,set_no,idx,seed,tries,nsfw,prompt) VALUES (101,9,0,1,1,0,'p')",
    "INSERT INTO selfie_candidate(guid,set_no,idx,seed,tries,nsfw,prompt) VALUES (101,1,4,1,1,0,'p')",
    "INSERT INTO selfie_candidate(guid,set_no,idx,seed,tries,nsfw,prompt) VALUES (101,1,0,1,4,0,'p')",
    "INSERT INTO selfie_candidate(guid,set_no,idx,seed,tries,nsfw,prompt) VALUES (101,1,0,1,1,1.5,'p')",
    "INSERT INTO selfie_candidate(guid,set_no,idx,seed,tries,nsfw,prompt) VALUES (101,1,0,1,1,0,printf('%.1001c','x'))",
    "INSERT INTO selfie_pick(ts,guid,source,after_set,after_idx) VALUES (1,101,'web',1,0)",
    "INSERT INTO selfie_pick(ts,guid,source) VALUES (1,101,'console')",
    "INSERT INTO selfie_pick(ts,guid,source,before_set,after_set,after_idx) VALUES (1,101,'console',1,1,0)",
    "INSERT INTO selfie_pick(ts,guid,source,after_set) VALUES (1,101,'console',1)",
    "INSERT INTO selfie_pick(ts,guid,source,undoes) VALUES (1,101,'console',99)",
    "INSERT INTO selfie_current(guid,set_no,idx,pick) VALUES (101,1,0,99)",
    "INSERT INTO selfie_current(guid,set_no,idx,pick) VALUES (101,9,0,1)",
    "INSERT INTO selfie_current(guid,set_no,idx,pick) VALUES (101,1,4,1)",
])
def test_rows_the_tables_could_never_mean_are_refused(store, sql):
    store.conn.execute("INSERT INTO selfie_set(guid,set_no,reason,status,requested,rendered) "
                       "VALUES (101,1,'first','ready',1,2)")
    store.conn.execute("INSERT INTO selfie_pick(id,ts,guid,source,after_set,after_idx) "
                       "VALUES (1,2,101,'console',1,0)")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(sql)
    store.conn.rollback()


def test_a_lust_set_cannot_be_a_cover_once_the_lust_style_is_held(store, data):
    from leeroy_mind.selfies.store import LUST_SET_KEY, lust_set
    ready(store, data, 101, 1)
    ready(store, data, 101, 2)
    store.set_meta(LUST_SET_KEY.format(guid=101, set_no=2), "1")
    assert lust_set(store.conn, 101, 2) and not lust_set(store.conn, 101, 1)
    with pytest.raises(SelfieError, match="lust style"):
        pick(store, 101, 2, 0, source="console", now=2.0, lust_held=True)
    assert current(store, 101) is None
    assert pick(store, 101, 1, 0, source="console", now=2.0, lust_held=True) is not None
    assert pick(store, 101, 2, 0, source="console", now=3.0) is not None  # she still has the style
