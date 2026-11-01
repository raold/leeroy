"""Synthetic off-screen nights: weighted plans, atomic scenes and private evidence."""
import random
from collections import Counter
from dataclasses import replace

import pytest

from conftest import make_persona, run
from leeroy_mind.nightly import encounters as enc
from leeroy_mind.nightly import knowledge
from leeroy_mind.nightly.relations import Copresence
from leeroy_mind.nightly.storyteller import Volition, arc_key
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Member
from nightly.conftest import no_real_name, yes


def reply(**changes):
    data = {"scene": "Arkon and Belra argued about kiting at the bank.",
        "memories": [{"who": "a", "text": "Belra lectured me on kiting.", "importance": 9, "valence": -0.8},
                     {"who": "b", "text": "Arkon listened to my advice.", "importance": 4, "valence": 0.5}],
        "deltas": [{"from": "a", "dim": "affinity", "delta": -5}], "bystanders": [], "gossip": None}
    return data | changes


def seeded(h, data=None, *, setting="the Orgrimmar bank steps", volition=None, private=0,
           judge=yes, idx=0):
    ctx = h.ctx({"encounter": [reply() if data is None else data], "judge": [judge]})
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,volition,private) "
                            "VALUES (?,?,101,102,?,'planned',?,?)",
                            (ctx.night, idx, setting, volition, private))
    ctx.skip("encounters", "plan")
    return ctx


def encounter(h):
    return h.store.conn.execute("SELECT * FROM encounter ORDER BY id DESC LIMIT 1").fetchone()


def prompt(ctx):
    call = next(c for c in ctx.reasoner.llm.calls if c.get("format", {}).get("title") == "encounter")
    return "\n".join(m["content"] for m in call["messages"])


def retrieved(monkeypatch, h, *ids):
    async def get(ctx, owner, query, k):
        return [m for m in h.store.get_memories(ids) if m.owner == owner][:k]
    monkeypatch.setattr(enc, "retrieve", get)


def probe(h):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate) VALUES (?,'live',36,0,0)",
                            (h.clock.now(),))


def secret_memory(h, *, known=True, scope="private"):
    with h.store.transaction():
        ident = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,private) "
            "VALUES ('2026-10-28',9,101,102,'quiet corner','done',1)").lastrowid
        root = f"encounter:{ident}"
        knowledge.learn(h.store, root, 101, "confided", ts=h.T)
        if known:
            knowledge.learn(h.store, root, 102, "confided", ts=h.T)
    mid = h.mem(101, "Belra keeps the silver locket hidden.", scope=scope, scope_with=102,
                evidence=(root,))
    return mid, root


def test_sample_pairs_caps_and_determinism():
    weights = {(a, b): 1.0 for a in range(2, 12) for b in range(a + 1, 12)}
    p1, p2 = (enc.sample_pairs(weights, 150, 3, random.Random("2026-10-29|enc")) for _ in range(2))
    assert p1 == p2 and len(p1) <= 15
    assert max(Counter(g for pair in p1 for g in pair).values()) <= 3
    assert enc.sample_pairs(weights, 0, 3, random.Random(1)) == []
    assert enc.sample_pairs(weights, 9, 0, random.Random(1)) == []
    bad = {(1, 1): 1, (1, 2): 0, (2, 3): -1, (3, 4): float("inf"), (4, 5): float("nan")}
    assert enc.sample_pairs(bad, 5, 3, random.Random(1)) == []


def test_plan_persisted_and_excludes_away_and_human(h):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (105,'x',0,9e9)")
    ctx = h.ctx()
    first = [dict(r) for r in run(enc.plan(ctx))]
    again = [dict(r) for r in run(enc.plan(ctx))]
    assert first and first == again
    assert not any({1, 105} & {r["a"], r["b"]} for r in first)
    assert max(Counter(g for r in first for g in (r["a"], r["b"])).values()) <= 3


