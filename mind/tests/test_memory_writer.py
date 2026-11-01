import dataclasses
import logging

import pytest

from conftest import run, seed_test_cast
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.client import LLMError
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.recall import Recall
from leeroy_mind.memory.writer import Embedder, MemoryCandidate, MemoryWriter, WriteResult
from leeroy_mind.protocol import Chat
from leeroy_mind.storage.rows import cosine, unpack_vec
from leeroy_mind.testing.fake_llm import FakeLLM, fake_embed


def jump(clock, seconds):
    clock._t += seconds


@pytest.fixture
def rig(store, clock):
    seed_test_cast(store)
    llm = FakeLLM()
    emb = Embedder(LLMScheduler(llm, clock=clock), "emb-model")
    return MemoryWriter(store, emb, clock), llm


def cand(text, **kw):
    base = dict(owner=101, kind="observation", t_event=1000, actors=(), importance=5,
                valence=0.0, source="extract", evidence=("chat:1",))
    base.update(kw)
    return MemoryCandidate(text=text, **base)


def test_embedder_packs_vectors_and_asks_for_512(rig):
    w, llm = rig
    vecs = run(w._embedder.embed(["alpha beta", "gamma"], 3))
    assert len(vecs) == 2 and len(vecs[0]) == 512 * 4
    assert llm.calls[0]["model"] == "emb-model" and llm.calls[0]["dimensions"] == 512
    assert cosine(unpack_vec(vecs[0]), unpack_vec(vecs[0])) == pytest.approx(1.0)


def test_rejects_non_game_without_evidence(rig):
    w, _ = rig
    r = run(w.write(cand("Coros pulled early.", evidence=())))
    assert r.action == "rejected" and r.id is None
    assert run(w.write(cand("Coros pulled early.", source="game", evidence=()))).action == "inserted"


def test_rejects_empty_text_and_unknown_owner(rig):
    w, _ = rig
    assert run(w.write(cand("   "))).action == "rejected"
    assert run(w.write(cand("hello there", owner=999))).action == "rejected"
    assert run(w.write(cand("hello there", owner=1))).action == "rejected"
    assert run(w.write(cand("guild wide note", owner=0))).action == "inserted"


def test_truncates_to_200_on_word_boundary(rig, store):
    w, _ = rig
    text = "x" * 190 + " straddlingword " + "y" * 30 + " tail"  # a word spans position 200
    assert len(text) > 225
    r = run(w.write(cand(text)))
    m = store.get_memories([r.id])[0]
    assert m.text == "x" * 190  # cut before the word, never inside it


def test_clamps_importance_and_valence(rig, store):
    w, _ = rig
    m = store.get_memories([run(w.write(cand("one", importance=99, valence=-5))).id])[0]
    assert (m.importance, m.valence) == (10, -1.0)
    m = store.get_memories([run(w.write(cand("two", importance=-3, valence=7))).id])[0]
    assert (m.importance, m.valence) == (1, 1.0)


def test_strips_item_links(rig, store):
    w, _ = rig
    link = "|cffa335ee|Hitem:49623:0|h[Shadowmourne]|h|r"
    m = store.get_memories([run(w.write(cand(f"Won {link} today."))).id])[0]
    assert m.text == "Won [Shadowmourne] today."


def test_rate_limits(rig, clock):
    w, _ = rig
    n = iter(f"unique thing number {i} xq{i}" for i in range(300))
    for _ in range(20):
        assert run(w.write(cand(next(n)))).action == "inserted"
    assert run(w.write(cand(next(n)))).action == "rate_limited"
    jump(clock, 3600)
    assert run(w.write(cand(next(n)))).action == "inserted"
    # 120 inserts within 24 h in total, at most 20 an hour
    count = 21
    while count < 120:
        jump(clock, 600)
        r = run(w.write(cand(next(n))))
        assert r.action == "inserted", (count, r)
        count += 1
    assert run(w.write(cand(next(n)))).action == "rate_limited"
    # another owner is unaffected
    assert run(w.write(cand("different owner entirely", owner=102))).action == "inserted"


