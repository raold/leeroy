"""Persona growth (P1c Task 11): self-notes that the night adds from what a member comes to see in
themselves, held to a fixed core (controller ruling B2: "A fixed core persona keeps growth from
becoming drift"), every change in the persona ledger and every ledger row undoable. Fakes only:
a FakeLLM answers for the reflection model. Every name and line here is invented."""
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from conftest import make_persona, run
from leeroy_mind.nightly.evidence import Evidence, load_items
from leeroy_mind.nightly.ground import Claim, screen_text
from leeroy_mind.nightly.growth import (BOT_Q, CORE_SCHEMA, add_note, apply_note, core_text,
                                        eligible, judge_core, rollback_week, undo)
from leeroy_mind.nightly.llm import INVARIANTS, check_schema
from leeroy_mind.nightly.store import ledger_add, ledger_rows
from leeroy_mind.storage.rows import PersonaStateRow
from nightly.conftest import IDENT

OCT26, OCT27 = (datetime(2026, 10, d, 12, tzinfo=timezone.utc).timestamp() for d in (26, 27))
ENCOUNTER = 7  # a private encounter: a 2am confession between Arkon and Belra


def make_ev(h, extra: dict | None = None):
    """Arkon's memories 20-24 and 30-32 (their ids made to match: fillers of Ebbo's take the
    others), as the items of one Evidence. 20 is dated 26 Oct, 21 and 22 27 Oct, 23 and 24 26
    Oct; 30 involves Testy; 31 reads "are u a bot lol"; 32 cites a private encounter. `extra`
    adds memories by id, {id: (text, t, kw)}, for the tests beyond the brief."""
    with h.store.transaction():
        h.store.conn.execute(
            "INSERT INTO encounter(id, night, idx, a, b, setting, status, scene, private) "
            "VALUES (?, '2026-10-26', 0, 101, 102, 'the smoke room', 'done', '', 1)", (ENCOUNTER,))
    spec = {20: ("Led the pull when nobody called it.", OCT26, {}),
            21: ("Called the wipe early and saved the repair bill.", OCT27, {}),
            22: ("Took the lead on the second pull too.", OCT27, {}),
            23: ("Marked the targets for everyone.", OCT26, {}),
            24: ("Kept the raid on schedule.", OCT26, {}),
            30: ("Testy asked me to lead the next pull.", OCT27, {"actors": (1,)}),
            31: ("are u a bot lol", OCT27, {}),
            32: ("Told Belra about my brother at 2am.", OCT27,
                 {"actors": (102,), "evidence": (f"encounter:{ENCOUNTER}",)}),
            **(extra or {})}
    for mid in range(1, max(spec) + 1):
        text, t, kw = spec.get(mid, (f"Ebbo's filler {mid}.", OCT26, {}))
        assert h.mem(101 if mid in spec else 105, text, t=t, **kw) == mid
    ev = Evidence()
    for item in load_items(h.store, [f"memory:{i}" for i in sorted(spec)], ident=IDENT):
        ev.add(item)
    return ev


def notes(h, guid=101) -> list[str]:
    return [n["text"] for n in h.store.get_state(guid).self_notes]


# -- the plan's tests ----------------------------------------------------------------------------

def test_eligibility(h):
    # items dated 26 and 27 Oct; memory:30 has actors {1}; memory:31 text "are u a bot lol"; memory:32 cites a private encounter
    ev = make_ev(h)   # test-local
    c = Claim("self_insight", 101, "I lead when nobody else will.", ["memory:20", "memory:21", "memory:22"])
    assert eligible(c, ev, owner_has_attitude=False, human=1, store=h.store)
    assert not eligible(replace(c, evidence=["memory:20", "memory:23", "memory:24"]), ev, owner_has_attitude=False, human=1, store=h.store)  # one day
    assert not eligible(replace(c, evidence=["memory:20", "memory:21", "memory:30"]), ev, owner_has_attitude=True, human=1, store=h.store)
    assert not eligible(replace(c, evidence=["memory:20", "memory:21", "memory:31"]), ev, owner_has_attitude=False, human=1, store=h.store)
    assert not eligible(replace(c, evidence=["memory:20", "memory:21", "memory:32"]), ev, owner_has_attitude=False, human=1, store=h.store)  # a confession


def test_promotion_paths_and_ledger(h):
    c = Claim("self_insight", 101, "I lead when nobody else will.", ["memory:20", "memory:21", "memory:22"])
    with h.store.transaction(): apply_note(h.store, 101, c, "extension", "", 5.0)
    assert [n["text"] for n in h.store.get_state(101).self_notes] == [c.text]
    assert [r["kind"] for r in ledger_rows(h.store, 101)] == ["note_added"]
    with h.store.transaction(): apply_note(h.store, 101, replace(c, text="I hate raiding."), "contradicts", "core says raider", 6.0)
    assert len(h.store.get_state(101).self_notes) == 1 and h.count("review_queue") == 1


