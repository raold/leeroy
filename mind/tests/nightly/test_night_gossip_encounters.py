"""Gossip joins the real encounter transaction, evidence aliases and privacy boundary."""
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.nightly import encounters as enc, gossip, knowledge
from nightly.conftest import yes
from nightly.test_night_encounters import encounter, prompt, reply, retrieved, seeded


def source(h, owner=101, *, text="Coros took my bracers.", actors=(103,), **kw):
    return h.mem(owner, text, actors=actors, importance=9, valence=-0.6,
                 evidence=(f"rule:gossip:{owner}",), **kw)


def telling(*, alias=2, teller="a", about="Coros", text="Coros took my bracers."):
    return {"teller": teller, "about": about, "source": alias, "text": text}


def hearsay(h, owner=102):
    return [m for m in h.rows(owner) if m.source == "hearsay"]


def private_source(h, owner=101, *, text="Testy got the bakery manager job.", scope="private"):
    line = h.say(h.T, h.human, text, channel="whisper", to=owner)
    root = f"chat:{line}"
    mid = h.mem(owner, text, actors=(h.human,), importance=9, evidence=(root,),
                scope=scope, scope_with=h.human if scope != "open" else 0)
    knowledge.learn(h.store, root, owner, "witnessed", ts=h.T)
    return mid, root


def seed_rumor(h, canonical, version_source=None, *, holder=101, shift=0):
    original = h.store.get_memories([canonical])[0]
    version_source = canonical if version_source is None else version_source
    held = h.store.get_memories([version_source])[0]
    root = gossip.source_root(h.store, original)
    with h.store.transaction():
        rumor = h.store.conn.execute(
            "INSERT INTO rumor(root,about,canonical,source,created,last_told) VALUES (?,?,?,?,?,?)",
            (root, original.actors[0], original.text, canonical, h.T, h.T)).lastrowid
        version = h.store.conn.execute("INSERT INTO rumor_version(rumor,teller,hearer,op,text,ts,source) "
            "VALUES (?,101,102,'sharpen',?,?,?)", (rumor, held.text, h.T, version_source)).lastrowid
        h.store.conn.execute("INSERT INTO rumor_state(rumor,guid,state,version,shift,updated) "
                            "VALUES (?,?,'spreader',?,?,?)", (rumor, holder, version, shift, h.T))
    return rumor, version


def name_rumor(h, rumor):
    with h.store.transaction():
        h.store.conn.execute("UPDATE encounter SET ref=? WHERE id=?",
                            (f"rule:rumor:{rumor}", encounter(h)["id"]))


def test_alias_resolution_and_atomic_replay(h, monkeypatch):
    for i in range(5):
        h.mem(105, f"An unrelated memory {i}.")
    mid = source(h)
    assert mid != 2
    retrieved(monkeypatch, h, mid)
    h.rel(102, 101, trust=60)
    ctx = seeded(h, reply(gossip=telling()))
    run(enc.stage_encounters(ctx))
    copy = hearsay(h)[0]
    assert copy.evidence == (f"memory:{mid}", f"encounter:{encounter(h)['id']}")
    assert copy.confidence == 0.5 and copy.scope == "open"
    assert h.store.get_relationship(102, 103).affinity == -3
    before = {table: h.count(table) for table in
              ("memory", "rumor", "rumor_version", "rumor_state", "rel_delta", "knows")}
    calls = len(ctx.reasoner.llm.calls)
    run(enc.stage_encounters(ctx))
    assert before == {table: h.count(table) for table in before}
    assert len(ctx.reasoner.llm.calls) == calls
    assert not h.rows(0)


