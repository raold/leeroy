"""`personas clean-notes` (owner's live DM test, 2026-11-16): relationship notes keep no sentence
built from retired everyday-word slang ("picked up 'X'", "say 'X' to me"). The sweep covers every
relationship row of every member in one run (members toward him, toward each other and his own
rows), with a receipt the run can be undone from. Invented members and notes only."""
import json

import pytest

from conftest import seed_test_cast
from leeroy_mind.__main__ import main
from leeroy_mind.personas import clean_notes as N
from leeroy_mind.storage.rows import Relationship

JUNK_PICKED = "I picked up 'still', 'only', and 'this' from him on the ferry."
JUNK_SAID = "I want the lantern night to end so I can hear him say 'yes' to me."
KEPT = "He fixed my paper kite twice."
SLANG = "I picked up 'zugzug' from them at the docks."


@pytest.mark.parametrize("note,removed", [
    (f"{JUNK_PICKED} {KEPT}", 1), (f"{KEPT} {JUNK_SAID}", 1), (f"{JUNK_PICKED} {JUNK_SAID}", 2),
    (KEPT, 0), (SLANG, 0), ("I picked up the tab at the inn.", 0),
    ("I picked up ‘grats’ from her.", 1),  # phone quotes; a chat word is everyday too
    ("She said 'no' to the raid. Fine by me.", 0),  # said, not "say 'X' to me"
])
def test_strip_takes_out_only_the_junk_sentences(note, removed):
    kept, n = N.strip(note)
    assert n == removed
    if not removed:
        assert kept == note
    else:
        assert "picked up 'still'" not in kept and "say 'yes' to me" not in kept
        assert (KEPT in kept) is (KEPT in note)


def test_a_mixed_list_counts_as_junk_when_any_word_is_everyday():
    assert N.junk("I picked up 'zugzug' and 'still' from them.")
    assert not N.junk("I picked up 'zugzug' and 'kekw' from them.")


def _rows(store):
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    rows = {(101, 1): f"{JUNK_PICKED} {KEPT}",       # a member toward him
            (102, 103): f"{KEPT} {JUNK_SAID}",       # a member toward a member
            (104, 1): JUNK_SAID,                     # emptied
            (105, 102): SLANG,                       # real slang: kept
            (1, 101): f"{JUNK_PICKED} {KEPT}"}       # his own row too
    for (a, b), notes in rows.items():
        store.put_relationship(Relationship(a=a, b=b, notes=notes))
    store.conn.commit()
    return rows


def _notes(store):
    return {(r[0], r[1]): r[2] for r in store.conn.execute("SELECT a, b, notes FROM relationship")}


def test_the_plan_sweeps_every_members_rows(store, clock):
    rows = _rows(store)
    plan = N.plan(store.conn)
    assert {(c.a, c.b) for c in plan} == {(101, 1), (102, 103), (104, 1), (1, 101)}
    assert {c.a for c in plan} == {101, 102, 104, 1}
    by = {(c.a, c.b): c for c in plan}
    assert by[(101, 1)].after == KEPT and by[(104, 1)].after == "" and by[(102, 103)].after == KEPT
    assert all(c.before == rows[(c.a, c.b)] for c in plan)


def test_apply_writes_a_receipt_and_undo_puts_back_what_it_left(store, clock):
    rows = _rows(store)
    with store.transaction():
        receipt = N.apply(store.conn, N.plan(store.conn), now=clock.now(), stamp="t1")
    assert len(receipt["rows"]) == 4
    after = _notes(store)
    assert after[(101, 1)] == KEPT and after[(105, 102)] == SLANG and after[(104, 1)] == ""
    store.put_relationship(Relationship(a=102, b=103, notes="The night wrote this since."))
    with store.transaction():
        assert N.undo(store.conn, "t1", now=clock.now()) == 3  # the row changed since stays
    back = _notes(store)
    assert back[(101, 1)] == rows[(101, 1)] and back[(1, 101)] == rows[(1, 101)]
    assert back[(102, 103)] == "The night wrote this since."
    with pytest.raises(ValueError), store.transaction():
        N.undo(store.conn, "t1", now=clock.now())
    with pytest.raises(KeyError), store.transaction():
        N.undo(store.conn, "nope", now=clock.now())
    assert json.loads(store.get_meta(N.RECEIPT + "t1"))["undone"] == clock.now()


def test_the_cli_plans_read_only_and_prints_no_text(store, tmp_path, capsys):
    rows = _rows(store)
    assert main(["personas", "clean-notes", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "would clean 4 notes (4 sentences) of 4 members, 2 toward him, 1 left empty" in out
    assert "relationship rows 5, with notes 5" in out and "nothing was written" in out
    assert "ferry" not in out and "lantern" not in out and "kite" not in out
    assert _notes(store) == rows


def test_the_cli_go_snapshots_cleans_and_undoes(store, tmp_path, capsys):
    rows = _rows(store)
    assert main(["personas", "clean-notes", "--data-dir", str(tmp_path), "--go"]) == 0
    out = capsys.readouterr().out
    assert "snapshot" in out and "cleaned 4 notes" in out
    stamp = out.rsplit("--undo ", 1)[1].split()[0]
    assert list((tmp_path / "snapshots").glob("mind-pre-clean-notes-*.db"))
    store.conn.rollback()
    assert _notes(store)[(101, 1)] == KEPT
    assert main(["personas", "clean-notes", "--data-dir", str(tmp_path), "--undo", stamp]) == 2
    assert main(["personas", "clean-notes", "--data-dir", str(tmp_path), "--undo", stamp,
                 "--go"]) == 0
    assert "put back 4 notes" in capsys.readouterr().out
    store.conn.rollback()
    assert _notes(store) == rows
