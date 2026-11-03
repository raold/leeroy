"""Lexicon births use actual public laughter and bounded, independently retryable proposals."""
import json
from dataclasses import replace

import pytest

from conftest import run
from nightly.conftest import no_real_name
from leeroy_mind.nightly import language_births as births


def laugh_line(h, at=None, *, author=102, text="pulling with my face again", laughers=(101, 1)):
    at = h.T if at is None else at
    line = h.say(at, author, text)
    for n, guid in enumerate(laughers):
        h.say(at + 5 + n * 3, guid, "haha")
    return line


def moment(h, n=0, *, owners=(101, 102, 104), author=102, laughers=(101, 1), private=False):
    at = h.T + n * 300
    line = laugh_line(h, at, author=author, text="Belra slipped on the ice again", laughers=laughers)
    event = h.boss(at, 631, 11, "wiped", owners)
    ids = [h.mem(g, "Belra slipped on the ice again.", t=at, importance=7, actors=owners,
                 evidence=(f"event:{event}", f"chat:{line}"),
                 scope="private" if private else "open", scope_with=1 if private else 0)
           for g in owners]
    return line, ids


def answer(**changes):
    return {"kind": "nickname", "term": "Frosty", "for": "Belra", "evidence": [1], **changes}


def context(h, *replies, **kwargs):
    return h.ctx({"candidate": list(replies) or [answer()]}, **kwargs)


def terms(h):
    return h.store.conn.execute("SELECT * FROM lexicon ORDER BY id").fetchall()


def calls(ctx):
    return [c for c in ctx.reasoner.llm.calls if "messages" in c]


def test_joke_born_from_two_distinct_laughs(h):
    line = laugh_line(h)
    laugh_line(h, h.T + 300, author=103, text="ok whatever", laughers=(104,))
    ctx = h.ctx()
    assert births.find_jokes(h.store, ctx.window) == [(line, {101, 1})]
    births.jokes(ctx)
    term, = terms(h)
    assert term["kind"] == "joke" and term["term"] == "pulling with my face again"
    assert term["origin_ref"] == f"chat:{line}" and term["originator"] == 102
    for guid in (101, 102):
        made = h.store.keyed_memory(guid, f"joke:{term['id']}")
        assert made.text == "Running joke: pulling with my face again"
        assert made.source == "chat" and made.kind == "story" and made.importance == 5
        assert made.valence == 0.5 and (made.scope, made.scope_with) == ("open", 0)
        assert {f"joke:{term['id']}", f"chat:{line}"} <= set(made.evidence)
    assert h.rows(0) == h.rows(h.human) == [] and calls(ctx) == []


def test_joke_ranking_cap_and_repeated_laugher(h):
    less = laugh_line(h, laughers=(101, 1))
    most = laugh_line(h, h.T + 300, laughers=(101, 103, 1))
    laugh_line(h, h.T + 600, laughers=(101, 101))
    assert births.find_jokes(h.store, h.ctx().window, limit=1) == [(most, {101, 103, 1})]
    assert [i for i, _ in births.find_jokes(h.store, h.ctx().window)][:2] == [most, less]
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, jokes_per_night=1)
    births.jokes(ctx)
    assert [r["origin_ref"] for r in terms(h)] == [f"chat:{most}"]


def test_jokes_repeat_and_existing_origin_do_not_duplicate(h):
    laugh_line(h)
    ctx = h.ctx()
    births.jokes(ctx)
    births.jokes(ctx)
    other_night = h.ctx(night="2026-10-30", window=ctx.window)
    births.jokes(other_night)
    assert len(terms(h)) == 1


@pytest.mark.parametrize("text", ["night Quillon lol", "as an AI I pulled again", "I left the guild",
    "belra has a crush on arkon", "modern rizz", "x " * 61 + "Quillon", "|TQuillon|t funny pull"])
def test_joke_screens_raw_text_before_cleanup_and_truncation(h, text):
    laugh_line(h, text=text)
    ctx = h.ctx()
    births.jokes(ctx)
    assert terms(h) == []
    assert no_real_name(h.store) == []


def test_joke_markup_cleaned_only_after_screening(h):
    laugh_line(h, text="|cffffffffpulling|r  with my face again")
    births.jokes(h.ctx())
    term, = terms(h)
    assert term["term"] == "pulling with my face again"