@pytest.mark.parametrize("alias", [1, 6, 999])
def test_nonmemory_alias_and_raw_memory_id_are_not_sources(h, monkeypatch, alias):
    for i in range(5):
        h.mem(105, f"An unrelated memory {i}.")
    mid = source(h)
    assert mid == 6
    retrieved(monkeypatch, h, mid)
    ctx = seeded(h, reply(gossip=telling(alias=alias)))
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "done"
    assert not hearsay(h) and h.count("rumor") == 0
    assert h.store.conn.execute("SELECT 1 FROM grounding_log WHERE reason='gossip source not offered'").fetchone()


def test_operation_in_prompt_comes_from_hearers_prior(h, monkeypatch):
    mid = source(h)
    retrieved(monkeypatch, h, mid)
    h.rel(102, 103, affinity=-40)
    ctx = seeded(h, reply(gossip=telling()))
    run(enc.stage_encounters(ctx))
    assert "operation assimilate, bending it toward what Belra already thinks of Coros" in prompt(ctx)
    assert h.store.conn.execute("SELECT op FROM rumor_version").fetchone()[0] == "assimilate"


def test_gossip_can_be_about_an_away_guildmate(h, monkeypatch):
    mid = source(h)
    retrieved(monkeypatch, h, mid)
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (103,'away',0,9e9)")
    ctx = seeded(h, reply(gossip=telling()))
    run(enc.stage_encounters(ctx))
    assert 103 not in ctx.members()
    assert "about Coros: operation" in prompt(ctx)
    assert len(hearsay(h)) == 1 and h.count("rumor") == 1


def test_gossip_normalizes_markup_and_human_real_name_before_grounding(h, monkeypatch):
    mid = source(h, text="Testy got the job.", actors=(h.human,))
    retrieved(monkeypatch, h, mid)
    ctx = seeded(h, reply(gossip=telling(about="Quillon",
        text="|cffff0000Quillon|r   got the job.")))
    run(enc.stage_encounters(ctx))
    assert hearsay(h)[0].text == "Testy got the job."
    version = h.store.conn.execute("SELECT text FROM rumor_version").fetchone()[0]
    assert version == "Testy got the job."
    assert h.store.get_relationship(102, h.human).affinity == 0


def test_low_score_context_is_not_offered_as_gossip(h, monkeypatch):
    mid = h.mem(101, "Coros took my bracers.", actors=(103,), importance=1, confidence=1,
                evidence=("rule:low-score",))
    retrieved(monkeypatch, h, mid)
    ctx = seeded(h, reply(gossip=telling()))
    run(enc.stage_encounters(ctx))
    assert "No memory may be passed on" in prompt(ctx)
    assert not hearsay(h)


@pytest.mark.parametrize("roll,allowed", [(0.0, False), (0.99, True)])
def test_only_chosen_tellers_private_memory_enters_scene(h, monkeypatch, roll, allowed):
    mid, root = private_source(h)
    other, _ = private_source(h, 102, text="Testy bought the copper telescope.")
    retrieved(monkeypatch, h, mid, other)
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: roll)
    h.persona(103, outside_raids=["idling"])
    ctx = seeded(h, reply(gossip=telling(about="Testy", text="Testy got the bakery manager job."),
                         bystanders=[{"who": "Coros", "noticed": "the argument"}]))
    run(enc.stage_encounters(ctx))
    text = prompt(ctx)
    assert ("bakery manager" in text) == allowed
    assert "copper telescope" not in text
    if allowed:
        assert "Nobody else is around" in text and not h.rows(103)
        copy = hearsay(h)[0]
        assert (copy.scope, copy.scope_with) == ("private", 101)
        assert knowledge.knowers(h.store, root) == {101, 102}
        assert h.count("rumor") == h.count("rumor_version") == h.count("rumor_state") == 0
        assert all(m.scope != "open" for g in (101, 102) for m in h.rows(g)
                   if root in knowledge.roots(h.store, m.evidence))
    else:
        assert not hearsay(h) and knowledge.knowers(h.store, root) == {101}