def test_rate_limit_waived_for_p1c(rig):
    w, _ = rig
    for i in range(20):
        run(w.write(cand(f"filler line {i} zz{i}")))
    assert run(w.write(cand("over the cap"))).action == "rate_limited"
    assert run(w.write(cand("over the cap"), enforce_limits=False)).action == "inserted"


def test_keyed_rows_update_in_place_and_skip_limits(rig, store):
    w, _ = rig
    for i in range(20):
        run(w.write(cand(f"filler line {i} zz{i}")))
    a = run(w.write(cand("We wiped 2 times.", source="game", key="boss:1:2:n")))
    b = run(w.write(cand("We wiped 3 times.", source="game", key="boss:1:2:n")))
    assert a.action == b.action == "updated" and a.id == b.id
    m = store.get_memories([a.id])[0]
    assert m.text == "We wiped 3 times." and m.embedding is None
    assert run(w.embed_pending()) >= 1
    assert store.get_memories([a.id])[0].embedding is not None


def test_dedup_merges_within_48h(rig, store):
    w, _ = rig
    a = run(w.write(cand("we wiped on sindragosa six times", actors=(101, 102),
                         evidence=("chat:1",), importance=4)))
    b = run(w.write(cand("we wiped on sindragosa six times", actors=(101, 102),
                         evidence=("chat:2",), importance=6)))
    m = store.get_memories([a.id])[0]
    assert b.action == "merged" and b.id == a.id
    assert m.evidence == ("chat:1", "chat:2") and m.importance == 6 and m.merged_n == 2


def test_dedup_is_by_cosine(rig):
    w, _ = rig
    run(w.write(cand("we wiped on sindragosa six times tonight again")))
    assert run(w.write(cand("we wiped on sindragosa six times tonight"))).action == "merged"
    assert run(w.write(cand("Arkon bought a sea turtle mount"))).action == "inserted"


def test_no_dedup_across_actor_sets_or_after_48h(rig, clock):
    w, _ = rig
    t = "we wiped on sindragosa six times"
    assert run(w.write(cand(t, actors=(101, 102)))).action == "inserted"
    assert run(w.write(cand(t, actors=(101, 103)))).action == "inserted"
    assert run(w.write(cand(t, actors=(101, 102, 103)))).action == "inserted"
    assert run(w.write(cand(t, actors=(101,)))).action == "inserted"
    assert run(w.write(cand(t, actors=(101, 102), kind="conversation"))).action == "inserted"
    assert run(w.write(cand(t, actors=(101, 102), owner=102))).action == "inserted"
    jump(clock, 48 * 3600)
    assert run(w.write(cand(t, actors=(101, 102)))).action == "inserted"


def test_embed_failure_still_inserts(rig, store):
    w, llm = rig
    llm.fail_next(1)
    r = run(w.write(cand("the embed service was down for this one")))
    assert r.action == "inserted"
    assert store.get_memories([r.id])[0].embedding is None
    assert run(w.embed_pending()) == 1
    assert store.get_memories([r.id])[0].embedding is not None
    assert run(w.embed_pending()) == 0


def test_embed_failure_dedups_by_exact_text(rig):
    w, llm = rig
    llm.fail_next(2)
    assert run(w.write(cand("Same Words, here!"))).action == "inserted"
    assert run(w.write(cand("same words here"))).action == "merged"


def test_embed_pending_survives_failure(rig, store):
    w, llm = rig
    llm.fail_next(1)
    r = run(w.write(cand("left without a vector")))
    llm.fail_next(2)  # the batch and the probe: Ollama is away
    assert run(w.embed_pending()) == 0
    assert store.get_memories([r.id])[0].embedding is None