def test_whisper_noncast_unpaired_and_dropped_laughs_do_not_count(h):
    private = h.say(h.T, 102, "a whispered joke", channel="whisper", to=101)
    h.say(h.T + 5, 101, "haha", channel="whisper", to=102)
    outside = laugh_line(h, h.T + 300, author=999)
    dropped = laugh_line(h, h.T + 600)
    h.store.conn.execute("UPDATE utterance SET dropped='held' WHERE guid=102 AND text=?",
                         ("pulling with my face again",))
    unpaired = laugh_line(h, h.T + 900)
    h.store.conn.execute("DELETE FROM utterance WHERE ts=?", (h.T + 900,))
    one = laugh_line(h, h.T + 1200, laughers=(101, 999))
    bad_reply = laugh_line(h, h.T + 1500, laughers=(101, 103))
    h.store.conn.execute("UPDATE utterance SET dropped='held' WHERE guid=103 AND ts=?", (h.T + 1508,))
    assert births.find_jokes(h.store, h.ctx().window) == []
    births.jokes(h.ctx())
    assert terms(h) == []


def test_private_recalled_ancestry_does_not_seed_public_joke_or_laughter(h):
    secret = h.mem(102, "Private detail.", scope="private", scope_with=1)
    line = laugh_line(h)
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE guid=102 AND ts=?",
                         (json.dumps([secret]), h.T))
    other = laugh_line(h, h.T + 300)
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE guid=101 AND ts=?",
                         (json.dumps([secret]), h.T + 305))
    assert births.find_jokes(h.store, h.ctx().window) == []
    births.jokes(h.ctx())
    assert terms(h) == []


def test_candidate_from_laughed_moment_uses_one_json_and_all_owners_adopt(h):
    line, ids = moment(h)
    ctx = context(h)
    assert births.count_candidates(ctx) == 1
    assert h.store.get_meta(f"nightly:language-candidates:{ctx.night}") is None
    run(births.candidates(ctx))
    term, = terms(h)
    assert (term["kind"], term["term"], term["target"]) == ("nickname", "Frosty", 102)
    assert term["origin_ref"] == f"chat:{line}" and term["originator"] == 102
    adopters = h.store.conn.execute("SELECT guid FROM lexicon_adopter WHERE term=? ORDER BY guid",
                                    (term["id"],)).fetchall()
    assert [r[0] for r in adopters] == [101, 102, 104]
    assert all(h.store.keyed_memory(g, "nick:102").text == "Everyone calls Belra 'Frosty' now."
               for g in (101, 102, 104))
    assert len(calls(ctx)) == 1 and calls(ctx)[0]["think"] is False
    assert births.count_candidates(ctx) == 0 and h.rows(0) == h.rows(h.human) == []


def test_human_nickname_target_is_allowed_without_human_memory(h):
    moment(h, author=1, laughers=(101, 102))
    ctx = context(h, answer(term="Frostboss", **{"for": "Testy"}, evidence=[4]))
    run(births.candidates(ctx))
    term, = terms(h)
    assert term["target"] == h.human and h.rows(h.human) == []


@pytest.mark.parametrize("change", [
    {"term": "Coros"}, {"for": "Coros"}, {"for": "Stranger"}, {"for": None},
    {"term": "Quillon"}, {"term": "|TQuillon|tFrosty"}, {"term": "rizz"},
    {"term": "A nickname much too long for them"}, {"evidence": [999999]},
])
def test_unsupported_or_unsafe_candidate_is_rejected(h, change):
    moment(h)
    ctx = context(h, answer(**change))
    run(births.candidates(ctx))
    assert terms(h) == [] and ctx.done("language", "candidates")
    assert len(calls(ctx)) == 1 and no_real_name(h.store) == []


@pytest.mark.parametrize("mode", ["no_laugh", "private", "outsider", "dropped", "private_recall"])
def test_ineligible_moments_make_no_model_call_or_budget_unit(h, mode):
    line, ids = moment(h, laughers=() if mode == "no_laugh" else (101, 1),
                       owners=(101, 102, 999) if mode == "outsider" else (101, 102, 104),
                       private=mode == "private")
    if mode == "dropped":
        h.store.conn.execute("UPDATE utterance SET dropped='held' WHERE guid=102 AND ts=?", (h.T,))
    elif mode == "private_recall":
        private = h.mem(102, "Private detail.", scope="private", scope_with=1)
        h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE guid=102 AND ts=?",
                             (json.dumps([private]), h.T))
    ctx = context(h)
    assert births.count_candidates(ctx) == 0
    run(births.candidates(ctx))
    assert terms(h) == [] and calls(ctx) == []