def test_pair_settings_and_seeded_half(h):
    h.persona(101, guild_role="officer", raid_role="healer")
    h.persona(102, guild_role="officer", raid_role="healer")
    ctx = h.ctx()
    assert enc.pair_setting(ctx, 101, 102) == enc.PAIR_SETTINGS[0]
    settings = [enc._setting(h.ctx(night=f"2026-11-{i:02}"), 101, 102,
                            random.Random(f"setting:{i}")) for i in range(1, 21)]
    assert 5 <= settings.count(enc.PAIR_SETTINGS[0]) <= 15
    h.persona(101, raid_role="healer")
    h.persona(102, raid_role="healer")
    assert enc.pair_setting(h.ctx(), 101, 102) == enc.PAIR_SETTINGS[1]
    habit = [{"substance": "tobacco", "when": "daily", "odds": 0.5, "max_state": "wired"}]
    h.persona(101, habits=habit, night_owl=True)
    h.persona(102, habits=habit)
    assert enc.pair_setting(h.ctx(), 101, 102) == enc.PAIR_SETTINGS[2]


def test_layers_and_weighted_plans_prefer_closest_five(h):
    for g in range(106, 113):
        name = f"Guildmate{g}"
        h.store.upsert_cast([Member(guid=g, name=name, race=2, class_=1, gender=0, level=80,
                                    rank=1, online=False, map=0, zone=0, spec_tab=-1)], h.T)
        save_persona(h.store, make_persona(g, name), source="seed", ts=h.T, race="Orc", class_name="Warrior")
    for g in range(102, 107):
        h.rel(101, g, affinity=70, familiarity=60)
    ctx = h.ctx()
    weights = enc.layers(ctx, 101)
    assert all(weights[g] == 3 for g in range(102, 107))
    assert all(weights[g] == 1 for g in range(107, 113))
    total, close = 0, 0
    for i in range(1, 31):
        ctx = h.ctx(night=f"2026-11-{i:02}")
        ctx.settings = replace(ctx.settings, volition_scenes=0)
        for row in run(enc.plan(ctx)):
            if 101 in (row["a"], row["b"]):
                other = row["b"] if row["a"] == 101 else row["a"]
                total += 1
                close += other in range(102, 107)
    assert total and close / total >= 0.4


def test_pair_weight_reads_today_traits(h):
    ctx = h.ctx()
    base = enc.pair_weight(ctx, 101, 102, None, shared=0, pair_setting=None)
    extra = enc.pair_weight(ctx, 101, 102, Copresence(boss_events=7), shared=9, pair_setting=True)
    assert extra == pytest.approx(base * 3.5)
    with h.store.transaction():
        h.store.conn.executemany("INSERT INTO trait_state(night,guid,trait,value) VALUES (?,?,'extraversion',1)",
                                [(h.night, 101), (h.night, 102)])
    assert enc.pair_weight(ctx, 101, 102, None, shared=0, pair_setting=None) < base


def test_plan_counts_selected_volitions_against_both_budgets(h, monkeypatch):
    from leeroy_mind.nightly import storyteller
    selected = [Volition(101, "recruit", 102, 1), Volition(103, "bond", 104, 1)]
    monkeypatch.setattr(storyteller, "volitions", lambda ctx: {})
    monkeypatch.setattr(storyteller, "choose", lambda ctx, vols: selected)
    resets = []
    monkeypatch.setattr(storyteller, "reset_want", lambda s, g, k, **kw: resets.append((g, k)))
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, encounters_per_night=1, encounters_per_member=1)
    rows = run(enc.plan(ctx))
    assert len(rows) == 1 and rows[0]["volition"] == "recruit"
    assert rows[0]["setting"] == "planning a mount run"
    assert resets == [(101, "recruit")]
    assert len(run(enc.plan(ctx))) == 1 and len(resets) == 1


