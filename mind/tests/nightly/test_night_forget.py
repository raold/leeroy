"""Weekly forgetting keeps source protection, privacy, and resumable atomic chunks."""
import json

import pytest

from conftest import run
from nightly.conftest import no_real_name
from leeroy_mind.nightly import forget, knowledge
from leeroy_mind.nightly.settings import iso_week
from leeroy_mind.nightly.store import insert_story, step_finished_at


def old(h, owner=101, text="A quiet evening of dailies.", **kwargs):
    at = h.T - 10 * 86400
    event = h.boss(at, 631, 11, "wiped", (owner,))
    return h.mem(owner, text, **{"t": at, "importance": 2,
                               "evidence": (f"event:{event}",), **kwargs})


def context(h, *replies, **kwargs):
    return h.ctx({"summary": list(replies) or [{"text": "Quiet evenings of dailies."}]}, **kwargs)


def summaries(h, owner=101):
    return [m for m in h.rows(owner) if (m.key or "").startswith("summary:")]


def chunk(h, *ids):
    return h.store.get_memories(ids)


def test_candidate_rules_and_event_order(h):
    first = old(h, t=h.T - 20 * 86400)
    second = old(h, key="slang:remembered_word")
    old(h, t=h.T - 86400)
    old(h, t=h.T - 7 * 86400)  # strict boundary
    old(h, importance=4)
    old(h, access_n=1)
    old(h, kind="plan")
    old(h, kind="story")
    for status in ("archived", "superseded", "rejected"):
        old(h, status=status)
    old(h, evidence=("story:1",))
    old(h, owner=102)
    for prefix in forget.PROTECTED_KEYS:
        old(h, key=prefix + "protected")
    assert [m.id for m in forget.candidates(h.store, 101, h.T)] == [first, second]


def test_story_evidence_protects_source_even_without_story_ref_on_memory(h):
    ident = old(h)
    insert_story(h.store, title="Remembered", summary="Quiet evenings.", participants=[101],
                 evidence=[f"memory:{ident}"], ts=h.T)
    assert forget.candidates(h.store, 101, h.T) == []


def test_age_uses_creation_time_not_old_event_time(h):
    old(h, t_created=h.T)
    assert forget.candidates(h.store, 101, h.T) == []


def test_summary_archives_originals_and_keeps_all_source_refs(h):
    ids = [old(h, actors=(102, 103), t=h.T - (10 - n) * 86400) for n in range(3)]
    ctx = context(h, {"text": "  Quiet   evenings\n of dailies. "})
    run(forget.summarize(ctx, 101, chunk(h, *ids), 0))
    made, = summaries(h)
    assert made.text == "Quiet evenings of dailies."
    assert made.evidence == tuple(f"memory:{i}" for i in ids)
    assert made.kind == "observation" and made.source == "reflection"
    assert made.importance == 3 and made.actors == (102, 103)
    assert made.t_event == h.T - 8 * 86400 and made.embedding is not None
    assert made.key == f"summary:{iso_week(h.night)}:0"
    assert {m.status for m in chunk(h, *ids)} == {"archived"}
    assert len([c for c in ctx.reasoner.llm.calls if "messages" in c]) == 1  # no think/judge
    assert h.rows(0) == h.rows(h.human) == []


@pytest.mark.parametrize("text", ["Coros joined the dailies.", "|cffffffffCoros|r joined us.",
                                  "Quillon joined us.", "We wiped 17 times.", "   "])
def test_ungrounded_summary_keeps_originals(h, text):
    ident = old(h)
    ctx = context(h, {"text": text})
    run(forget.summarize(ctx, 101, chunk(h, ident), 0))
    assert summaries(h) == [] and chunk(h, ident)[0].status == "active"
    assert ctx.done("forget", f"{iso_week(h.night)}:101:0")
    assert h.store.conn.execute("SELECT 1 FROM grounding_log WHERE verdict='rejected'").fetchone()
    assert no_real_name(h.store) == []


def test_runs_once_across_distinct_nights_in_same_week(h):
    old(h)
    first = context(h)
    run(forget.stage_forget(first))
    later = old(h)
    second = context(h, night="2026-10-30")
    run(forget.stage_forget(second))
    assert len(summaries(h)) == 1 and second.reasoner.llm.calls == []
    assert chunk(h, later)[0].status == "active"
    assert forget.count_pending(second) == 0
    next_week = context(h, night="2026-11-02")
    run(forget.stage_forget(next_week))
    assert len(summaries(h)) == 2 and chunk(h, later)[0].status == "archived"