def test_open_source_copy_inherits_other_private_shown_context(h, monkeypatch):
    private, _ = private_source(h)
    public = source(h)
    retrieved(monkeypatch, h, private, public)
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 0.99)
    ctx = seeded(h, reply(gossip=telling(alias=3)))
    run(enc.stage_encounters(ctx))
    copy = hearsay(h)[0]
    assert copy.evidence[0] == f"memory:{public}"
    assert (copy.scope, copy.scope_with) == ("private", 101)
    assert h.count("rumor") == 0 and "Nobody else is around" in prompt(ctx)


def test_legacy_open_row_with_private_root_keeps_every_new_memory_private(h, monkeypatch):
    mid, root = private_source(h, scope="open")
    retrieved(monkeypatch, h, mid)
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 0.99)
    ctx = seeded(h, reply(gossip=telling(about="Testy", text="Testy got the bakery manager job.")))
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "done"
    assert all(m.scope != "open" for owner in (101, 102) for m in h.rows(owner) if m.id != mid)
    assert hearsay(h)[0].scope_with == 101
    assert h.count("rumor") == 0


def test_private_copy_never_enters_a_second_hop(h, monkeypatch):
    mid, root = private_source(h)
    retrieved(monkeypatch, h, mid)
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 0.99)
    first = seeded(h, reply(gossip=telling(about="Testy", text="Testy got the bakery manager job.")))
    run(enc.stage_encounters(first))
    copied = hearsay(h)[0]
    retrieved(monkeypatch, h, copied.id)
    second_reply = reply(scene="Belra and Coros talked at the bank.", deltas=[],
        memories=[{"who": "a", "text": "Coros was at the bank.", "importance": 3, "valence": 0},
                  {"who": "b", "text": "Belra was at the bank.", "importance": 3, "valence": 0}],
        gossip=telling(about="Testy", text="Testy got the bakery manager job."))
    second = h.ctx({"encounter": [second_reply], "judge": [yes]}, night="2026-10-30")
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status) "
                            "VALUES (?,0,102,103,'the Orgrimmar bank steps','planned')", (second.night,))
    second.skip("encounters", "plan")
    run(enc.stage_encounters(second))
    assert "bakery" not in prompt(second) and "manager" not in prompt(second)
    assert 103 not in knowledge.knowers(h.store, root)
    assert not any(root in knowledge.roots(h.store, m.evidence) for m in h.rows(103))
    assert knowledge.scope_leaks(h.store) == []


def test_private_permission_changed_during_model_call_aborts(h, monkeypatch):
    mid, root = private_source(h)
    retrieved(monkeypatch, h, mid)
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 0.99)
    def revoke(_):
        with h.store.transaction():
            h.store.conn.execute("UPDATE knows SET may_tell=0 WHERE root=? AND guid=101", (root,))
        return reply(gossip=telling(about="Testy", text="Testy got the bakery manager job."))
    ctx = seeded(h, revoke)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned" and not ctx.done("encounters", "enc:0")
    assert h.count("memory") == 1 and not hearsay(h)


def test_named_private_rumor_forces_all_provenance_before_bystanders(h, monkeypatch):
    canonical, root = private_source(h)
    version_source = h.mem(101, "Testy was hired as bakery manager.", actors=(h.human,),
        importance=9, source="hearsay", confidence=0.5, evidence=(f"memory:{canonical}",),
        scope="private", scope_with=h.human)
    rumor, _ = seed_rumor(h, canonical, version_source)
    retrieved(monkeypatch, h)  # The named source must not depend on search ranking.
    monkeypatch.setattr(gossip, "discretion_roll", lambda *args: 0.99)
    h.persona(103, outside_raids=["idling"])
    ctx = seeded(h, reply(bystanders=[{"who": "Coros", "noticed": "the argument"}]), volition="gossip")
    name_rumor(h, rumor)
    run(enc.stage_encounters(ctx))
    assert "bakery manager" in prompt(ctx) and "Nobody else is around" in prompt(ctx)
    assert not h.rows(103)
    for owner in (101, 102):
        scene = [m for m in h.rows(owner) if m.source == "offscreen"][-1]
        assert {f"memory:{canonical}", f"memory:{version_source}"} <= set(scene.evidence)
        assert scene.scope != "open"