def test_add_note_caps_at_five_dropping_oldest():
    st = PersonaStateRow(guid=101, self_notes=tuple({"text": f"n{i}", "evidence": [], "added": i} for i in range(5)))
    new, dropped = add_note(st, {"text": "new", "evidence": [], "added": 9})
    assert [n["text"] for n in new.self_notes] == ["n1", "n2", "n3", "n4", "new"] and dropped["text"] == "n0"


def test_undo_reverses_ledger_rows(h):
    # apply_note adds "a" -> undo(note_added) removes it and writes an undo row; a second undo of the same row returns False;
    # rollback_week removes "b" -> undo(note_rolled_back) puts "b" back; every ledger text passes screen_text
    a = Claim("self_insight", 101, "a", ["memory:1", "memory:2", "memory:3"])
    with h.store.transaction():
        apply_note(h.store, 101, a, "consistent", "", 5.0)
    (added,) = ledger_rows(h.store, 101)
    with h.store.transaction():
        assert undo(h.store, added["id"], now=6.0)
    assert notes(h) == []
    done = ledger_rows(h.store, 101)
    assert [(r["kind"], r["ref"]) for r in done] == [("undo", added["id"]), ("note_added", None)]
    with h.store.transaction():
        assert not undo(h.store, added["id"], now=7.0)
    assert len(ledger_rows(h.store, 101)) == 2
    with h.store.transaction():                                                   # the same note, added again later
        apply_note(h.store, 101, a, "consistent", "", 7.5)
        assert not undo(h.store, added["id"], now=7.6)                            # the old row stays undone
        assert undo(h.store, ledger_rows(h.store, 101)[0]["id"], now=7.7)         # its own row takes it back
    assert notes(h) == []
    with h.store.transaction():
        apply_note(h.store, 101, replace(a, text="b"), "extension", "", 8.0)
        assert rollback_week(h.store, 101, 8.0, 9.0) == 1
    assert notes(h) == []
    (rolled,) = [r for r in ledger_rows(h.store, 101) if r["kind"] == "note_rolled_back"]
    with h.store.transaction():
        assert undo(h.store, rolled["id"], now=10.0)
    assert notes(h) == ["b"]
    assert all(screen_text(r["text"], IDENT) == r["text"] for r in ledger_rows(h.store, 101))


# -- beyond the plan -----------------------------------------------------------------------------

def test_the_schemas_and_the_bot_question():
    assert CORE_SCHEMA["title"] == "core_judge"
    assert CORE_SCHEMA["properties"]["verdict"]["enum"] == ["consistent", "extension", "contradicts"]
    check_schema(CORE_SCHEMA, {"verdict": "extension", "why": "x" * 120})
    for bad in ({"verdict": "maybe", "why": ""}, {"verdict": "consistent", "why": "x" * 121}, {"verdict": "consistent"}):
        with pytest.raises(ValueError):
            check_schema(CORE_SCHEMA, bad)
    for t in ("are u a bot lol", "R U REAL", "are you npc or what", "are you a script", "r u ai"):
        assert BOT_Q.search(t), t
    for t in ("you are a bot", "are you ready", "are we real", "real talk"):
        assert not BOT_Q.search(t), t


def test_eligibility_beyond_the_brief(h):
    """Only a woman with an attitude is held back by his presence; three citations at least; a
    cited memory that is private (here, private with Testy) or that a disclosure wrote (Task 8's
    disclose: key) keeps the insight from becoming a note, and so does a cited whisper line."""
    line = h.say(OCT27, 101, "honestly i think i lead better than i admit", channel="whisper", to=1)
    ev = make_ev(h, {40: ("Testy said I was the steady one tonight.", OCT27, {"scope": "private", "scope_with": 1}),
                     41: ("Told Testy something real. He listened.", OCT27, {"key": "disclose:3"})})
    for item in load_items(h.store, [f"chat:{line}"], ident=IDENT):
        ev.add(item)
    c = Claim("self_insight", 101, "I lead when nobody else will.", ["memory:20", "memory:21", "memory:30"])
    assert eligible(c, ev, owner_has_attitude=False, human=1, store=h.store)
    assert not eligible(replace(c, evidence=["memory:20", "memory:21"]), ev, owner_has_attitude=False, human=1, store=h.store)
    assert not eligible(replace(c, evidence=["memory:20", "memory:21", "memory:99"]), ev, owner_has_attitude=False, human=1, store=h.store)
    for other in ("memory:40", "memory:41", f"chat:{line}"):
        assert not eligible(replace(c, evidence=["memory:20", "memory:22", other]), ev, owner_has_attitude=False, human=1, store=h.store), other
    # two items on one night, 21:00 and 01:00 the next morning, are one night of play
    late = datetime(2026, 10, 28, 1, tzinfo=timezone.utc).timestamp()
    eve = datetime(2026, 10, 27, 21, tzinfo=timezone.utc).timestamp()
    ev2 = Evidence()
    for ref, t in (("memory:21", eve), ("memory:22", late), ("memory:24", eve)):
        ev2.add(replace(ev[ref], t=t))
    assert not eligible(replace(c, evidence=["memory:21", "memory:22", "memory:24"]), ev2, owner_has_attitude=False, human=1, store=h.store)