def test_summaries_never_mix_scopes_or_partners(h):
    h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate) VALUES (?,'live',36,0,0)",
                         (h.clock.now(),))
    ids = [old(h), old(h)]
    line = h.say(h.T - 10 * 86400, 1, "Quiet dailies again.", channel="whisper", to=101)
    h.clock.set(h.T)
    ids += [old(h, evidence=(f"chat:{line}",), scope="private", scope_with=1) for _ in range(2)]
    enc = h.store.conn.execute("INSERT INTO encounter(night,idx,a,b,setting,status,scene,private) "
        "VALUES ('2026-10-19',0,101,102,'camp','done','Quiet talk.',1)").lastrowid
    ids.append(old(h, evidence=(f"encounter:{enc}",), scope="secret", scope_with=102))
    ids.append(old(h, scope="private", scope_with=104))  # explicit caller request
    ctx = context(h, *[{"text": "Quiet evenings of dailies."}] * 4)
    run(forget.stage_forget(ctx))
    made = summaries(h)
    assert {(m.scope, m.scope_with) for m in made} == {
        ("open", 0), ("private", 1), ("private", 104), ("secret", 102)}
    for row in made:
        sources = chunk(h, *(int(ref.split(":")[1]) for ref in row.evidence))
        assert {(m.scope, m.scope_with) for m in sources} == {(row.scope, row.scope_with)}
    assert {m.status for m in chunk(h, *ids)} == {"archived"}
    assert knowledge.scope_leaks(h.store) == []


def test_private_hearsay_keeps_transmitter_as_partner(h):
    line = h.say(h.T - 10 * 86400, 1, "Quiet dailies.", channel="whisper", to=101)
    h.clock.set(h.T)
    source = old(h, evidence=(f"chat:{line}",), scope="private", scope_with=1, importance=5)
    heard = old(h, owner=102, evidence=(f"memory:{source}",), scope="private", scope_with=101)
    ctx = context(h)
    run(forget.summarize(ctx, 102, chunk(h, heard), 0))
    assert [(m.scope, m.scope_with) for m in summaries(h, 102)] == [("private", 101)]


def test_chunks_at_most_forty_and_numbers_span_scope_groups(h):
    ids = [old(h) for _ in range(41)]
    ids.append(old(h, scope="private", scope_with=1))
    ctx = context(h, *[{"text": "Quiet evenings of dailies."}] * 3)
    run(forget.stage_forget(ctx))
    made = summaries(h)
    assert sorted(len(m.evidence) for m in made) == [1, 1, 40]
    assert {m.key for m in made} == {f"summary:{iso_week(h.night)}:{n}" for n in range(3)}
    assert {ref for m in made for ref in m.evidence} == {f"memory:{i}" for i in ids}


def test_count_pending_is_read_only_and_counts_exact_scope_groups(h):
    for _ in range(41):
        old(h)
    old(h, scope="secret", scope_with=102)
    old(h, owner=102)
    ctx = context(h)
    assert forget.count_pending(ctx) == 4
    assert h.store.get_meta(f"nightly:forget-plan:{iso_week(h.night)}") is None
    assert h.count("night_step") == 0 and ctx.reasoner.llm.calls == []


def test_failed_chunk_resumes_with_frozen_identity_after_siblings_archive(h, monkeypatch):
    ids = [old(h) for _ in range(81)]
    ctx = context(h, *[{"text": "Quiet evenings of dailies."}] * 3)
    put = ctx.writer.put

    def fail_middle(row, **kwargs):
        if row.key.endswith(":1"):
            raise RuntimeError("synthetic failed middle chunk")
        return put(row, **kwargs)

    monkeypatch.setattr(ctx.writer, "put", fail_middle)
    run(forget.stage_forget(ctx))
    week = iso_week(h.night)
    assert ctx.failures["forget"] == 1
    assert step_finished_at(h.store, "forget", week) is None
    assert {m.key for m in summaries(h)} == {f"summary:{week}:0", f"summary:{week}:2"}
    assert forget.count_pending(ctx) == 1
    extra = old(h)
    resumed = context(h, night="2026-10-30")
    run(forget.stage_forget(resumed))
    assert len([c for c in resumed.reasoner.llm.calls if "messages" in c]) == 1
    assert len(summaries(h)) == 3
    middle = next(m for m in summaries(h) if m.key.endswith(":1"))
    assert middle.evidence == tuple(f"memory:{i}" for i in ids[40:80])
    assert chunk(h, extra)[0].status == "active"  # frozen weekly plan
    assert step_finished_at(h.store, "forget", week) is not None


def test_summary_and_archive_rollback_together(h, monkeypatch):
    ident = old(h)
    ctx = context(h)
    put = ctx.writer.put

    def write_then_fail(row, **kwargs):
        put(row, **kwargs)
        raise RuntimeError("synthetic post-insert failure")

    monkeypatch.setattr(ctx.writer, "put", write_then_fail)
    run(forget.stage_forget(ctx))
    assert summaries(h) == [] and chunk(h, ident)[0].status == "active"
    assert ctx.failures["forget"] == 1
    assert step_finished_at(h.store, "forget", iso_week(h.night)) is None


