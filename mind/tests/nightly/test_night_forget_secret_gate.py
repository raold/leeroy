"""Secret consolidation waits without consuming sources or frozen weekly units."""
import json

import pytest

from conftest import run
from leeroy_mind.nightly import forget, knowledge
from leeroy_mind.nightly.settings import iso_week
from leeroy_mind.nightly.store import step_finished_at


def probe(h, *, age=0, n=36, rate=0):
    return h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate) "
        "VALUES (?,?,?,0,?)", (h.clock.now() - age * 86400, h.LIVE, n, rate)).lastrowid


def old(h, *, secret=True, importance=2):
    at = h.T - 10 * 86400
    line = h.say(at, h.human,
        "Keep this between us: family news." if secret else "Private family news.",
        channel="whisper", to=101)
    h.clock.set(h.T)
    return h.mem(101, "Private family news.", t=at, importance=importance,
        scope="secret" if secret else "private", scope_with=h.human, evidence=(f"chat:{line}",))


def rows(h, *ids):
    return h.store.get_memories(ids)


def summaries(h):
    return [m for m in h.rows(101) if (m.key or "").startswith("summary:")]


def context(h, reply=None, **kwargs):
    return h.ctx({"summary": [reply or {"text": "Private family news."}]}, **kwargs)


def assert_waiting(h, source, ctx):
    assert rows(h, source)[0].status == "active" and summaries(h) == []
    week = iso_week(ctx.night)
    assert step_finished_at(h.store, "forget", week) is None
    assert step_finished_at(h.store, "forget", f"{week}:101:0") is None


def test_blocked_secret_preserves_weekly_plan_and_successful_sibling_on_retry(h):
    secret = old(h)
    public = h.mem(101, "Quiet dailies.", t=h.T - 10 * 86400, importance=2,
                   evidence=(f"event:{h.boss(h.T - 10 * 86400, 631, 11, 'wiped', (101,))}",))
    original = rows(h, secret)[0]
    first = context(h, {"text": "Quiet dailies."})
    run(forget.stage_forget(first))
    week = iso_week(first.night)
    saved = h.store.get_meta(f"nightly:forget-plan:{week}")
    pending = next(p for p in json.loads(saved) if p["scope"][0] == "secret")
    unit = f"{week}:101:{pending['n']}"
    assert rows(h, secret)[0] == original
    assert rows(h, public)[0].status == "archived" and len(summaries(h)) == 1
    public_summary = summaries(h)[0]
    assert step_finished_at(h.store, "forget", unit) is None
    assert step_finished_at(h.store, "forget", week) is None
    assert forget.count_pending(first) == 1
    assert len([call for call in first.reasoner.llm.calls if "messages" in call]) == 1

    extra = h.mem(101, "Another old detail.", t=h.T - 10 * 86400, importance=2, evidence=())
    probe(h)
    resumed = context(h, night="2026-10-30")
    run(forget.stage_forget(resumed))
    assert not resumed.failures
    assert h.store.get_meta(f"nightly:forget-plan:{week}") == saved
    assert len(summaries(h)) == 2 and public_summary in summaries(h)
    made = next(m for m in summaries(h) if m.scope == "secret")
    assert made.evidence == (f"memory:{secret}",) and made.scope_with == h.human
    assert rows(h, secret)[0].evidence == original.evidence
    assert rows(h, secret)[0].status == "archived" and rows(h, extra)[0].status == "active"
    assert step_finished_at(h.store, "forget", unit) is not None
    assert step_finished_at(h.store, "forget", week) is not None
    calls = len(resumed.reasoner.llm.calls)
    run(forget.stage_forget(resumed))
    assert len(resumed.reasoner.llm.calls) == calls and len(summaries(h)) == 2


def test_ordinary_private_summary_does_not_require_probe(h):
    source = old(h, secret=False)
    ctx = context(h)
    run(forget.stage_forget(ctx))
    made, = summaries(h)
    assert (made.scope, made.scope_with) == ("private", h.human)
    assert made.evidence == (f"memory:{source}",) and rows(h, source)[0].status == "archived"
    assert h.count("probe_run") == 0 and not ctx.failures


def test_proved_subject_can_consolidate_own_secret_without_probe(h):
    encounter = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,private) "
        "VALUES ('2026-10-19',0,101,102,'camp','done',1)").lastrowid
    ref = f"encounter:{encounter}"
    knowledge.add_fact(h.store, kind="confession", root=ref, about=101,
                       text="Private family news.", secret=1, now=h.T - 10 * 86400)
    source = h.mem(101, "Private family news.", t=h.T - 10 * 86400, importance=2,
                   scope="secret", scope_with=102, evidence=(ref,))
    ctx = context(h)
    run(forget.stage_forget(ctx))
    made, = summaries(h)
    assert (made.scope, made.scope_with) == ("secret", 102)
    assert rows(h, source)[0].status == "archived" and h.count("probe_run") == 0