def test_named_rumor_snapshot_change_aborts_scene(h, monkeypatch):
    canonical = source(h)
    rumor, version = seed_rumor(h, canonical)
    retrieved(monkeypatch, h)
    def change(_):
        with h.store.transaction():
            h.store.conn.execute("UPDATE rumor_version SET text='Coros returned my bracers.' WHERE id=?",
                                (version,))
        return reply()
    ctx = seeded(h, change, volition="gossip")
    name_rumor(h, rumor)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned" and h.count("memory") == 1
    assert not ctx.done("encounters", "enc:0")


def test_correction_runs_without_gossip_payload_and_keeps_shown_refs(h, monkeypatch):
    canonical = source(h)
    version_source = h.mem(104, "Coros took my bracers twice.", actors=(103,), importance=8,
        evidence=(f"memory:{canonical}",))
    rumor, _ = seed_rumor(h, canonical, version_source, holder=102, shift=-3)
    old = h.mem(102, "Coros took my bracers twice.", actors=(101, 103), importance=5,
        source="hearsay", confidence=0.5, evidence=(f"memory:{canonical}",))
    h.rel(102, 103, affinity=-3)
    retrieved(monkeypatch, h)
    ctx = seeded(h, reply(), volition="correct")
    name_rumor(h, rumor)
    run(enc.stage_encounters(ctx))
    assert "Operation: correct" in prompt(ctx) and "canonical account" in prompt(ctx)
    corrected = next(m for m in hearsay(h) if m.key == f"correct:{rumor}")
    assert corrected.text == "Turns out Coros took my bracers." and corrected.confidence == 0.9
    assert h.store.get_memories([old])[0].status == "superseded"
    assert h.store.get_relationship(102, 103).affinity == -1.5
    scene = [m for m in h.rows(102) if m.source == "offscreen"][-1]
    assert {f"memory:{canonical}", f"memory:{version_source}"} <= set(scene.evidence)


def test_gossip_failure_rolls_back_scene_and_can_retry(h, monkeypatch):
    mid = source(h)
    retrieved(monkeypatch, h, mid)
    data = reply(gossip=telling())
    ctx = seeded(h, data)
    original = gossip.after_gossip
    def failed(*args, **kw):
        original(*args, **kw)
        raise RuntimeError("synthetic failure after gossip writes")
    monkeypatch.setattr(gossip, "after_gossip", failed)
    run(enc.stage_encounters(ctx))
    assert encounter(h)["status"] == "planned" and h.count("memory") == 1
    assert all(h.count(table) == 0 for table in
               ("rumor", "rumor_version", "rumor_state", "rel_delta", "knows", "emotion"))
    assert not ctx.done("encounters", "enc:0")
    monkeypatch.setattr(gossip, "after_gossip", original)
    retry = h.ctx({"encounter": [data], "judge": [yes]})
    run(enc.stage_encounters(retry))
    assert encounter(h)["status"] == "done" and len(hearsay(h)) == 1


def test_fade_runs_once_before_plan_and_resume_does_not_move_it(h, monkeypatch):
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, encounters_per_night=0)
    seen = []
    original_fade, original_plan = gossip.fade, enc.plan
    def faded(store, *, now):
        seen.append("fade")
        return original_fade(store, now=now)
    async def planned(context):
        assert context.done("encounters", "fade")
        seen.append("plan")
        return await original_plan(context)
    monkeypatch.setattr(gossip, "fade", faded)
    monkeypatch.setattr(enc, "plan", planned)
    run(enc.stage_encounters(ctx))
    h.clock.advance(15 * 86400)
    run(enc.stage_encounters(ctx))
    assert seen == ["fade", "plan", "plan"]