@pytest.mark.parametrize("change", ["text", "scope", "access", "importance", "status", "story"])
def test_source_changes_during_model_call_abort_summary_and_archive(h, change):
    ident = old(h)

    def mutate(_):
        if change == "story":
            insert_story(h.store, title="Remembered", summary="Quiet evenings.", participants=[101],
                         evidence=[f"memory:{ident}"], ts=h.T)
        else:
            edits = {"text": "text='Changed memory.'", "scope": "scope='private',scope_with=1",
                     "access": "access_n=1", "importance": "importance=8", "status": "status='superseded'"}
            h.store.conn.execute(f"UPDATE memory SET {edits[change]} WHERE id=?", (ident,))
        return {"text": "Quiet evenings of dailies."}

    ctx = context(h, mutate)
    run(forget.stage_forget(ctx))
    assert summaries(h) == [] and chunk(h, ident)[0].status != "archived"
    assert ctx.failures["forget"] == 1
    assert step_finished_at(h.store, "forget", iso_week(h.night)) is None


def test_deep_legacy_open_wrapper_cannot_launder_private_lineage(h):
    line = h.say(h.T - 10 * 86400, 1, "Quiet dailies.", channel="whisper", to=101)
    h.clock.set(h.T)
    ident = old(h, evidence=(f"chat:{line}",), importance=5)
    for _ in range(6):
        ident = old(h, evidence=(f"memory:{ident}",), importance=5)
    leaf = old(h, evidence=(f"memory:{ident}",))
    ctx = context(h)
    run(forget.summarize(ctx, 101, chunk(h, leaf), 0))
    assert summaries(h) == [] and ctx.reasoner.llm.calls == []
    assert chunk(h, leaf)[0].status == "active"


def test_ancestor_privacy_changes_during_generation_abort(h):
    ancestor = old(h, importance=5)
    ident = ancestor
    for _ in range(5):
        ident = old(h, evidence=(f"memory:{ident}",), importance=5)
    leaf = old(h, evidence=(f"memory:{ident}",))

    def change(_):
        h.store.conn.execute("UPDATE memory SET scope='private',scope_with=1 WHERE id=?", (ancestor,))
        return {"text": "Quiet evenings of dailies."}

    ctx = context(h, change)
    run(forget.stage_forget(ctx))
    assert ctx.failures["forget"] == 1 and summaries(h) == []
    assert chunk(h, leaf)[0].status == "active"


def test_source_recalled_during_embedding_is_not_archived(h, monkeypatch):
    ident = old(h)
    ctx = context(h)
    embed = ctx.embed

    async def recalled(texts):
        vectors = await embed(texts)
        h.store.conn.execute("UPDATE memory SET access_n=1 WHERE id=?", (ident,))
        return vectors

    monkeypatch.setattr(ctx, "embed", recalled)
    run(forget.stage_forget(ctx))
    assert summaries(h) == [] and chunk(h, ident)[0].status == "active"
    assert ctx.failures["forget"] == 1
    assert step_finished_at(h.store, "forget", iso_week(h.night)) is None


def test_wrong_partner_in_legacy_private_wrapper_is_not_borrowed(h):
    source = old(h, scope="private", scope_with=102, importance=5)
    leaf = old(h, scope="private", scope_with=1, evidence=(f"memory:{source}",))
    ctx = context(h)
    run(forget.summarize(ctx, 101, chunk(h, leaf), 0))
    assert summaries(h) == [] and ctx.reasoner.llm.calls == []
    assert chunk(h, leaf)[0].status == "active"


@pytest.mark.parametrize("kind", ["cycle", "missing", "rejected"])
def test_incomplete_or_cyclic_ancestry_stays_untouched(h, kind):
    ancestor = old(h, importance=5)
    leaf = old(h, evidence=(f"memory:{ancestor}",))
    if kind == "cycle":
        h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?",
                             (json.dumps([f"memory:{leaf}"]), ancestor))
    elif kind == "missing":
        h.store.conn.execute("DELETE FROM memory WHERE id=?", (ancestor,))
    else:
        h.store.conn.execute("UPDATE memory SET status='rejected' WHERE id=?", (ancestor,))
    ctx = context(h)
    run(forget.stage_forget(ctx))
    assert summaries(h) == [] and ctx.reasoner.llm.calls == []
    assert chunk(h, leaf)[0].status == "active"


def test_human_and_shared_memory_never_enter_weekly_plan(h):
    old(h, owner=0)
    old(h, owner=h.human)
    ctx = context(h)
    assert forget.count_pending(ctx) == 0
    run(forget.stage_forget(ctx))
    assert ctx.reasoner.llm.calls == []
    assert all(m.status == "active" for m in h.rows(0) + h.rows(h.human))


def test_rejected_summary_completes_week_without_retrying_model(h):
    ident = old(h)
    ctx = context(h, {"text": "Coros joined the dailies."})
    run(forget.stage_forget(ctx))
    run(forget.stage_forget(ctx))
    assert len(ctx.reasoner.llm.calls) == 1
    assert chunk(h, ident)[0].status == "active"
    assert step_finished_at(h.store, "forget", iso_week(h.night)) is not None
