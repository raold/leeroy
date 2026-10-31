import asyncio
import json

import pytest

from conftest import make_persona, run, seed_test_cast
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.scheduler import Expired, LLMScheduler
from leeroy_mind.memory.extract import (
    EXTRACTION_SCHEMA, Extractor, build_extraction_messages, validate_extraction)
from leeroy_mind.memory.threads import ClosedThread
from leeroy_mind.memory.writer import Embedder, MemoryWriter
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Chat, Member
from leeroy_mind.testing.fake_llm import FakeLLM, script_from

HUMAN = HumanIdentity(1, "Testmage", "Testy", "Zorblat", True)
NAMES = {1: "Testy", 101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna"}
SPEC = [(101, "Arkon", "i will bring flasks tonight"), (1, "Testmage", "great, thanks"),
        (102, "Belra", "ill make the food")]


def add_human(store):
    store.upsert_cast([Member(guid=1, name="Testmage", race=1, class_=8, gender=0, level=83,
                              rank=0, online=True, map=0, zone=0, spec_tab=-1)], 1000.0)


def mk_lines(store, spec, channel="guild"):
    ids = []
    for frm, name, text in spec:
        ev = Chat(ts=0, channel=channel, from_guid=frm, from_name=name, to_guid=0, text=text,
                  map=0, zone=0)
        ids.append(store.insert_chat_line(ev, session_id=None, origin="game"))
    return ids, store.chat_lines(ids)


def mem(speaker="Arkon", text="I told Belra I will bring flasks.", ev=("L1",), imp=5,
        what="promise", **kw):
    return {"speaker": speaker, "text": text, "importance": imp, "valence": 0.2, "about": [],
            "evidence": list(ev), "what": what, "disagrees": False, **kw}


def val(store, memories, overheard=None, human=HUMAN):
    ids, lines = mk_lines(store, SPEC)
    assert ids[0] == 1
    return validate_extraction({"memories": memories, "overheard": overheard}, lines, NAMES, 1,
                               human=human)


def test_schema_shape():
    item = EXTRACTION_SCHEMA["properties"]["memories"]["items"]
    assert item["properties"]["nudges"]["items"]["properties"]["dim"]["enum"]
    assert item["properties"]["text"]["maxLength"] == 200


def test_validate_drops_inventions(store):
    out = val(store, [
        mem("Nobody"),                                  # unknown speaker
        mem(ev=("L999",)),                              # evidence not in the thread
        mem(ev=()),                                     # no evidence
        mem(text="I first."), mem(text="I second."), mem(text="I third.")])
    assert [m.text for m in out.memories] == ["I first.", "I second."]
    assert all(m.owner == 101 and m.evidence == ("chat:1",) for m in out.memories)
    assert all(m.kind == "conversation" and m.source == "extract" for m in out.memories)


def test_validate_rejects_reply_without_memories_list(store):
    ids, lines = mk_lines(store, SPEC)
    with pytest.raises(ValueError):
        validate_extraction({"overheard": None}, lines, NAMES, 1, human=HUMAN)


def test_human_raises_importance_floor(store):
    """With him in it a memory is worth 5 at least; without him, under 4 it is small talk
    between members and is not kept (fix wave G2)."""
    out = val(store, [mem(ev=("L2",), imp=2, text="Testy thanked me."),
                      mem("Belra", "I cook.", ev=("L3",), imp=2),
                      mem("Belra", "I offered to cook.", ev=("L3",), imp=4)])
    assert [m.importance for m in out.memories] == [5, 4]


def test_human_among_actors_raises_importance_floor(store):
    """Named in a member's own memory, he raises only what passed the small-talk floor on the
    model's own rating (fix wave G3, rehearsal 3 check 8)."""
    out = val(store, [mem(imp=2, about=["Testy"], text="I owe Testy."),
                      mem(imp=4, about=["Testy"], text="I owe Testy a flask.")])
    assert [m.text for m in out.memories] == ["I owe Testy a flask."]
    assert out.memories[0].importance == 5 and out.memories[0].actors == (101, 1)


def test_human_gets_no_memories(store):
    out = val(store, [mem("Testy", "I like flasks."), mem("Testmage", "I like x y z")])
    assert out.memories == []


def test_real_name_hits_are_dropped_not_rewritten(store):
    out = val(store, [mem(text="Zorblat said hi to me."), mem(text="ZORBLAT said hi to me."),
                      mem(text="Testy said hi to me.")],
              {"text": "Zorblat is here.", "importance": 6})
    assert [m.text for m in out.memories] == ["Testy said hi to me."]
    assert out.overheard is None


def test_the_lowercase_word_is_kept_in_memories_and_overheard(store):
    """The drop rule uses redaction's matcher: the name's configured, Title and UPPER forms
    go; the ordinary lower-case word the name may also be stays."""
    bill = HumanIdentity(1, "Testmage", "Testy", "Bill", True)
    out = val(store, [mem(text="I paid the bill for the raid."), mem(text="Bill paid me back."),
                      mem(speaker="Belra", text="BILL owes me gold.", ev=("L3",))],
              {"text": "Arkon paid the bill.", "importance": 6}, human=bill)
    assert [m.text for m in out.memories] == ["I paid the bill for the raid."]
    assert out.overheard == ("Arkon paid the bill.", 6)


def test_the_names_title_form_in_overheard_is_dropped(store):
    bill = HumanIdentity(1, "Testmage", "Testy", "Bill", True)
    out = val(store, [mem(text="I like flasks.")], {"text": "Bill is here.", "importance": 6},
              human=bill)
    assert out.overheard is None


def test_member_romance_is_dropped(store):
    out = val(store, [mem(text="I have a crush on Belra."),
                      mem(text="I flirt with Testy whenever I can.", ev=("L2",))],
              {"text": "Arkon is dating Belra.", "importance": 7})
    assert [m.text for m in out.memories] == ["I flirt with Testy whenever I can."]
    assert out.overheard is None


def test_human_is_required(store):
    ids, lines = mk_lines(store, SPEC)
    with pytest.raises(TypeError):
        validate_extraction({"memories": []}, lines, NAMES, 1)
    with pytest.raises(TypeError):
        build_extraction_messages(lines, NAMES, 1)
    with pytest.raises(TypeError):
        Extractor(store, None, None, "m", human_guid=lambda: 1, bystanders=lambda t, s: [])


def test_romance_about_absent_member_is_dropped(store):
    out = val(store, [mem(text="I have a crush on Dunna."),
                      mem(text="I have a crush on her.", about=["Belra"]),
                      mem(text="I have a crush on her.", about=["Dunna"]),
                      mem(text="I teamed up with Dunna on the flasks.", about=["Dunna"])])
    assert [m.text for m in out.memories] == ["I teamed up with Dunna on the flasks."]
    assert out.memories[0].actors == (101,)  # whom it is with comes from the lines (fix G2)


def test_nudge_to_absent_member_is_kept(store):
    out = val(store, [mem(nudges=[{"to": "Dunna", "dim": "trust", "delta": 1}])])
    assert [(n.a, n.b) for n in out.nudges] == [(101, 104)]


def test_first_person_enforced(store):
    out = val(store, [mem(text="Arkon will bring flasks."), mem(text="Flasks are coming."),
                      mem(text="We are bringing flasks."), mem(text="My flasks are ready.")])
    assert [m.text for m in out.memories] == ["We are bringing flasks.", "My flasks are ready."]


def test_bracketed_evidence_accepted(store):
    out = val(store, [mem(ev=("[L1]", "L2"))])
    assert out.memories[0].evidence == ("chat:1", "chat:2")


def test_nudges_are_combined_per_pair_and_dim(store):
    out = val(store, [
        mem(text="I helped.", nudges=[{"to": "Belra", "dim": "trust", "delta": 2}]),
        mem(text="I helped again.", nudges=[{"to": "Belra", "dim": "trust", "delta": 2},
                                            {"to": "Belra", "dim": "respect", "delta": 1}])])
    assert [(n.dim, n.delta) for n in out.nudges] == [("trust", 3), ("respect", 1)]


def test_nudges_clamped_and_pending(store):
    out = val(store, [mem(nudges=[
        {"to": "Belra", "dim": "trust", "delta": 9},
        {"to": "Belra", "dim": "bogus", "delta": 1},
        {"to": "Nobody", "dim": "trust", "delta": 1},
        {"to": "Arkon", "dim": "trust", "delta": 1},
        {"to": "Belra", "dim": "attraction", "delta": 2},
        {"to": "Testy", "dim": "attraction", "delta": -9}])])
    assert [(n.a, n.b, n.dim, n.delta) for n in out.nudges] == [
        (101, 102, "trust", 3), (101, 1, "attraction", -3)]


def test_prompt_never_names_the_real_name(store):
    ids, lines = mk_lines(store, [(101, "Arkon", "hey Zorblat |Hplayer:Belra|h[Belra]|h"),
                                  (1, "Testmage", "hi")])
    msgs = build_extraction_messages(lines, NAMES, 1, human=HUMAN)
    text = json.dumps(msgs)
    assert "Zorblat" not in text and "|H" not in text
    assert "[L1] Arkon: hey Testy [Belra]" in text and "[L2] Testy: hi" in text
    assert "Testy (character Testmage), the guild master" in text


def test_prompt_takes_the_title_and_upper_forms_of_the_real_name_out(store):
    ids, lines = mk_lines(store, [(102, "Belra", "Zorblat and ZORBLAT are here, zorblat is not")])
    text = json.dumps(build_extraction_messages(lines, NAMES, 1, human=HUMAN))
    assert "Zorblat" not in text and "ZORBLAT" not in text
    assert "Belra: Testy and Testy are here, zorblat is not" in text  # lower case: a word


class Rig:
    def __init__(self, store, clock, replies, bystanders=lambda t, s: []):
        add_human(store)
        seed_test_cast(store)
        self.llm = FakeLLM(script_from(replies))
        sched = LLMScheduler(self.llm, clock=clock)
        writer = MemoryWriter(store, Embedder(sched, "emb"), clock, human=lambda: HUMAN)
        self.ex = Extractor(store, sched, writer, "m", human_guid=lambda: 1,
                            bystanders=bystanders, human=lambda: HUMAN)
        self.store = store

    def thread(self, spec):
        ids, _ = mk_lines(self.store, spec)
        tid = self.store.open_thread("guild", 0.0)
        return ClosedThread(tid, "guild", tuple(ids), "silence")


def status(store, tid):
    return store.thread_status(tid)


def test_extract_end_to_end(store, clock):
    reply = json.dumps({"memories": [
        mem(text="I promised Testy I would bring flasks tonight.", ev=("L1", "L2"), imp=4,
            about=["Testy"], nudges=[{"to": "Testy", "dim": "respect", "delta": 1}]),
        mem("Belra", "I offered to cook for the raid.", ev=("L3",))], "overheard": None})
    rig = Rig(store, clock, [reply])
    t = rig.thread(SPEC)
    r = run(rig.ex.extract(t))
    assert (r.written, r.merged, r.nudges, r.status) == (2, 0, 1, "extracted")
    first = store.memories_with_actors(101, [1])[0]
    assert first.evidence == ("chat:1", "chat:2") and first.importance == 5
    assert store.get_relationship(101, 102).interactions == 1
    assert store.get_relationship(102, 101).interactions == 1
    assert store.get_relationship(101, 1).interactions == 1
    assert store.get_relationship(1, 101).interactions == 0
    assert store.get_relationship(101, 102).last_seen == max(r.ts for r in store.chat_lines(t.line_ids))
    assert store.get_relationship(101, 101).interactions == 0
    [d] = store.pending_rel_deltas()
    assert (d["a"], d["b"], d["dim"], d["delta"], d["applied"]) == (101, 1, "respect", 1, 0)
    assert status(store, t.thread_id) == "extracted" and store.threads_pending() == []
    prompt = json.dumps(rig.llm.calls[0]["messages"])
    assert "Zorblat" not in prompt and "[L1] Arkon:" in prompt
    assert rig.llm.calls[0]["format"] == EXTRACTION_SCHEMA and rig.llm.calls[0]["think"] is False


def test_the_model_can_follow_a_reloaded_config(store, clock):
    """Task 17: mind.toml is reloaded at login, and extraction must move to the new live model
    with the talk (two live models do not fit on the GPU)."""
    add_human(store)
    seed_test_cast(store)
    reply = json.dumps({"memories": [], "overheard": None})
    llm = FakeLLM(script_from([reply, reply]))
    sched = LLMScheduler(llm, clock=clock)
    writer = MemoryWriter(store, Embedder(sched, "emb"), clock, human=lambda: HUMAN)
    model = ["live-a"]
    ex = Extractor(store, sched, writer, lambda: model[0], human_guid=lambda: 1,
                   bystanders=lambda t, s: [], human=lambda: HUMAN)

    def thread():
        ids, _ = mk_lines(store, SPEC)
        return ClosedThread(store.open_thread("guild", 0.0), "guild", tuple(ids), "silence")
    assert run(ex.extract(thread())).status == "extracted"
    model[0] = "live-b"
    assert run(ex.extract(thread())).status == "extracted"
    assert [c["model"] for c in llm.calls if "messages" in c] == ["live-a", "live-b"]


def test_unworthy_thread_is_skipped(store, clock):
    rig = Rig(store, clock, [])
    t = rig.thread([(101, "Arkon", "gz")])
    assert run(rig.ex.extract(t)).status == "skipped"
    assert status(store, t.thread_id) == "skipped" and rig.llm.calls == []


def test_extract_failure_marks_thread_failed(store, clock):
    rig = Rig(store, clock, ["garbage", "garbage again"])
    t = rig.thread(SPEC)
    r = run(rig.ex.extract(t))
    assert r.status == "failed" and r.written == 0
    assert store.threads_pending() == [t.thread_id] and status(store, t.thread_id) == "failed"
    assert store.memories_with_actors(101, [101]) == []


def test_llm_error_marks_thread_failed(store, clock):
    rig = Rig(store, clock, [])
    rig.llm.fail_next()
    t = rig.thread(SPEC)
    assert run(rig.ex.extract(t)).status == "failed"
    assert status(store, t.thread_id) == "failed"


def test_expired_marks_thread_failed(store, clock):
    rig = Rig(store, clock, [])
    t = rig.thread(SPEC)

    async def boom(priority, job, **kw):
        raise Expired(1.0)
    rig.ex._scheduler.submit = boom
    assert run(rig.ex.extract(t)).status == "failed"
    assert status(store, t.thread_id) == "failed"


@pytest.mark.parametrize("imp,expected", [(4, 0), (6, 3)])
def test_overheard_only_if_importance_5_plus(store, clock, imp, expected):
    reply = json.dumps({"memories": [], "overheard": {
        "text": "Arkon and Belra are sorting out the raid food.", "importance": imp}})
    rig = Rig(store, clock, [reply], bystanders=lambda t, s: [103, 104, 105, 101, 1])
    r = run(rig.ex.extract(rig.thread(SPEC)))
    assert r.written == expected
    for b in (103, 104, 105):
        rows = store.memories_with_actors(b, [101])
        assert len(rows) == (1 if expected else 0)
        if rows:
            assert rows[0].kind == "observation" and rows[0].source == "chat"
            assert rows[0].evidence == ("chat:1", "chat:2", "chat:3")
    assert store.memories_with_actors(101, [102]) == []


def test_overheard_caps_bystanders_at_ten(store, clock):
    reply = json.dumps({"memories": [], "overheard": {"text": "Big news.", "importance": 8}})
    rig = Rig(store, clock, [reply], bystanders=lambda t, s: list(range(200, 230)))
    for g in range(200, 212):
        save_persona(store, make_persona(g, f"P{g}"), source="seed", ts=1.0,
                     race="Human", class_name="Warrior")
    r = run(rig.ex.extract(rig.thread(SPEC)))
    assert r.written == 10


def test_run_survives_one_threads_error(store, clock):
    reply = json.dumps({"memories": [mem()], "overheard": None})
    rig = Rig(store, clock, [reply])
    good = rig.thread(SPEC)
    bad = ClosedThread(rig.store.open_thread("guild", 0.0), "guild", (9999,), "silence")
    real = rig.ex.extract

    async def flaky(t):
        if t is bad:
            raise RuntimeError("db exploded")
        return await real(t)
    rig.ex.extract = flaky

    async def go():
        q = asyncio.Queue()
        q.put_nowait(bad)
        q.put_nowait(good)
        task = asyncio.create_task(rig.ex.run(q))
        await asyncio.wait_for(q.join(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(go())
    assert status(store, bad.thread_id) == "failed" and status(store, good.thread_id) == "extracted"


def test_nudges_stored_pending_and_combined(store, clock):
    reply = json.dumps({"memories": [
        mem(ev=("L1",), nudges=[{"to": "Belra", "dim": "trust", "delta": 9},
                                {"to": "Belra", "dim": "trust", "delta": 9}])], "overheard": None})
    rig = Rig(store, clock, [reply])
    run(rig.ex.extract(rig.thread(SPEC)))
    [d] = store.pending_rel_deltas()
    assert (d["a"], d["b"], d["dim"], d["delta"], d["applied"]) == (101, 102, "trust", 3, 0)


def test_romance_screen_covers_members_absent_from_the_thread(store, clock):
    reply = json.dumps({"memories": [mem(text="I have a crush on Ebbo."),
                                     mem(text="I like the plan.")], "overheard": None})
    rig = Rig(store, clock, [reply])
    r = run(rig.ex.extract(rig.thread(SPEC)))
    assert r.written == 1


def test_close_time_is_kept_and_done_threads_are_not_redone(store, clock):
    reply = json.dumps({"memories": [mem()], "overheard": None})
    rig = Rig(store, clock, [reply])
    t = rig.thread(SPEC)
    store.close_thread(t.thread_id, 1234.0, "pending")
    assert run(rig.ex.extract(t)).status == "extracted"
    assert store.thread_closed(t.thread_id) == 1234.0
    again = run(rig.ex.extract(t))
    assert again.status == "extracted" and again.written == 0 and sum(c["model"] == "m" for c in rig.llm.calls) == 1
    sk = rig.thread([(101, "Arkon", "gz")])
    assert run(rig.ex.extract(sk)).status == "skipped"
    assert run(rig.ex.extract(sk)).status == "skipped"


def test_cancel_mid_writes_leaves_nothing_half_committed(store, clock):
    reply = json.dumps({"memories": [mem(nudges=[{"to": "Belra", "dim": "trust", "delta": 1}])],
                        "overheard": {"text": "Arkon sorted the flasks.", "importance": 7}})
    rig = Rig(store, clock, [reply, reply], bystanders=lambda t, s: [103, 104])
    t = rig.thread(SPEC)
    store.close_thread(t.thread_id, 50.0, "pending")
    real = rig.ex._writer.write

    async def cancelling(c, **kw):
        if c.owner == 104:
            raise asyncio.CancelledError()
        return await real(c, **kw)
    rig.ex._writer.write = cancelling

    async def go():
        try:
            await rig.ex.extract(t)
        except asyncio.CancelledError:
            return "cancelled"
    assert run(go()) == "cancelled"
    assert store.pending_rel_deltas() == [] and status(store, t.thread_id) == "pending"
    assert store.get_relationship(101, 102).interactions == 0
    rig.ex._writer.write = real
    assert run(rig.ex.extract(t)).status == "extracted"
    assert len(store.pending_rel_deltas()) == 1
    assert store.get_relationship(101, 102).interactions == 1


def test_rivalry_over_the_human_is_kept(store):
    out = val(store, [
        mem(text="Belra flirted with Testy all night and I hated it.", about=["Belra", "Testy"]),
        mem(text="I flirt with Belra.", about=["Belra"])])
    assert [m.text for m in out.memories] == [
        "Belra flirted with Testy all night and I hated it."]


def test_possessive_or_punctuated_speaker_name_is_fine(store):
    out = val(store, [mem(text="Arkon's raid nights wear me out."),
                      mem(text="Arkon, that is me, brings flasks."),
                      mem(text="Arkon brings flasks for us.")])
    assert [m.text for m in out.memories] == [
        "Arkon's raid nights wear me out.", "Arkon, that is me, brings flasks."]


def test_prompt_follows_his_pronouns(store):
    import dataclasses
    _, lines = mk_lines(store, [(1, "Testmage", "hi all")])
    sys_ = build_extraction_messages(lines, NAMES, 1, human=HUMAN)[0]["content"]
    assert "Always call them Testy" in sys_  # unset: nothing assumed
    he = dataclasses.replace(HUMAN, pronouns="he")
    assert "Always call him Testy" in build_extraction_messages(lines, NAMES, 1,
                                                                human=he)[0]["content"]