def test_encounter_memories_clamped_deltas_emotions_and_replay(h):
    ctx = seeded(h)
    before = h.store.get_relationship(101, 102).affinity
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "done"
    m = h.rows(101)[0]
    assert (m.importance, m.kind, m.source, m.actors) == (6, "conversation", "offscreen", (102,))
    assert m.evidence == (f"encounter:{encounter(h)['id']}",)
    assert h.store.get_relationship(101, 102).affinity == before - 3
    emotions = h.store.conn.execute("SELECT * FROM emotion ORDER BY guid").fetchall()
    assert [(x["type"], x["target"], x["intensity"]) for x in emotions] == [("reproach", 102, 2), ("admiration", 101, 1)]
    counts = [h.count(t) for t in ("memory", "emotion", "rel_delta", "knows")]
    run(enc.stage_encounters(ctx))
    assert counts == [h.count(t) for t in ("memory", "emotion", "rel_delta", "knows")]
    assert no_real_name(h.store) == []


def test_intent_and_arc_last_beat(h):
    key = arc_key("feud", 101, 102)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO story_arc(kind,key,opened,last_beat) VALUES ('feud',?,0,0)", (key,))
    ctx = seeded(h, volition="make_up")
    run(enc.stage_encounters(ctx))
    assert "Arkon wants to patch things up with Belra." in prompt(ctx)
    assert h.store.conn.execute("SELECT last_beat FROM story_arc WHERE key=?", (key,)).fetchone()[0] == h.T


def test_bystanders_learn_and_each_reminder_keeps_own_words(h):
    h.persona(103, outside_raids=["idling"])
    h.persona(104, outside_raids=["idling"])
    ctx = seeded(h, reply(bystanders=[{"who": "Coros", "noticed": "them arguing by the bank"},
                                     {"who": h.ctx().name(104), "noticed": "Belra storming off"}]))
    run(enc.stage_encounters(ctx))
    root = f"encounter:{encounter(h)['id']}"
    for g, words in ((103, "them arguing by the bank"), (104, "Belra storming off")):
        assert g in knowledge.knowers(h.store, root)
        assert knowledge.believes_knows(h.store, 101, g, root) == "no"
        assert knowledge.believes_knows(h.store, 102, g, root) == "no"
        assert h.rows(g)[0].text == f"Noticed Arkon and Belra: {words}"
        assert h.rows(g)[0].scope == "open"
        fact = h.store.conn.execute("SELECT text FROM fact WHERE holder=? AND root=?", (g, root)).fetchone()
        assert fact[0] == words
    h.clock.advance(86400)
    run(knowledge.stage_knows(h.ctx(night="2026-10-30")))
    assert any("them arguing by the bank" in m.text for m in h.rows(103)[1:])
    assert any("Belra storming off" in m.text for m in h.rows(104)[1:])


def test_bystander_must_be_around_and_names_are_bounded(h):
    h.persona(103, outside_raids=["idling"])
    ctx = seeded(h, reply(bystanders=[{"who": "Ebbo", "noticed": "the argument"},
                                     {"who": "Coros", "noticed": "Ebbo laughed at them"}]))
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "done"
    assert not h.rows(103) and not h.rows(105)
    reasons = [r[0] for r in h.store.conn.execute("SELECT reason FROM grounding_log")]
    assert "bystander not around" in reasons
    assert any("name" in x for x in reasons)


@pytest.mark.parametrize("field", ["scene", "a", "b"])
def test_normalized_real_names_reject_entire_scene(h, field):
    data = reply()
    bad = "Qui|cffffffffllon|r watched."
    if field == "scene":
        data["scene"] = bad
    else:
        data["memories"][0 if field == "a" else 1]["text"] = bad
    ctx = seeded(h, data)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "rejected"
    assert all(h.count(t) == 0 for t in ("memory", "rel_delta", "knows", "emotion"))
    assert no_real_name(h.store) == []


def test_rejected_judge_writes_no_effects(h):
    def reject(text):
        result = yes(text)
        result["verdicts"][1]["invariants_ok"] = False
        return result
    ctx = seeded(h, judge=reject)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "rejected"
    assert all(h.count(t) == 0 for t in ("memory", "rel_delta", "knows", "emotion"))