def test_real_name_is_screened(store, clock):
    seed_test_cast(store)
    h = HumanIdentity(1, "Testmage", "Testy", "Zorblat", True)
    w = MemoryWriter(store, Embedder(LLMScheduler(FakeLLM(), clock=clock), "m"), clock,
                     human=lambda: h)
    m = store.get_memories([run(w.write(cand("Zorblat linked me his gear."))).id])[0]
    assert "Zorblat" not in m.text and "Testy" in m.text
    m = store.get_memories([run(w.write(cand("ZORBLAT got the dice."))).id])[0]
    assert m.text == "Testy got the dice."
    bill = HumanIdentity(1, "Testmage", "Testy", "Bill", True)  # a name that is also a word
    w = MemoryWriter(store, Embedder(LLMScheduler(FakeLLM(), clock=clock), "m"), clock,
                     human=lambda: bill)
    m = store.get_memories([run(w.write(cand("I paid the bill and Bill owes me."))).id])[0]
    assert m.text == "I paid the bill and Testy owes me."


def test_candidate_shape():
    assert {f.name for f in dataclasses.fields(MemoryCandidate)} >= {
        "owner", "kind", "text", "t_event", "actors", "importance", "valence", "source",
        "evidence", "confidence", "place", "boss_id", "raid_id", "key"}


def test_game_rows_never_merge_by_cosine(rig):
    w, _ = rig
    a = "Arkon won the Frostbrand Axe from the third boss today"
    b = "Arkon won the Frostbrand Axe from the third boss tonight"
    assert run(w.write(cand(a, source="game", evidence=(), actors=(101,)))).action == "inserted"
    assert run(w.write(cand(b, source="game", evidence=(), actors=(101,)))).action == "inserted"
    assert run(w.write(cand(a, source="game", evidence=(), actors=(101,)))).action == "merged"


class _SameVec(FakeLLM):
    """Scores every text as one vector, as a real embedder scores a close paraphrase."""

    async def embed(self, model, inputs, *, dimensions=512, timeout=None):
        return [fake_embed("new job monday", dimensions) for _ in inputs]


def _line(store, channel, frm, to, text):
    return store.insert_chat_line(Chat(ts=0, channel=channel, from_guid=frm, from_name="x",
                                       to_guid=to, text=text, map=0, zone=0),
                                  session_id=None, origin="human")


def _talk(text, line, importance=5):
    return MemoryCandidate(owner=102, kind="conversation", text=text, t_event=1000,
                           actors=(1, 102), importance=importance, valence=0.0, source="extract",
                           evidence=(f"chat:{line}",))


def test_a_public_twin_never_merges_into_a_whisper_only_memory(store, clock):
    """Privacy I1: merging would make the whispered text eligible on every channel."""
    seed_test_cast(store)
    emb = Embedder(LLMScheduler(_SameVec(), clock=clock), "m")
    w = MemoryWriter(store, emb, clock)
    wl = _line(store, "whisper", 1, 102, "dont tell anyone, got the hospital job, pay is insane")
    private = run(w.write(_talk("Testy told me he got the hospital job and the pay is insane.",
                                wl, importance=6)))
    jump(clock, 3600)
    gl = _line(store, "guild", 1, 0, "got a new job guys, start monday")
    public = run(w.write(_talk("Testy told me he got a new job and starts on Monday.", gl)))
    assert (private.action, public.action) == ("inserted", "inserted") and private.id != public.id
    assert store.get_memories([private.id])[0].evidence == (f"chat:{wl}",)
    got = run(Recall(store, emb, clock, tau_days=5.0).recall(
        102, "new job", present=[1], addressee=1, channel="guild", touch=False))
    assert [m.id for m in got] == [public.id]  # the whispered version stays out of guild chat


