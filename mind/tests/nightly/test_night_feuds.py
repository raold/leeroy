"""Accepted scenes create bounded public feuds; their commit owns every side effect."""
from dataclasses import replace
import json

import pytest

from conftest import make_persona
from leeroy_mind.nightly import feuds, knowledge
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.wow import CLASSES, RACES


NEGATIVE = [{"from": "a", "dim": "affinity", "delta": -2}]
POSITIVE = [{"from": "b", "dim": "trust", "delta": 2}]


def scene(h, *, a=101, b=102, ctx=None, private=0, status="done", text="Arkon and Belra fell out."):
    ctx = ctx or h.ctx()
    idx = h.store.conn.execute("SELECT coalesce(max(idx),-1)+1 FROM encounter WHERE night=?",
                               (ctx.night,)).fetchone()[0]
    ident = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,scene,private) "
        "VALUES (?,?,?,?,?,?,?,?)", (ctx.night, idx, a, b, "the bank steps", status, text, private)).lastrowid
    return dict(h.store.conn.execute("SELECT * FROM encounter WHERE id=?", (ident,)).fetchone())


def hostile(h, a=101, b=102, affinity=-40):
    h.rel(a, b, affinity=affinity)
    h.rel(b, a, affinity=affinity)


def opened(h, *, ctx=None, enc=None, shown=()):
    ctx = ctx or h.ctx()
    enc = enc or scene(h, ctx=ctx)
    return feuds.maybe_open(h.store, ctx, enc, NEGATIVE, shown)


def test_feud_opens_sides_and_remembers_makeup(h):
    hostile(h)
    h.rel(103, 101, affinity=50)
    h.rel(103, 102, affinity=0)
    h.rel(104, 102, affinity=60)
    h.rel(104, 101, affinity=0)
    # Close to both is not a side.
    h.rel(105, 101, affinity=60)
    h.rel(105, 102, affinity=50)
    ctx, enc = h.ctx(), scene(h, a=102, b=101)
    ident = opened(h, ctx=ctx, enc=enc)
    feud = feuds.open_feuds(h.store)[0]
    assert (feud["id"], feud["a"], feud["b"]) == (ident, 101, 102)
    assert json.loads(feud["sides"]) == {"101": [103], "102": [104]}
    assert json.loads(feud["evidence"]) == [f"encounter:{enc['id']}"]
    fact = h.store.conn.execute("SELECT * FROM fact WHERE kind='feud_cause'").fetchone()
    assert fact["root"] == f"encounter:{enc['id']}" and fact["text"] == enc["scene"] and fact["secret"] == 0
    assert h.rows(103)[0].text == "Sided with Arkon when Arkon and Belra fell out."
    assert h.rows(104)[0].text == "Sided with Belra when Belra and Arkon fell out."
    for guid, ally, other in ((103, 101, 102), (104, 102, 101)):
        memory = h.rows(guid)[0]
        assert (memory.kind, memory.source, memory.importance, memory.valence) == ("reflection", "reflection", 5, -0.2)
        assert memory.actors == (ally, other) and memory.evidence == (f"feud:{ident}",)
        assert (memory.scope, memory.scope_with) == ("open", 0)
        assert h.store.get_relationship(guid, other).affinity == -2
        ledger = h.store.conn.execute("SELECT * FROM knows WHERE root=? AND guid=?", (f"feud:{ident}", guid)).fetchone()
        assert (ledger["how"], ledger["via"]) == ("told", ally)
    assert not h.rows(105)
    arc = h.store.conn.execute("SELECT * FROM story_arc").fetchone()
    assert arc["key"] == "feud:101:102" and arc["status"] == "open"
    h.clock.advance(10)
    makeup = scene(h)
    assert feuds.maybe_end(h.store, ctx, makeup, POSITIVE) == ident
    assert feuds.open_feuds(h.store) == []
    assert h.rows(101)[0].text == "Made up with Belra."
    assert h.rows(102)[0].text == "Made up with Arkon."
    for guid, other in ((101, 102), (102, 101)):
        memory = h.rows(guid)[0]
        assert memory.importance == 6 and memory.valence == 0.5 and memory.actors == (other,)
        assert memory.evidence == (f"feud:{ident}", f"encounter:{makeup['id']}")
    assert h.store.conn.execute("SELECT status FROM story_arc").fetchone()[0] == "resolved"
    before = h.count("memory")
    assert feuds.maybe_end(h.store, ctx, makeup, POSITIVE) is None
    assert opened(h, ctx=ctx, enc=enc) is None  # old scene cannot reopen the resolved feud
    assert h.count("memory") == before and h.count("feud") == 1
    assert not h.rows(0) and not h.rows(h.human)


@pytest.mark.parametrize("a_score,b_score,total,allowed", [
    (-30, -30, -1, True), (-29, -40, -1, False), (-40, -29, -1, False),
    (-40, -40, 0, False), (-40, -40, 1, False),
])
def test_open_requires_both_affinities_and_negative_scene(h, a_score, b_score, total, allowed):
    h.rel(101, 102, affinity=a_score)
    h.rel(102, 101, affinity=b_score)
    result = feuds.maybe_open(h.store, h.ctx(), scene(h),
                             [{"from": "a", "dim": "affinity", "delta": total}], ())
    assert (result is not None) is allowed


