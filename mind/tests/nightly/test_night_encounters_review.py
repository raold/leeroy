"""Independent checks across the encounter, budgeting and knowledge contracts."""
from conftest import run
from leeroy_mind.nightly import budget, encounters, knowledge
from nightly.test_night_encounters import encounter, reply, retrieved, seeded


def test_budget_covers_actual_scene_and_judge_calls_even_with_bystanders(h):
    h.persona(103, outside_raids=["idling"])
    ctx = seeded(h, reply(bystanders=[{"who": "Coros", "noticed": "their argument"}]))
    run(encounters.stage_encounters(ctx))
    assert encounter(h)["status"] == "done"
    assert len(h.rows(103)) == 1
    calls = [c["format"]["title"] for c in ctx.reasoner.llm.calls if c.get("format")]
    assert calls == ["encounter", "judge"]
    assert budget.unit_calls("encounters") == len(calls)
    assert budget.estimate({"encounters": 1}, measured={}) == {
        "encounters": len(calls) * budget.PLAN_S["json"]}
    run(encounters.stage_encounters(ctx))
    assert [c["format"]["title"] for c in ctx.reasoner.llm.calls if c.get("format")] == calls


def test_later_knowledge_pass_does_not_treat_shown_prior_scene_as_witnessed(h, monkeypatch):
    with h.store.transaction():
        old = h.store.conn.execute(
            "INSERT INTO encounter(night,idx,a,b,setting,status,scene) "
            "VALUES ('2026-10-28',17,101,103,'the Orgrimmar bank steps','done',?)",
            ("Arkon and Coros discussed fishing.",)).lastrowid
        old_root = f"encounter:{old}"
        knowledge.learn(h.store, old_root, 101, "witnessed", ts=h.T - 86400)
        knowledge.learn(h.store, old_root, 103, "witnessed", ts=h.T - 86400)
    prior = h.mem(101, "Coros told me about fishing.", source="offscreen",
                  evidence=(old_root,), t=h.T - 86400)
    retrieved(monkeypatch, h, prior)
    ctx = seeded(h)
    run(encounters.stage_encounters(ctx))
    new_root = f"encounter:{encounter(h)['id']}"
    assert encounter(h)["status"] == "done"
    assert f"memory:{prior}" in h.rows(102)[0].evidence
    assert 102 in knowledge.knowers(h.store, new_root)
    assert 102 not in knowledge.knowers(h.store, old_root)
    h.clock.advance(86400)
    run(knowledge.stage_knows(h.ctx(night="2026-10-30", window=(h.T, h.T + 86400))))
    assert 102 in knowledge.knowers(h.store, new_root)
    assert 102 not in knowledge.knowers(h.store, old_root)