def test_none_proposal_completes_without_term(h):
    moment(h)
    ctx = context(h, answer(kind="none", term="", **{"for": None}))
    run(births.candidates(ctx))
    run(births.candidates(ctx))
    assert terms(h) == [] and len(calls(ctx)) == 1 and ctx.done("language", "candidates")


def test_candidate_cap_is_frozen_across_resume_and_new_moments(h, monkeypatch):
    for n in range(5):
        moment(h, n)
    ctx = context(h, answer(kind="joke", term="frost joke", **{"for": None}),
                  answer(kind="joke", term="slippery joke", **{"for": None}),
                  answer(kind="joke", term="icy joke", **{"for": None}))
    create = births.lexicon.create_term

    def fail_middle(store, context, **kwargs):
        if kwargs["term"] == "slippery joke":
            raise RuntimeError("synthetic middle proposal failure")
        return create(store, context, **kwargs)

    monkeypatch.setattr(births.lexicon, "create_term", fail_middle)
    assert births.count_candidates(ctx) == 3
    run(births.candidates(ctx))
    assert ctx.failures["language"] == 1 and len(terms(h)) == 2
    assert not ctx.done("language", "candidates") and births.count_candidates(ctx) == 1
    monkeypatch.setattr(births.lexicon, "create_term", create)
    resumed = context(h, answer(kind="joke", term="slippery joke", **{"for": None}))
    run(births.candidates(resumed))
    assert len(calls(resumed)) == 1 and len(terms(h)) == 3
    assert resumed.done("language", "candidates") and births.count_candidates(resumed) == 0
    moment(h, 6)
    run(births.candidates(resumed))
    assert len(calls(resumed)) == 1


@pytest.mark.parametrize("change", ["scope", "ancestor", "drop", "text", "laugh"])
def test_candidate_rechecks_all_sources_and_sent_laughter_after_await(h, change):
    line, ids = moment(h)
    ancestor = h.mem(101, "Earlier public detail.", importance=5,
                     evidence=(f"chat:{line}",))
    original = h.store.get_memories([ids[-1]])[0]
    h.store.conn.execute("UPDATE memory SET evidence=? WHERE id=?",
                         (json.dumps([*original.evidence, f"memory:{ancestor}"]), ids[-1]))

    def mutate(_):
        if change in {"scope", "ancestor"}:
            ident = ids[-1] if change == "scope" else ancestor
            h.store.conn.execute("UPDATE memory SET scope='private',scope_with=1 WHERE id=?", (ident,))
        elif change == "drop":
            h.store.conn.execute("UPDATE utterance SET dropped='held' WHERE guid=102 AND ts=?", (h.T,))
        elif change == "text":
            h.store.conn.execute("UPDATE memory SET text='Different moment.' WHERE id=?", (ids[-1],))
        else:
            h.store.conn.execute("UPDATE chat_line SET text='ok' WHERE ts>? AND ts<=?", (h.T, h.T + 10))
        return answer()

    ctx = context(h, mutate)
    run(births.candidates(ctx))
    assert terms(h) == [] and ctx.failures["language"] == 1
    assert not ctx.done("language", "candidates")


def test_candidate_memory_failure_rolls_back_term_adopters_and_checkpoint(h, monkeypatch):
    moment(h)
    ctx = context(h)
    put = ctx.writer.put

    def write_then_fail(row, **kwargs):
        put(row, **kwargs)
        raise RuntimeError("synthetic adopter memory failure")

    monkeypatch.setattr(ctx.writer, "put", write_then_fail)
    run(births.candidates(ctx))
    assert terms(h) == [] and h.count("lexicon_adopter") == 0
    assert ctx.failures["language"] == 1 and not ctx.done("language", "candidates")
    assert not any((m.key or "").startswith("nick:") for g in (101, 102, 104) for m in h.rows(g))
