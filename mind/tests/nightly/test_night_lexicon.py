"""Language spreads through actual public use, with bounded attention and public ancestry."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nightly.conftest import no_real_name
from leeroy_mind.nightly import lexicon


def term(h, word="floor inspector", *, kind="joke", owners=(101, 102), target=0,
         originator=101, now=None, ctx=None):
    at = h.T if now is None else now
    source = h.say(at - 86400, originator, word)
    h.clock.set(at)
    ctx = ctx or h.ctx()
    ident = lexicon.create_term(h.store, ctx, kind=kind, term=word, target=target,
        origin_ref=f"chat:{source}", originator=originator, adopters=owners, now=at)
    assert ident is not None
    return ident


def row(h, ident):
    return h.store.conn.execute("SELECT * FROM lexicon WHERE id=?", (ident,)).fetchone()


def holder(h, ident, guid):
    return h.store.conn.execute("SELECT * FROM lexicon_adopter WHERE term=? AND guid=?",
                                (ident, guid)).fetchone()


def memory(h, ident, guid=101):
    return h.store.keyed_memory(guid, lexicon.memory_key(row(h, ident)))


def spoken(h, text, *, guid=101, at=None, audience=(), recalls=(), **kwargs):
    ident = h.say(h.T if at is None else at, guid, text, **kwargs)
    with h.store.transaction():
        h.store.conn.execute("UPDATE chat_line SET audience=? WHERE id=?", (json.dumps(audience), ident))
        if recalls:
            h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE id=(SELECT max(id) FROM utterance)",
                                 (json.dumps(recalls),))
    return ident


@pytest.mark.parametrize("word,allowed", [("ZUG", True), ("suspicious", True),
    ("the ratioed", True), ("SUS", False), ("no cap!", False), ("vibe check", False),
    ("|cffffffffyeet|r", False)])
def test_era_whole_words(word, allowed):
    assert lexicon.era_ok(word) is allowed


def test_adoption_formula_and_prestige(h):
    assert lexicon.adopt_chance(1, 100, 10, 0) == pytest.approx(.6)
    assert lexicon.adopt_chance(.3, -100, 1, 30) == pytest.approx(.02)
    assert lexicon.adopt_chance(.3, 50, 5.5, 5) == pytest.approx(.21)
    h.persona(101, guild_role="officer")
    h.persona(102, officer_title="RAID LEAD")
    assert [lexicon.prestige(h.store, g, 1) for g in (1, 101, 102, 103)] == [1, 1, 1, .3]


def test_explicit_adopters_human_bookkeeping_and_duplicate_reuse(h):
    ctx = h.ctx()
    ident = term(h, owners=(), ctx=ctx)
    assert h.count("lexicon_adopter") == 0
    assert lexicon.adopt(h.store, ctx, ident, 1, via=101, now=h.T, use=True)
    assert holder(h, ident, 1)["uses"] == 1 and h.rows(1) == []
    assert lexicon.adopt(h.store, ctx, ident, 101, via=1, now=h.T)
    before = row(h, ident)["born"]
    same = lexicon.create_term(h.store, ctx, kind="joke", term="floor inspector",
        origin_ref=row(h, ident)["origin_ref"], originator=101, now=h.T + 10, adopters=(102,))
    assert same == ident and row(h, ident)["born"] == before
    assert memory(h, ident).text == "Running joke: floor inspector"
    assert memory(h, ident).actors == (1, 102)
    assert memory(h, ident).importance == 5 and memory(h, ident).valence == .5
    assert lexicon.source_snapshot(h.store, [f"memory:{memory(h, ident).id}"]) is not None
    assert ctx.reasoner.llm.calls == []


def test_nickname_three_holders_and_human_called_name(h):
    ident = term(h, "Frosty", kind="nickname", target=1, owners=(101,))
    assert memory(h, ident).text == "I call Testy 'Frosty'."
    ctx = h.ctx()
    for guid in (102, 103):
        assert lexicon.adopt(h.store, ctx, ident, guid, via=101, now=h.T)
    assert all(memory(h, ident, g).text == "Everyone calls Testy 'Frosty' now." for g in (101, 102, 103))
    assert no_real_name(h.store) == []


def test_legacy_nickname_upgrades_without_rebirth_and_accepts_self_target(h):
    ident = term(h, "Frosty", kind="nickname", target=101, owners=(101,))
    old = memory(h, ident)
    h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?",
                         (json.dumps([row(h, ident)["origin_ref"]]), old.id))
    assert lexicon.adopt(h.store, h.ctx(), ident, 102, via=101, now=h.T)
    assert h.count("lexicon") == 1 and memory(h, ident).id == old.id
    assert memory(h, ident).evidence[0] == f"joke:{ident}"


def test_nickname_collision_veto_and_refresh_touch_only_the_matching_term(h):
    first = term(h, "Frosty", kind="nickname", target=103, owners=(101,))
    second = term(h, "Sparky", kind="nickname", target=103, owners=(101,))
    assert holder(h, first, 101)["active"] == 0
    # A legacy inconsistent adopter row cannot let an old term overwrite the new key.
    h.store.conn.execute("UPDATE lexicon_adopter SET active=1 WHERE term=?", (first,))
    lexicon.refresh_term(h.store, h.ctx(), first, now=h.T)
    assert memory(h, second).text == "I call Coros 'Sparky'."
    assert lexicon.veto(h.store, first, now=h.T)
    assert memory(h, second).status == "active" and "Sparky" in memory(h, second).text


@pytest.mark.parametrize("word", ["quillon", "|TQuillon|tFrosty", "|Tlanguage model|tFrosty", "rizz"])
def test_unsafe_nickname_rejected_before_cleanup(h, word):
    source = h.say(h.T, 101, "Frosty")
    assert lexicon.create_term(h.store, h.ctx(), kind="nickname", term=word, target=101,
        origin_ref=f"chat:{source}", originator=101, adopters=(102,), now=h.T) is None
    assert h.count("lexicon") == 0


def test_limited_attention_archives_only_that_holders_least_used_memory(h):
    ctx = h.ctx()
    ids = [term(h, f"joke number {i}", owners=(101, 102), now=h.T + i, ctx=ctx) for i in range(8)]
    h.store.conn.execute("UPDATE lexicon_adopter SET uses=5 WHERE guid=101")
    h.store.conn.execute("UPDATE lexicon_adopter SET uses=0 WHERE guid=101 AND term=?", (ids[3],))
    new = term(h, "fresh joke", owners=(101,), now=h.T + 9, ctx=ctx)
    assert holder(h, ids[3], 101)["active"] == 0
    assert memory(h, ids[3], 101).status == "archived"
    assert memory(h, ids[3], 102).status == "active"
    assert holder(h, new, 101)["active"] == 1
    assert h.store.conn.execute("SELECT count(*) FROM lexicon_adopter WHERE guid=101 AND active=1").fetchone()[0] == 8


def test_stored_audience_conversions_use_actual_public_speaker_and_direction(h, monkeypatch):
    ident = term(h, owners=(101,))
    h.rel(102, 101, affinity=70)
    h.rel(101, 102, affinity=-80)
    spoken(h, "floor inspector", audience=(102, 103))
    ctx = h.ctx()
    calls = []
    def chance(prestige, affinity, agreeableness, years):
        calls.append(affinity)
        return 1 if affinity == 70 else 0
    monkeypatch.setattr(lexicon, "adopt_chance", chance)
    lexicon.spread(ctx)
    assert holder(h, ident, 102)["active"] == 1
    assert holder(h, ident, 102)["via"] == 101
    assert holder(h, ident, 103) is None
    assert calls == [70, 0]
    assert row(h, ident)["uses"] == 1
    assert ctx.reasoner.llm.calls == []


@pytest.mark.parametrize("bad", ["whisper", "game", "unpaired", "dropped", "private", "nested_private"])
def test_spread_rejects_unsent_or_nonpublic_provenance(h, bad):
    ident = term(h, owners=(101,))
    kwargs = {"channel": "whisper", "to": 1} if bad == "whisper" else {"origin": "game"} if bad == "game" else {}
    spoken(h, "floor inspector", **kwargs)
    if bad == "unpaired":
        h.store.conn.execute("DELETE FROM utterance WHERE ts=?", (h.T,))
    elif bad == "dropped":
        h.store.conn.execute("UPDATE utterance SET dropped='screened' WHERE ts=?", (h.T,))
    elif bad in {"private", "nested_private"}:
        secret = h.mem(101, "Private input.", scope="private", scope_with=1)
        if bad == "nested_private":
            nested = spoken(h, "Public looking history", at=h.T - 100, recalls=(secret,))
            secret = h.mem(101, "A legacy public wrapper.", evidence=(f"chat:{nested}",))
        h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE ts=?", (json.dumps([secret]), h.T))
    lexicon.spread(h.ctx())
    assert row(h, ident)["uses"] == 0


def test_actual_human_use_counts_without_a_memory_copy(h):
    ident = term(h, owners=(101,))
    spoken(h, "floor inspector", guid=1)
    lexicon.spread(h.ctx())
    assert row(h, ident)["uses"] == 1 and holder(h, ident, 1)["uses"] == 1
    assert h.rows(1) == []


def test_line_receipt_freezes_attribution_across_edit_and_overlap(h):
    first = term(h, "floor inspector", owners=(101,))
    second = term(h, "mana manager", owners=(101,))
    line = spoken(h, "floor inspector")
    ctx = h.ctx()
    lexicon.spread(ctx)
    h.store.conn.execute("UPDATE chat_line SET text='mana manager' WHERE id=?", (line,))
    h.store.conn.execute("UPDATE utterance SET text='mana manager' WHERE ts=?", (h.T,))
    lexicon.spread(ctx)
    assert [row(h, i)["uses"] for i in (first, second)] == [1, 0]
    spoken(h, "mana manager", at=h.T + 1)
    lexicon.spread(ctx)
    assert [row(h, i)["uses"] for i in (first, second)] == [1, 1]


def test_mutation_from_recalled_copy_updates_holders_without_provenance_cycle(h):
    ident = term(h)
    before = memory(h, ident).id
    line = spoken(h, "Our boots are applying for a desk job", recalls=(before,))
    spoken(h, "lol", guid=102, at=h.T + 1)
    lexicon.spread(h.ctx())
    assert row(h, ident)["term"] == "Our boots are applying for a desk job"
    assert memory(h, ident).id == before
    assert all(memory(h, ident, g).text == "Running joke: Our boots are applying for a desk job" for g in (101, 102))
    assert lexicon.source_snapshot(h.store, [f"memory:{before}"]) is not None
    assert f"chat:{line}" in dict(lexicon.source_snapshot(h.store, [f"joke:{ident}"]))
    assert f"memory:{before}" not in json.loads(h.store.get_meta(f"nightly:lexicon-sources:{ident}"))


@pytest.mark.parametrize("variant", ["|TQuillon|tOur boots need a desk job", "|Tlanguage model|tOur boots need a desk job", "Our boots are goated"])
def test_mutation_screens_raw_payload_and_era_before_rewrite(h, variant):
    ident = term(h)
    spoken(h, variant, recalls=(memory(h, ident).id,))
    spoken(h, "lol", guid=102, at=h.T + 1)
    lexicon.spread(h.ctx())
    assert row(h, ident)["term"] == "floor inspector"
    assert no_real_name(h.store) == []


def test_fade_uses_logical_nights_and_adds_current_usage_once(h):
    ident = term(h)
    h.store.conn.execute("UPDATE lexicon SET heat=10 WHERE id=?", (ident,))
    ctx = h.ctx()
    lexicon.fade(ctx)
    spoken(h, "floor inspector")
    lexicon.spread(ctx)
    lexicon.fade(ctx)
    lexicon.fade(ctx)
    assert row(h, ident)["heat"] == pytest.approx(11)
    later = h.ctx(night="2026-11-01")
    lexicon.fade(later)
    assert row(h, ident)["heat"] == pytest.approx(11 * .85 ** 3)
    lexicon.fade(later)
    assert row(h, ident)["heat"] == pytest.approx(11 * .85 ** 3)


def test_dormant_callback_restores_all_active_adopters_but_veto_never_returns(h):
    ident = term(h, now=h.T - 30 * 86400)
    h.clock.set(h.T)
    ctx = h.ctx()
    lexicon.fade(ctx)
    assert row(h, ident)["status"] == "dormant"
    assert memory(h, ident).status == "archived"
    assert lexicon.create_term(h.store, ctx, kind="joke", term="floor inspector",
        origin_ref=row(h, ident)["origin_ref"], originator=101, now=h.T) is None
    spoken(h, "floor inspector")
    lexicon.spread(ctx)
    assert row(h, ident)["status"] == "active"
    assert all(memory(h, ident, g).status == "active" for g in (101, 102))
    assert lexicon.veto(h.store, ident, now=h.T)
    assert not lexicon.veto(h.store, ident, now=h.T)
    spoken(h, "floor inspector", at=h.T + 10)
    lexicon.spread(ctx)
    assert row(h, ident)["status"] == "vetoed" and row(h, ident)["uses"] == 1
    assert memory(h, ident).status == "archived"


def test_disabled_adoption_cannot_revive_a_dormant_term(h):
    ident = term(h, now=h.T - 30 * 86400)
    h.clock.set(h.T)
    ctx = h.ctx()
    lexicon.fade(ctx)
    ctx.settings = replace(ctx.settings, adopt_max=0)
    spoken(h, "floor inspector")
    lexicon.spread(ctx)
    assert row(h, ident)["status"] == "dormant" and row(h, ident)["uses"] == 0


def test_veto_also_archives_orphan_matching_copies(h):
    ident = term(h, owners=(101, 102))
    h.store.conn.execute("DELETE FROM lexicon_adopter WHERE term=? AND guid=102", (ident,))
    assert lexicon.veto(h.store, ident, now=h.T)
    assert memory(h, ident, 102).status == "archived"


@pytest.mark.parametrize("legacy", [False, True])
def test_vetoed_high_use_holdings_never_consume_attention(h, legacy):
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, adopt_max=1)
    old = term(h, owners=(101,), ctx=ctx)
    h.store.conn.execute("UPDATE lexicon_adopter SET uses=100 WHERE term=?", (old,))
    if legacy:
        h.store.conn.execute("UPDATE lexicon SET status='vetoed' WHERE id=?", (old,))
        h.store.conn.execute("UPDATE memory SET status='archived' WHERE id=?", (memory(h, old).id,))
    else:
        assert lexicon.veto(h.store, old, now=h.T)
        assert holder(h, old, 101)["active"] == 0
    fresh = term(h, "mana manager", owners=(), ctx=ctx)
    assert lexicon.adopt(h.store, ctx, fresh, 101, via=102, now=h.T + 1)
    assert holder(h, fresh, 101)["active"] == 1
    assert holder(h, old, 101)["active"] == 0
    assert memory(h, fresh).status == "active"


def test_delayed_logical_night_usage_heats_once_then_decays_next_night(h):
    ctx = h.ctx(night="2026-11-01")
    source = h.say(ctx.window[0] - 1, 101, "floor inspector")
    spoken(h, "floor inspector", at=ctx.window[0] + 3600)
    delayed = ctx.window[1] + 3600  # Monday after 06:00, still processing Sunday.
    h.clock.set(delayed)
    ident = lexicon.create_term(h.store, ctx, kind="joke", term="floor inspector",
        origin_ref=f"chat:{source}", originator=101, adopters=(101,), now=delayed)
    assert row(h, ident)["born"] == delayed
    lexicon.spread(ctx)
    lexicon.fade(ctx)
    assert row(h, ident)["uses"] == 1 and row(h, ident)["heat"] == pytest.approx(1)
    lexicon.fade(ctx)
    assert row(h, ident)["heat"] == pytest.approx(1)
    monday = h.ctx(night="2026-11-02")
    # Duplicate creation must preserve Sunday's origin receipt.
    assert lexicon.create_term(h.store, monday, kind="joke", term="floor inspector",
        origin_ref=f"chat:{source}", originator=101, now=delayed + 1) == ident
    lexicon.fade(monday)
    assert row(h, ident)["heat"] == pytest.approx(.85)
    lexicon.fade(monday)
    assert row(h, ident)["heat"] == pytest.approx(.85)


def test_complete_origin_graph_rejects_private_missing_and_cycles(h):
    source = h.say(h.T, 101, "A public start")
    private = h.mem(101, "Private root", scope="private", scope_with=1)
    root = private
    for _ in range(5):
        root = h.mem(101, "A wrapper", evidence=(f"memory:{root}",))
    assert lexicon.source_snapshot(h.store, (ref for ref in [f"chat:{source}"])) is not None
    assert lexicon.source_snapshot(h.store, [f"memory:{root}"]) is None
    assert lexicon.source_snapshot(h.store, ["memory:999999"]) is None
    h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?", (json.dumps([f"memory:{root}"]), root))
    assert lexicon.source_snapshot(h.store, [f"memory:{root}"]) is None
    assert lexicon.create_term(h.store, h.ctx(), kind="joke", term="unsafe", originator=101,
        origin_ref=f"chat:{source}", now=h.T, refs=(f"memory:{private}",)) is None


def test_spread_failure_rolls_back_usage_memories_and_receipt(h, monkeypatch):
    ident = term(h, owners=(101,))
    line = spoken(h, "floor inspector", audience=(102,))
    ctx = h.ctx()
    monkeypatch.setattr(ctx, "rng", lambda *args: SimpleNamespace(random=lambda: 0))
    original = ctx.writer.put
    def fail_second(memory, **kwargs):
        if memory.owner == 102:
            raise RuntimeError("writer interrupted")
        return original(memory, **kwargs)
    monkeypatch.setattr(ctx.writer, "put", fail_second)
    assert not ctx.commit("language", "spread", lambda store: lexicon.spread(ctx))
    assert row(h, ident)["uses"] == 0 and holder(h, ident, 102) is None
    assert h.store.get_meta(f"nightly:lexicon-line:{line}") is None
    monkeypatch.setattr(ctx.writer, "put", original)
    assert ctx.commit("language", "spread", lambda store: lexicon.spread(ctx))
    assert row(h, ident)["uses"] == 1 and holder(h, ident, 102)["active"] == 1