def test_dedup_merges_only_within_one_privacy_class(store, clock):
    seed_test_cast(store)
    w = MemoryWriter(store, Embedder(LLMScheduler(_SameVec(), clock=clock), "m"), clock)
    g1 = _line(store, "guild", 1, 0, "new job on monday")
    g2 = _line(store, "guild", 102, 0, "gz on the job")
    w1 = _line(store, "whisper", 1, 102, "the new job pays well")
    w2 = _line(store, "whisper", 102, 1, "proud of you")
    w3 = _line(store, "whisper", 103, 102, "did he tell you about the job")
    public = run(w.write(_talk("He has a new job from Monday.", g1)))
    assert run(w.write(_talk("He starts a new job on Monday.", g2))) == \
        WriteResult(public.id, "merged")  # positive control: public into public
    whisper = run(w.write(_talk("He told me the new job pays well.", w1)))
    assert whisper.action == "inserted" and whisper.id != public.id  # whisper-only, not public
    assert run(w.write(_talk("He said the new job pays well.", w2))) == \
        WriteResult(whisper.id, "merged")  # the same whisper pair
    other_pair = run(w.write(_talk("Coros asked me about his new job.", w3)))
    assert other_pair.action == "inserted" and other_pair.id not in (public.id, whisper.id)
    mixed = run(w.write(MemoryCandidate(
        owner=102, kind="conversation", text="He talked about the new job.", t_event=1000,
        actors=(1, 102), importance=5, valence=0.0, source="extract",
        evidence=(f"chat:{w1}", f"chat:{g1}"))))
    assert mixed.action == "inserted" and mixed.id not in (public.id, whisper.id, other_pair.id)
    kept = store.get_memories([mixed.id])[0]
    assert (kept.scope, kept.scope_with) == ("private", 1)  # B2: any whisper line makes it private
    assert store.get_memories([whisper.id])[0].evidence == (f"chat:{w1}", f"chat:{w2}")


def test_keyed_rows_count_toward_the_limits(rig):
    w, _ = rig
    for i in range(20):
        assert run(w.write(cand(f"keyed {i}", source="game", key=f"k:{i}"))).action == "updated"
    assert run(w.write(cand("one more plain line"))).action == "rate_limited"


def test_limit_is_rechecked_after_the_embed_await(store, clock):
    import asyncio
    seed_test_cast(store)
    gate = asyncio.Event()
    w = MemoryWriter(store, Embedder(LLMScheduler(FakeLLM(gate=gate), clock=clock), "m"), clock)

    async def go():
        gate.set()
        for i in range(19):
            await w.write(cand(f"seed line {i} qq{i}"), enforce_limits=False)
        gate.clear()
        t1 = asyncio.ensure_future(w.write(cand("racer one alpha")))
        t2 = asyncio.ensure_future(w.write(cand("racer two beta")))
        await asyncio.sleep(0.05)  # both passed the first check and wait on the embed
        gate.set()
        return [r.action for r in await asyncio.gather(t1, t2)]
    assert sorted(run(go())) == ["inserted", "rate_limited"]


def test_embed_pending_does_not_stamp_a_stale_vector(store, clock):
    import asyncio
    seed_test_cast(store)
    gate = asyncio.Event()
    w = MemoryWriter(store, Embedder(LLMScheduler(FakeLLM(gate=gate), clock=clock), "m"), clock)

    async def go():
        a = await w.write(cand("We wiped 2 times.", source="game", key="boss:1:2:n"))
        pending = asyncio.ensure_future(w.embed_pending())
        await asyncio.sleep(0.05)
        await w.write(cand("We wiped 3 times.", source="game", key="boss:1:2:n"))  # rewritten
        gate.set()
        return a.id, await pending
    mid, done = run(go())
    assert done == 0
    m = store.get_memories([mid])[0]
    assert m.text == "We wiped 3 times." and m.embedding is None
    assert [r.id for r in store.memories_missing_embedding(10)] == [mid]