def test_a_note_that_pushes_the_oldest_out_ledgers_both(h):
    for i in range(5):
        with h.store.transaction():
            apply_note(h.store, 101, Claim("self_insight", 101, f"n{i}", [f"memory:{i + 1}"]), "consistent", "", float(i))
    with h.store.transaction():
        apply_note(h.store, 101, Claim("self_insight", 101, "n5", ["memory:9"]), "extension", "", 5.0)
    assert notes(h) == ["n1", "n2", "n3", "n4", "n5"]
    newest = ledger_rows(h.store, 101)[:2]
    assert [(r["kind"], r["text"], json.loads(r["evidence"])) for r in newest] == [
        ("note_dropped", "n0", ["memory:1"]), ("note_added", "n5", ["memory:9"])]
    assert h.store.get_state(101).self_notes[-1] == {"text": "n5", "evidence": ["memory:9"], "added": 5.0}


def test_undo_puts_a_dropped_note_back_and_ledgers_what_that_pushes_out(h):
    for i in range(6):
        with h.store.transaction():
            apply_note(h.store, 101, Claim("self_insight", 101, f"n{i}", [f"memory:{i + 1}"]), "consistent", "", float(i))
    (dropped,) = [r for r in ledger_rows(h.store, 101) if r["kind"] == "note_dropped"]
    with h.store.transaction():
        assert undo(h.store, dropped["id"], now=10.0)
    assert notes(h) == ["n2", "n3", "n4", "n5", "n0"]
    back = h.store.get_state(101).self_notes[-1]
    assert back == {"text": "n0", "evidence": ["memory:1"], "added": 0.0}       # its own time, from its note_added row
    assert [(r["kind"], r["text"]) for r in ledger_rows(h.store, 101)[:2]] == [("undo", "undid note_dropped: n0"),
                                                                             ("note_dropped", "n1")]
    with h.store.transaction():
        assert not undo(h.store, dropped["id"], now=11.0)                         # already undone


def test_rollback_week_queues_each_note_and_undo_resolves_its_queue_row(h):
    for i, t in enumerate((1.0, 5.0, 6.0)):
        with h.store.transaction():
            apply_note(h.store, 101, Claim("self_insight", 101, f"n{i}", [f"memory:{i + 1}"]), "consistent", "", t)
    with h.store.transaction():
        assert rollback_week(h.store, 101, 5.0, 7.0) == 2
        assert rollback_week(h.store, 101, 5.0, 7.5) == 0
    assert notes(h) == ["n0"]
    queued = h.store.conn.execute("SELECT id, guid, kind, text, evidence, resolved FROM review_queue ORDER BY id").fetchall()
    assert [(r["guid"], r["kind"], r["text"], json.loads(r["evidence"]), r["resolved"]) for r in queued] == [
        (101, "drift_rollback", "n1", ["memory:2"], None), (101, "drift_rollback", "n2", ["memory:3"], None)]
    rolled = {r["text"]: r for r in ledger_rows(h.store, 101) if r["kind"] == "note_rolled_back"}
    assert {t: r["ref"] for t, r in rolled.items()} == {"n1": queued[0]["id"], "n2": queued[1]["id"]}
    with h.store.transaction():
        assert undo(h.store, rolled["n2"]["id"], now=8.0)
    assert notes(h) == ["n0", "n2"] and h.store.get_state(101).self_notes[-1]["added"] == 6.0
    assert [r["resolved"] for r in h.store.conn.execute("SELECT resolved FROM review_queue ORDER BY id")] == [None, 8.0]