@pytest.mark.parametrize("a,b", [(1, 101), (101, 1), (101, 101), (0, 101), (999, 101)])
def test_neither_hook_simulates_human_self_or_unknown_pair(h, a, b):
    hostile(h, a, b)
    enc = scene(h, a=a, b=b)
    assert opened(h, enc=enc) is None
    assert feuds.maybe_end(h.store, h.ctx(), enc, POSITIVE) is None
    assert all(h.count(table) == 0 for table in ("feud", "story_arc", "fact", "memory", "knows", "rel_delta"))


def test_private_hooks_return_before_any_lookup(h):
    assert feuds.maybe_open(h.store, None, {"private": 1}, None, None) is None
    assert feuds.maybe_end(h.store, None, {"private": 1}, None) is None


def test_private_positive_confession_cannot_end_a_feud(h):
    hostile(h)
    ident = opened(h)
    private = scene(h, private=1)
    assert feuds.maybe_end(h.store, h.ctx(), private, POSITIVE) is None
    assert [row["id"] for row in feuds.open_feuds(h.store)] == [ident]
    assert h.rows(101) == h.rows(102) == []
    assert h.store.conn.execute("SELECT status FROM story_arc").fetchone()[0] == "open"


@pytest.mark.parametrize("change", ["planned", "rejected", "changed_scene", "changed_pair", "stored_private", "secret_fact"])
def test_unaccepted_or_stale_scene_does_not_open(h, change):
    hostile(h)
    enc = scene(h)
    if change in {"planned", "rejected"}:
        h.store.conn.execute("UPDATE encounter SET status=? WHERE id=?", (change, enc["id"]))
    elif change == "changed_scene":
        enc["scene"] = "A different scene."
    elif change == "changed_pair":
        enc["b"] = 104
    elif change == "stored_private":
        h.store.conn.execute("UPDATE encounter SET private=1 WHERE id=?", (enc["id"],))
    else:
        knowledge.add_fact(h.store, kind="confession", root=f"encounter:{enc['id']}",
                           about=101, text="a secret", secret=1, now=h.T)
    assert opened(h, enc=enc) is None
    assert h.count("feud") == h.count("memory") == h.count("story_arc") == 0


@pytest.mark.parametrize("kind", ["private", "secret", "legacy_nested", "missing"])
def test_nonopen_or_incomplete_shown_context_blanks_cause(h, kind):
    hostile(h)
    if kind == "missing":
        shown = ("memory:999999",)
    else:
        hidden = h.mem(101, "The copper telescope is hidden.", scope="secret" if kind == "secret" else "private", scope_with=104)
        if kind == "legacy_nested":
            hidden = h.mem(101, "A legacy open wrapper.", evidence=(f"memory:{hidden}",))
        shown = (f"memory:{hidden}",)
    ident = opened(h, shown=shown)
    assert ident is not None
    assert feuds.open_feuds(h.store)[0]["cause"] == ""
    assert h.store.conn.execute("SELECT text FROM fact WHERE kind='feud_cause'").fetchone()[0] == ""


def test_public_cause_is_screened_and_bounded(h):
    hostile(h)
    enc = scene(h, text="|cffffffffQuillon|r watched Arkon and Belra argue. " + "Again. " * 30)
    assert opened(h, enc=enc)
    cause = feuds.open_feuds(h.store)[0]["cause"]
    assert len(cause) <= 120 and "Quillon" not in cause and "|c" not in cause
    assert cause.startswith("Testy watched")


def test_side_thresholds_and_relationship_cap(h):
    hostile(h)
    h.rel(103, 101, affinity=40)
    h.rel(103, 102, affinity=14.999)
    h.rel(104, 101, affinity=39.999)
    h.rel(105, 101, affinity=40)
    h.rel(105, 102, affinity=15)
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, relationship_speed=1)
    assert opened(h, ctx=ctx)
    assert len(h.rows(103)) == 1 and not h.rows(104) and not h.rows(105)
    assert h.store.get_relationship(103, 102).affinity == pytest.approx(13.999)
    assert h.store.conn.execute("SELECT delta,reason FROM rel_delta").fetchone()[:] == (-1, "sided")


def test_at_most_three_strongest_supporters_per_side(h):
    for guid, name in ((106, "Fendrel"), (107, "Gorath"), (108, "Helwen"), (109, "Istron")):
        h.store.upsert_cast([Member(guid=guid, name=name, race=2, class_=1, gender=0, level=80,
                                   rank=3, online=False, map=0, zone=0, spec_tab=-1)], h.T)
        save_persona(h.store, make_persona(guid, name), source="seed", ts=h.T,
                     race=RACES[2], class_name=CLASSES[1])
    hostile(h)
    for guid in (103, 104, 105, 106):
        h.rel(guid, 101, affinity=40 + guid - 103)
    for guid in (107, 108, 109):
        h.rel(guid, 102, affinity=60)
    assert opened(h)
    sides = json.loads(feuds.open_feuds(h.store)[0]["sides"])
    assert sides == {"101": [106, 105, 104], "102": [107, 108, 109]}
    assert not h.rows(103) and h.count("memory") == 6