def test_duplicate_memory_owner_rejects(h):
    data = reply()
    data["memories"][1]["who"] = "a"
    run(enc.stage_encounters(seeded(h, data)))
    assert encounter(h)["status"] == "rejected" and h.count("memory") == 0


@pytest.mark.parametrize("passed,known", [(False, True), (True, False), (True, True)])
def test_secrets_require_probe_and_known_roots(h, monkeypatch, passed, known):
    mid, root = secret_memory(h, known=known)
    if passed:
        probe(h)
    retrieved(monkeypatch, h, mid)
    h.persona(103, outside_raids=["idling"])
    ctx = seeded(h, reply(bystanders=[{"who": "Coros", "noticed": "the argument"}]))
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "done"
    permitted = passed and known
    assert ("silver locket" in prompt(ctx)) == permitted
    assert ("a secret you share: never passed on" in prompt(ctx)) == permitted
    assert ("People who might be around:" in prompt(ctx)) != permitted
    for g in (101, 102):
        m = h.rows(g)[-1]
        assert m.scope == ("secret" if permitted else "open")
        assert (f"memory:{mid}" in m.evidence) == permitted
    assert not knowledge.scope_leaks(h.store) if permitted else True
    if permitted:
        assert not h.rows(103)


@pytest.mark.parametrize("scope", ["private", "secret"])
def test_scope_only_sensitive_memory_never_becomes_shared(h, monkeypatch, scope):
    probe(h)
    mid = h.mem(101, "Belra keeps a cobalt diary.", scope=scope, scope_with=102, evidence=())
    wrapper = h.mem(101, "Belra mentioned the cobalt diary.", evidence=(f"memory:{mid}",))
    retrieved(monkeypatch, h, wrapper)
    ctx = seeded(h)
    run(enc.stage_encounters(ctx))
    assert "cobalt diary" not in prompt(ctx)
    assert h.rows(102)[0].scope == "open"


def test_nonsecret_whisper_root_omitted(h, monkeypatch):
    line = h.say(h.T, 101, "Belra asked about the copper parcel.", channel="whisper", to=102)
    mid = h.mem(101, "Belra asked about the copper parcel.", evidence=(f"chat:{line}",))
    retrieved(monkeypatch, h, mid)
    ctx = seeded(h)
    run(enc.stage_encounters(ctx))
    assert "copper parcel" not in prompt(ctx)


def test_private_scene_has_no_bystanders_and_memories_stay_secret(h):
    h.persona(103, outside_raids=["idling"])
    ctx = seeded(h, reply(bystanders=[{"who": "Coros", "noticed": "the argument"}]), private=1)
    run(enc.stage_encounters(ctx))
    assert not h.rows(103)
    assert all(h.rows(g)[0].scope == "secret" for g in (101, 102))


@pytest.mark.parametrize("volition,expected", [("confront", 1), ("make_up", 0), (None, 0)])
def test_blowup_flag_uses_clamped_deltas(h, volition, expected):
    data = reply(deltas=[{"from": "a", "dim": "affinity", "delta": -30},
                         {"from": "b", "dim": "respect", "delta": -2}])
    run(enc.stage_encounters(seeded(h, data, volition=volition)))
    assert encounter(h)["blowup"] == expected