def test_undo_of_a_note_already_gone_or_back_reverses_nothing(h):
    a = Claim("self_insight", 101, "a", ["memory:1"])
    with h.store.transaction():
        apply_note(h.store, 101, a, "consistent", "", 1.0)
        rollback_week(h.store, 101, 0.0, 2.0)
    added, rolled = ([r for r in ledger_rows(h.store, 101) if r["kind"] == k][0] for k in ("note_added", "note_rolled_back"))
    with h.store.transaction():
        assert not undo(h.store, added["id"], now=3.0)                            # rolled back already: not there
        assert undo(h.store, rolled["id"], now=4.0)
        apply_note(h.store, 101, replace(a, text="b"), "consistent", "", 5.0)
        assert rollback_week(h.store, 101, 5.0, 6.0) == 1
    (rolled_b,) = [r for r in ledger_rows(h.store, 101) if r["kind"] == "note_rolled_back" and r["text"] == "b"]
    with h.store.transaction():
        apply_note(h.store, 101, replace(a, text="b"), "consistent", "", 7.0)    # the same note came back by itself
        assert not undo(h.store, rolled_b["id"], now=8.0)
    assert notes(h) == ["a", "b"]
    assert not undo(h.store, 9999, now=9.0)                                      # no such row


def test_undo_resolves_a_drift_flag_once(h):
    """A drift flag's undo resolves its review row once, and touches nothing of another kind or
    member. What a stance_band undo does is Task 12's (its branch goes in undo), so this test
    only pins that undoing a flag leaves the band's row alone (review-task11 M2)."""
    with h.store.transaction():
        review = h.store.conn.execute("INSERT INTO review_queue(guid, kind, text, evidence, reason, created) "
                                      "VALUES (101, 'drift_line', 'lol ok', '[]', 'not him', 1.0)").lastrowid
        flag = ledger_add(h.store, guid=101, kind="drift_flag", text="lol ok", evidence=[], ref=review, now=1.0)
        band = ledger_add(h.store, guid=102, kind="stance_band", text="drawn -> lust: act flirt", evidence=["chat:3"],
                          now=1.0, score_before=45.0)
    with h.store.transaction():
        assert undo(h.store, flag, now=2.0)
        assert not undo(h.store, flag, now=3.0)
    assert h.store.conn.execute("SELECT resolved FROM review_queue WHERE id = ?", (review,)).fetchone()[0] == 2.0
    assert [(r["kind"], r["ref"]) for r in ledger_rows(h.store, 101)] == [("undo", flag), ("drift_flag", review)]
    assert [r["kind"] for r in ledger_rows(h.store, 102)] == ["stance_band"]       # the band's row untouched
    with h.store.transaction():                                                   # resolved on the dashboard first
        seen = h.store.conn.execute("INSERT INTO review_queue(guid, kind, text, evidence, reason, created, resolved) "
                                    "VALUES (101, 'drift_line', 'eh', '[]', 'not him', 1.0, 1.5)").lastrowid
        flag2 = ledger_add(h.store, guid=101, kind="drift_flag", text="eh", evidence=[], ref=seen, now=1.0)
        assert not undo(h.store, flag2, now=5.0)
    assert h.store.conn.execute("SELECT resolved FROM review_queue WHERE id = ?", (seen,)).fetchone()[0] == 1.5
    (u,) = [r for r in ledger_rows(h.store) if r["kind"] == "undo"]
    assert (u["guid"], u["ref"], u["text"]) == (101, flag, "undid drift_flag: lol ok")
    with h.store.transaction():
        assert not undo(h.store, u["id"], now=4.0)                                # an undo is not undone
    assert band not in [r["ref"] for r in ledger_rows(h.store) if r["kind"] == "undo"]


def test_core_text_is_the_fixed_core():
    """The persona less what is not who the member is: lines, bits, the secret, the interview,
    typing, looks, the disclosure layers and her tastes (Task 7's carry, row 56)."""
    p = make_persona(101, "Arkon", maximal=True)
    d = json.loads(core_text(p))
    for key in ("sample_lines", "bits", "secret", "mock_interview", "typing", "appearance", "disclosures"):
        assert key not in d, key
    assert "drawn_to" not in d["romance"] and "turned_off_by" not in d["romance"] and d["romance"]["stance"] == "none"
    assert {"identity", "big_five", "values", "nevers", "opinions", "game", "play", "real_life"} <= d.keys()
    assert core_text(p) == json.dumps(d)


def test_judge_core_asks_with_the_core_and_never_his_real_name(h):
    p = h.persona(101, values=["standing up for the new raiders"])
    ctx = h.ctx({"core_judge": [{"verdict": "contradicts", "why": "core says raider"}]})
    assert run(judge_core(ctx, 101, "I would rather lead than raid with Testy.")) == ("contradicts", "core says raider")
    (call,) = [c for c in ctx.reasoner.llm.calls if "messages" in c]
    system, user = call["messages"][0]["content"], call["messages"][-1]["content"]
    assert call["format"] == CORE_SCHEMA and core_text(p) in user and "I would rather lead than raid with Testy." in user
    assert "Arkon" in system and system.endswith(INVARIANTS.format(human="Testy"))
    assert "quillon" not in (system + user).casefold()