def test_owner_refs_come_only_from_the_owner(rig, store):
    """An "owner:" evidence ref marks the owner's own history; nothing else may claim one."""
    w, _ = rig
    ref = ("owner:M7",)
    story = "Testy held the bridge alone until the raid came back."
    assert run(w.write(cand(story, evidence=ref))).action == "rejected"
    assert run(w.write(cand(story, evidence=("chat:1", "owner:M7")))).action == "rejected"
    assert run(w.write(cand(story, source="game", evidence=ref))).action == "rejected"
    r = run(w.write(cand(story, source="owner", kind="story", evidence=ref, key="owner:M7:101")))
    m = store.get_memories([r.id])[0]
    assert r.action == "updated" and (m.source, m.evidence, m.key) == ("owner", ref,
                                                                     "owner:M7:101")


def test_a_candidate_may_carry_its_creation_time(rig, store, clock):
    w, _ = rig
    old = 1_600_000_000  # an old memory, as the owner's history seeds them
    r = run(w.write(cand("An old story.", source="owner", kind="story", evidence=("owner:M1",),
                         key="owner:M1:101", t_event=old, t_created=old)))
    m = store.get_memories([r.id])[0]
    assert (m.t_event, m.t_created, m.t_accessed) == (old, old, old)
    m = store.get_memories([run(w.write(cand("A plain new one."))).id])[0]
    assert m.t_created == m.t_accessed == int(clock.now())  # without one: the clock's now


class _Picky(FakeLLM):
    """An embedder that fails on one text (a vector Ollama cannot make), or on everything
    while `down` is set (an outage)."""

    def __init__(self):
        super().__init__()
        self.down = False

    async def embed(self, model, inputs, *, dimensions=512, timeout=None):
        if self.down or any("poison" in t for t in inputs):
            self.calls.append({"input": list(inputs), "failed": True})
            raise LLMError("embed failed")
        return await super().embed(model, inputs, dimensions=dimensions, timeout=timeout)


def _unembedded(store, clock, texts):
    w = MemoryWriter(store, Embedder(LLMScheduler(FakeLLM(), clock=clock), "m"), clock)
    ids = []
    for t in texts:
        r = run(w.write(cand(t, source="game", key=f"k:{t}")))  # keyed: stored without a vector
        ids.append(r.id)
    return ids


def test_one_bad_text_never_blocks_the_backfill(store, clock):
    """Liveops M10: the backfill retried the same oldest batch forever."""
    seed_test_cast(store)
    poison, good1, good2 = _unembedded(store, clock, ["poison text here", "good one", "good two"])
    llm = _Picky()
    w = MemoryWriter(store, Embedder(LLMScheduler(llm, clock=clock), "m"), clock)
    assert run(w.embed_pending()) == 2
    got = {m.id: m.embedding for m in store.get_memories([poison, good1, good2])}
    assert got[poison] is None and got[good1] is not None and got[good2] is not None
    llm.calls.clear()
    assert run(w.embed_pending()) == 0 and llm.calls == []  # parked: not tried every 10 s
    jump(clock, MemoryWriter.PARK_S)
    assert run(w.embed_pending()) == 0
    assert ["poison text here"] in [c["input"] for c in llm.calls]  # tried again later
    llm.calls.clear()
    assert run(w.embed_pending()) == 0 and llm.calls == []  # and parked again


def test_an_outage_parks_nothing_and_warns_once(store, clock, caplog):
    seed_test_cast(store)
    ids = _unembedded(store, clock, ["first", "second"])
    llm = _Picky()
    llm.down = True
    w = MemoryWriter(store, Embedder(LLMScheduler(llm, clock=clock), "m"), clock)
    with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
        for _ in range(3):
            assert run(w.embed_pending()) == 0
    assert caplog.text.count("embedding backfill failed") == 1
    llm.down = False
    assert run(w.embed_pending()) == 2  # nothing was parked by the outage
    assert all(m.embedding is not None for m in store.get_memories(ids))


def test_a_passing_embed_failure_is_retried_at_once_not_parked(rig, store):
    w, llm = rig
    llm.fail_next(1)
    r = run(w.write(cand("missed its vector once")))
    llm.fail_next(1)  # the backfill's batch fails once; the probe and the retry pass
    assert run(w.embed_pending()) == 1
    assert store.get_memories([r.id])[0].embedding is not None