def test_failed_commit_rolls_back_every_effect_and_can_retry(h, monkeypatch):
    ctx = seeded(h)
    original = ctx.writer.put
    calls = 0
    def fail_second(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second write failure")
        return original(*args, **kw)
    monkeypatch.setattr(ctx.writer, "put", fail_second)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned" and not ctx.done("encounters", "enc:0")
    assert all(h.count(t) == 0 for t in ("memory", "rel_delta", "knows", "emotion", "grounding_log"))
    retry = h.ctx({"encounter": [reply()], "judge": [yes]})
    run(enc.stage_encounters(retry))
    assert encounter(h)["status"] == "done" and h.count("memory") == 2


def test_scope_change_during_model_await_aborts_without_effects(h, monkeypatch):
    mid = h.mem(101, "Belra brought a brass compass.", evidence=())
    retrieved(monkeypatch, h, mid)
    def changed(text):
        with h.store.transaction():
            h.store.conn.execute("UPDATE memory SET scope='private' WHERE id=?", (mid,))
        return reply()
    ctx = seeded(h, changed)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned"
    assert h.count("memory") == 1 and h.count("knows") == 0
    assert not ctx.done("encounters", "enc:0")


def test_sensitive_change_before_retrieval_finishes_never_enters_prompt(h, monkeypatch):
    mid = h.mem(101, "Belra brought a pewter goblet.", evidence=())
    original = h.store.get_memories([mid])[0]
    async def stale(ctx, owner, query, k):
        if owner == 102:
            with h.store.transaction():
                h.store.conn.execute("UPDATE memory SET scope='secret' WHERE id=?", (mid,))
            return []
        return [original]
    monkeypatch.setattr(enc, "retrieve", stale)
    ctx = seeded(h)
    run(enc.stage_encounters(ctx))
    assert "pewter goblet" not in prompt(ctx)
    assert encounter(h)["status"] == "done" and h.rows(102)[0].scope == "open"


def test_late_ledger_failure_rolls_back_memories_deltas_emotions_and_scene(h, monkeypatch):
    h.persona(103, outside_raids=["idling"])
    ctx = seeded(h, reply(bystanders=[{"who": "Coros", "noticed": "the argument"}]))
    def failed(*args, **kw):
        raise RuntimeError("synthetic fact failure after participant writes")
    monkeypatch.setattr(knowledge, "add_fact", failed)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned"
    assert all(h.count(t) == 0 for t in ("memory", "rel_delta", "knows", "emotion", "fact", "thinks_knows"))
    assert not ctx.done("encounters", "enc:0")


def test_private_sibling_cannot_borrow_a_known_secret_roots_permission(h, monkeypatch):
    secret, _ = secret_memory(h)
    private = h.mem(101, "Belra keeps a jade ring.", scope="private", scope_with=103, evidence=())
    wrapper = h.mem(101, "Belra mentioned the jade ring.", evidence=(f"memory:{secret}", f"memory:{private}"))
    probe(h)
    retrieved(monkeypatch, h, wrapper)
    ctx = seeded(h)
    run(enc.stage_encounters(ctx))
    assert "jade ring" not in prompt(ctx)
    assert h.rows(102)[0].scope == "open"


def test_new_private_root_during_generation_aborts(h, monkeypatch):
    mid = h.mem(101, "Belra carried a golden compass.", evidence=("rule:keepsake",))
    retrieved(monkeypatch, h, mid)
    def changed(text):
        knowledge.add_fact(h.store, kind="confession", root="rule:keepsake", about=101,
                           text="a kept confidence", secret=1, now=h.T)
        return reply()
    ctx = seeded(h, changed)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned" and h.count("memory") == 1
    assert h.count("knows") == 0 and not ctx.done("encounters", "enc:0")


def test_bystander_settings_match_groups_and_are_bounded(h):
    h.persona(103, guild_role="officer")
    h.persona(104, raid_role="healer")
    habit = [{"substance": "alcohol", "when": "daily", "odds": 0.5, "max_state": "tipsy"}]
    h.persona(105, habits=habit)
    ctx = h.ctx()
    row = {"a": 101, "b": 102, "private": 0}
    for setting, expected in zip(enc.PAIR_SETTINGS, (103, 104, 105)):
        assert enc.bystander_candidates(ctx, row | {"setting": setting}, random.Random(1), shown=[]) == [expected]
    assert not enc.bystander_candidates(ctx, row | {"setting": enc.PAIR_SETTINGS[0], "private": 1},
                                       random.Random(1), shown=[])


def test_away_after_plan_does_not_render_saved_pair(h):
    ctx = seeded(h)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (102,'x',0,9e9)")
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned" and not ctx.reasoner.llm.calls
    assert not ctx.done("encounters", "enc:0")
