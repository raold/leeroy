"""Mentor/feud scene integration uses final accepted rows and bounded scene changes."""
from conftest import run
from leeroy_mind.nightly import encounters as enc, feuds
from leeroy_mind.nightly.storyteller import arc_key
from nightly.test_night_confessions import answer, prepare, scene
from nightly.test_night_encounters import encounter, probe, reply, retrieved, seeded


def mentor_pair(h):
    with h.store.transaction():
        return h.store.conn.execute("INSERT INTO mentor(mentor,mentee,started,ends) VALUES (102,101,?,?)",
                                    (h.T - 86400, h.T + 28 * 86400)).lastrowid


def open_feud(h):
    with h.store.transaction():
        ident = h.store.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES (101,102,?,'an old argument')",
                                     (h.T - 86400,)).lastrowid
        h.store.conn.execute("INSERT INTO story_arc(kind,key,opened,last_beat) VALUES ('feud',?,?,?)",
                            (arc_key("feud", 101, 102), h.T - 86400, h.T - 86400))
    return ident


def test_pair_bonus_uses_active_mentor_then_open_feud(h):
    ctx = h.ctx()
    assert enc.pair_bonus(ctx, 101, 102) == 0
    feud = open_feud(h)
    assert enc.pair_bonus(ctx, 101, 102) == 1.5
    mentor = mentor_pair(h)
    assert enc.pair_bonus(ctx, 101, 102) == enc.pair_bonus(ctx, 102, 101) == 2
    with h.store.transaction():
        h.store.conn.execute("UPDATE mentor SET ends=? WHERE id=?", (h.T, mentor))
    assert enc.pair_bonus(ctx, 101, 102) == 1.5
    with h.store.transaction():
        h.store.conn.execute("UPDATE feud SET ended=? WHERE id=?", (h.T, feud))
    assert enc.pair_bonus(ctx, 101, 102) == 0


def test_relationship_settings_keep_mentor_direction_and_existing_volition(h):
    class Select:
        def random(self):
            return 0.0
    ctx = h.ctx()
    feud = open_feud(h)
    assert enc._setting(ctx, 101, 102, Select()) == "a tense moment in guild chat"
    mentor_pair(h)
    for a, b in ((101, 102), (102, 101)):
        assert enc._setting(ctx, a, b, Select()) == "a quiet run where Belra shows Arkon the ropes"
    assert enc._setting(ctx, 101, 102, Select(), "recruit") == "planning a mount run"


def test_hooks_receive_final_scene_clamped_deltas_and_all_shown_refs(h, monkeypatch):
    mid = h.mem(101, "Coros brought a copper compass.", actors=(103,), importance=9,
                evidence=("rule:copper-compass",))
    retrieved(monkeypatch, h, mid)
    calls = []
    def opening(store, ctx, row, deltas, shown):
        current = store.conn.execute("SELECT * FROM encounter WHERE id=?", (row["id"],)).fetchone()
        assert current["status"] == row["status"] == "done"
        assert current["scene"] == row["scene"] and current["private"] == row["private"] == 0
        calls.append(("open", deltas, shown))
    def ending(store, ctx, row, deltas):
        calls.append(("end", deltas, row["_shown_refs"]))
    monkeypatch.setattr(feuds, "maybe_open", opening)
    monkeypatch.setattr(feuds, "maybe_end", ending)
    data = reply(scene="Arkon and Belra argued.   ",
        deltas=[{"from": "a", "dim": "affinity", "delta": -99},
                {"from": "b", "dim": "trust", "delta": 99}])
    run(enc.stage_encounters(seeded(h, data)))
    expected = [{"from": "a", "dim": "affinity", "delta": -3},
                {"from": "b", "dim": "trust", "delta": 3}]
    assert calls == [("open", expected, (f"memory:{mid}",)),
                     ("end", expected, (f"memory:{mid}",))]


def test_negative_then_positive_accepted_scenes_open_and_end_feud(h):
    h.rel(101, 102, affinity=-40)
    h.rel(102, 101, affinity=-40)
    first = seeded(h, reply(deltas=[{"from": "a", "dim": "affinity", "delta": -99}]))
    run(enc.stage_encounters(first))
    feud = feuds.open_feuds(h.store)[0]
    assert feud["cause"] == encounter(h)["scene"][:120]
    second = scene(h, reply(deltas=[{"from": "a", "dim": "affinity", "delta": 99}]),
                   setting="the Orgrimmar bank steps", idx=1)
    run(enc.stage_encounters(second))
    assert not feuds.open_feuds(h.store)
    assert any(m.text == "Made up with Belra." for m in h.rows(101))
    assert any(m.text == "Made up with Arkon." for m in h.rows(102))


def test_confession_never_ends_existing_feud_even_with_positive_deltas(h):
    prepare(h)
    probe(h)
    ident = open_feud(h)
    ctx = scene(h, answer(h, deltas=[{"from": "a", "dim": "affinity", "delta": 3}]))
    run(enc.stage_encounters(ctx))
    assert encounter(h)["private"] == 1
    assert [r["id"] for r in feuds.open_feuds(h.store)] == [ident]
    assert not any(m.text.startswith("Made up with") for g in (101, 102) for m in h.rows(g))


def test_confession_never_opens_feud_even_with_negative_deltas(h):
    prepare(h)
    h.rel(101, 102, affinity=-40, trust=70)
    h.rel(102, 101, affinity=-40, trust=10)
    ctx = scene(h, answer(h, deltas=[{"from": "a", "dim": "affinity", "delta": -3}]))
    run(enc.stage_encounters(ctx))
    assert encounter(h)["private"] == 1 and not feuds.open_feuds(h.store)
    assert not h.store.conn.execute("SELECT 1 FROM fact WHERE kind='feud_cause'").fetchone()