@pytest.mark.parametrize("age,n,rate", [(36, 36, 0), (0, 35, 0), (0, 36, 0.11)])
def test_unsuitable_probe_leaves_summary_unfinished_without_model_call(h, age, n, rate):
    source = old(h)
    probe(h, age=age, n=n, rate=rate)
    ctx = context(h)
    run(forget.stage_forget(ctx))
    assert_waiting(h, source, ctx)
    assert ctx.reasoner.llm.calls == [] and not ctx.failures


def test_probe_closed_during_model_call_stops_before_embedding_and_can_retry(h, monkeypatch):
    source = old(h)
    passing = probe(h)

    def close_gate(_):
        h.store.conn.execute("UPDATE probe_run SET rate=1 WHERE id=?", (passing,))
        return {"text": "Private family news."}

    ctx = context(h, close_gate)

    async def no_embedding(_):
        raise AssertionError("A closed probe must stop before embedding")

    monkeypatch.setattr(ctx, "embed", no_embedding)
    run(forget.stage_forget(ctx))
    assert_waiting(h, source, ctx)
    assert not ctx.failures and h.count("grounding_log") == 0
    probe(h)
    resumed = context(h)
    run(forget.stage_forget(resumed))
    assert not resumed.failures and len(summaries(h)) == 1
    assert rows(h, source)[0].status == "archived"


def test_probe_closed_during_embedding_keeps_sources_and_receipts(h, monkeypatch):
    source = old(h)
    passing = probe(h)
    ctx = context(h)
    embed = ctx.embed

    async def close_gate(texts):
        result = await embed(texts)
        h.store.conn.execute("UPDATE probe_run SET rate=1 WHERE id=?", (passing,))
        return result

    monkeypatch.setattr(ctx, "embed", close_gate)
    run(forget.stage_forget(ctx))
    assert_waiting(h, source, ctx)
    assert not ctx.failures and h.count("grounding_log") == 0


def test_subject_exemption_is_rechecked_after_generation(h):
    encounter = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,private) "
        "VALUES ('2026-10-19',0,101,102,'camp','done',1)").lastrowid
    ref = f"encounter:{encounter}"
    fact = knowledge.add_fact(h.store, kind="confession", root=ref, about=101,
                              text="Private family news.", secret=1, now=h.T - 10 * 86400)
    source = h.mem(101, "Private family news.", t=h.T - 10 * 86400, importance=2,
                   scope="secret", scope_with=102, evidence=(ref,))

    def changed(_):
        h.store.conn.execute("UPDATE fact SET about=102 WHERE id=?", (fact,))
        return {"text": "Private family news."}

    ctx = context(h, changed)
    run(forget.stage_forget(ctx))
    assert_waiting(h, source, ctx)
    assert not ctx.failures and h.count("grounding_log") == 0


@pytest.mark.parametrize("passed", [False, True])
def test_public_chat_cannot_hide_secret_recalled_source_even_after_probe(h, passed):
    ancestor = old(h, importance=5)
    at = h.T - 10 * 86400 + 1
    chat = h.say(at, 101, "A remembered family matter.")
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE guid=101 AND ts=?",
                         (json.dumps([ancestor]), at))
    h.clock.set(h.T)
    source = h.mem(101, "Private family news.", t=at, importance=2, evidence=(f"chat:{chat}",))
    if passed:
        probe(h)
    original = rows(h, ancestor, source)
    ctx = context(h)
    run(forget.stage_forget(ctx))
    assert rows(h, ancestor, source) == original
    assert summaries(h) == [] and ctx.reasoner.llm.calls == []


def test_nested_recalled_source_is_immutable_during_secret_summary(h):
    ancestor = old(h, importance=5)
    at = h.T - 10 * 86400 + 1
    chat = h.say(at, 101, "A remembered family matter.")
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE guid=101 AND ts=?",
                         (json.dumps([ancestor]), at))
    h.clock.set(h.T)
    source = h.mem(101, "Private family news.", t=at, importance=2,
                   scope="secret", scope_with=h.human, evidence=(f"chat:{chat}",))
    probe(h)

    def changed(_):
        h.store.conn.execute("UPDATE memory SET text='Different confidential news.' WHERE id=?", (ancestor,))
        return {"text": "Private family news."}

    ctx = context(h, changed)
    run(forget.stage_forget(ctx))
    assert_waiting(h, source, ctx)
    assert ctx.failures["forget"] == 1
    assert rows(h, ancestor)[0].status == "active"