def test_away_member_does_not_take_sides(h):
    hostile(h)
    h.rel(103, 101, affinity=50)
    h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (103,'break',?,?)", (h.T - 1, h.T + 86400))
    assert opened(h)
    assert not h.rows(103) and json.loads(feuds.open_feuds(h.store)[0]["sides"])["101"] == []


def test_night_cap_survives_delayed_resume_and_resets_for_next_night(h):
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, feuds_per_night=1)
    hostile(h)
    hostile(h, 103, 104)
    assert opened(h, ctx=ctx)
    h.clock.advance(3 * 86400)
    delayed = scene(h, a=103, b=104, ctx=ctx)
    assert opened(h, ctx=ctx, enc=delayed) is None
    later = h.ctx(night="2026-11-01")
    later.settings = ctx.settings
    assert opened(h, ctx=later, enc=scene(h, a=103, b=104, ctx=later))
    assert h.count("feud") == 2


def test_reopened_pair_reuses_resolved_arc_but_new_feud_identity(h):
    hostile(h)
    ctx = h.ctx()
    first = opened(h, ctx=ctx)
    makeup = scene(h)
    assert feuds.maybe_end(h.store, ctx, makeup, POSITIVE) == first
    h.clock.advance(5)
    second = opened(h, ctx=ctx)
    assert second != first and h.count("story_arc") == 1
    arc = h.store.conn.execute("SELECT * FROM story_arc").fetchone()
    assert arc["status"] == "open" and arc["opened"] == h.clock.now()
    assert feuds.maybe_end(h.store, ctx, makeup, POSITIVE) is None
    assert [row["id"] for row in feuds.open_feuds(h.store)] == [second]


@pytest.mark.parametrize("scope,legacy", [("private", False), ("secret", False), ("secret", True)])
def test_makeup_memories_inherit_all_shown_privacy(h, scope, legacy):
    hostile(h)
    ctx = h.ctx()
    ident = opened(h, ctx=ctx)
    source = h.mem(101, "A confidence about the silver locket.", scope=scope, scope_with=102)
    if legacy:
        source = h.mem(101, "An open wrapper of that confidence.", evidence=(f"memory:{source}",))
    makeup = scene(h)
    makeup["_shown_refs"] = (f"memory:{source}",)
    assert feuds.maybe_end(h.store, ctx, makeup, POSITIVE) == ident
    for guid, other in ((101, 102), (102, 101)):
        remembered = next(row for row in h.rows(guid) if row.text.startswith("Made up with"))
        assert (remembered.scope, remembered.scope_with) == (scope, other)
        assert remembered.evidence == (f"feud:{ident}", f"encounter:{makeup['id']}")


def test_delta_sign_uses_bounded_scene_values_and_rejects_nonfinite(h):
    hostile(h)
    ctx, enc = h.ctx(), scene(h)
    # Raw -100 + 4 would be negative, but both normalize to -3 +3 == 0.
    deltas = [{"from": "a", "dim": "affinity", "delta": -100},
              {"from": "b", "dim": "trust", "delta": 4}]
    assert feuds.maybe_open(h.store, ctx, enc, deltas, ()) is None
    deltas[1]["delta"] = float("nan")
    assert feuds.maybe_open(h.store, ctx, enc, deltas, ()) is None
    deltas[1] = {"from": "b", "dim": "attraction", "delta": -3}
    assert feuds.maybe_open(h.store, ctx, enc, deltas, ()) is None
    assert h.count("feud") == 0


def test_failure_rolls_back_feud_fact_sides_and_arc(h, monkeypatch):
    hostile(h)
    h.rel(103, 101, affinity=50)
    ctx, enc = h.ctx(), scene(h)
    original = knowledge.learn
    def fail(*args, **kw):
        original(*args, **kw)
        raise RuntimeError("synthetic ledger failure")
    monkeypatch.setattr(knowledge, "learn", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        opened(h, ctx=ctx, enc=enc)
    assert all(h.count(table) == 0 for table in ("feud", "story_arc", "fact", "memory", "knows", "rel_delta", "rel_cap"))
    assert h.store.get_relationship(103, 102).affinity == 0


def test_makeup_memory_failure_keeps_feud_open(h, monkeypatch):
    hostile(h)
    ctx = h.ctx()
    ident = opened(h, ctx=ctx)
    original = ctx.writer.put
    calls = 0
    def fail_second(*args, **kw):
        nonlocal calls
        calls += 1
        result = original(*args, **kw)
        if calls == 2:
            raise RuntimeError("synthetic makeup failure")
        return result
    monkeypatch.setattr(ctx.writer, "put", fail_second)
    with pytest.raises(RuntimeError, match="synthetic"):
        feuds.maybe_end(h.store, ctx, scene(h), POSITIVE)
    assert [r["id"] for r in feuds.open_feuds(h.store)] == [ident]
    assert not h.rows(101) and not h.rows(102)
    assert h.store.conn.execute("SELECT status FROM story_arc").fetchone()[0] == "open"
