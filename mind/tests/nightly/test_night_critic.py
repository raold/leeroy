"""The nightly critic scores synthetic sent lines; no model or live data is used."""
import json
import random
from dataclasses import replace

import pytest

from conftest import run
from nightly.conftest import no_real_name
from leeroy_mind.nightly import budget, critic, growth
from leeroy_mind.nightly.ground import Claim
from leeroy_mind.nightly.settings import night_bounds
from leeroy_mind.nightly.store import enqueue_review, ledger_add, ledger_rows


def sent(h, guid=101, n=4, *, at=None, text="Synthetic guild line", memories=(), channel="guild"):
    ids = [h.utter((h.T if at is None else at) + i, guid, f"{text} {i}.", memories) for i in range(n)]
    if channel == "whisper":
        h.store.conn.executemany("UPDATE utterance SET channel='whisper',to_guid=1 WHERE id=?",
                                 [(ident,) for ident in ids])
    return ids


def rows(h, ids):
    return [h.store.conn.execute("SELECT * FROM utterance WHERE id=?", (ident,)).fetchone() for ident in ids]


def answer(*scores, why="Unlike their usual speech."):
    return {"scores": [{"i": i, "score": score, "why": why} for i, score in enumerate(scores or (5, 1))]}


def context(h, *answers, **kwargs):
    return h.ctx({"critic": list(answers) or [answer()]}, **kwargs)


def flags(h, guid=101):
    return [row for row in ledger_rows(h.store, guid) if row["kind"] == "drift_flag"]


def note(h, *, at=None):
    at = h.T - 2 * 86400 if at is None else at
    growth.apply_note(h.store, 101, Claim("self_insight", 101, "I can be more patient.", ["rule:patient"]),
                      "consistent", "", at)


def flag(h, at, *, night=None, with_review=False):
    review = (enqueue_review(h.store, guid=101, kind="drift_line", text="An earlier line.",
                              evidence=[], reason="Synthetic flag.", now=at) if with_review else None)
    ident = ledger_add(h.store, guid=101, kind="drift_flag", text="An earlier line.",
                       evidence=[], ref=review, now=at)
    if night is not None:
        h.store.set_meta(f"nightly:critic-flag-night:{ident}", night)
    return ident


def test_seeded_sampling_is_capped_and_requires_three_sent_cast_lines(h):
    for guid in (101, 102, 103):
        sent(h, guid, 8)
    sent(h, 104, 2)
    quiet = sent(h, 105, 3)
    h.store.conn.execute("UPDATE utterance SET dropped='guard' WHERE id=?", (quiet[-1],))
    sent(h, 0, 4)
    sent(h, h.human, 4)
    sent(h, 999, 4)
    window = (h.T, h.T + 100)
    first = critic.sample_lines(h.store, window, random.Random(9), per_member=2, total=5)
    again = critic.sample_lines(h.store, window, random.Random(9), per_member=2, total=5)
    assert {g: [r["id"] for r in rr] for g, rr in first.items()} == {
        g: [r["id"] for r in rr] for g, rr in again.items()}
    assert sum(map(len, first.values())) == 5
    assert set(first) <= {101, 102, 103} and all(len(rr) <= 2 for rr in first.values())
    assert critic.sample_lines(h.store, window, random.Random(9), per_member=0, total=5) == {}
    assert critic.sample_lines(h.store, window, random.Random(9), per_member=2, total=0) == {}
    assert critic.sample_lines(h.store, (h.T + 100, h.T + 200), random.Random(9), per_member=2, total=5) == {}


def test_low_scores_screen_source_and_reason_and_write_linked_audit_rows(h):
    ids = sent(h, text="Quillon said that")
    ctx = context(h, answer(5, 1, why="|TQuillon|tQuillon would not say this."))
    run(critic.critic_member(ctx, 101, rows(h, ids[:2])))
    score = h.store.conn.execute("SELECT * FROM critic_score").fetchone()
    review = h.store.conn.execute("SELECT * FROM review_queue").fetchone()
    flag_row, = flags(h)
    assert (score["guid"], score["n"], score["mean"]) == (101, 2, 3.0)
    assert review["kind"] == "drift_line" and review["text"] == "[name] said that 1."
    assert "Quillon" not in review["reason"] and "[name]" in review["reason"]
    assert flag_row["ref"] == review["id"] and flag_row["text"] == review["text"]
    assert json.loads(review["evidence"]) == [f"utterance:{ids[1]}"]
    assert h.store.get_meta(critic._flag_key(flag_row["id"])) == ctx.night
    prompts = str(ctx.reasoner.llm.calls)
    assert "Quillon" not in prompts and len(ctx.reasoner.llm.calls) == 1
    # Original injected utterances retain their raw source; newly written audit text does not.
    assert not any(table in {"review_queue", "persona_ledger"} for table, _ in no_real_name(h.store))
    assert h.count("memory") == 0 and ctx.done("critic", "101")


def test_scores_can_arrive_out_of_order_but_stay_attached_to_displayed_indices(h):
    ids = sent(h)
    reply = {"scores": [{"i": 1, "score": 1, "why": "Second line."},
                        {"i": 0, "score": 5, "why": "First line."}]}
    run(critic.critic_member(context(h, reply), 101, rows(h, ids[:2])))
    assert h.store.conn.execute("SELECT text FROM review_queue").fetchone()[0] == "Synthetic guild line 1."


def test_quiet_members_and_dropped_lines_make_no_model_call(h):
    sent(h, n=2)
    ids = sent(h, 102, 3)
    h.store.conn.execute("UPDATE utterance SET dropped='policy' WHERE id=?", (ids[-1],))
    ctx = context(h)
    run(critic.stage_critic(ctx))
    assert ctx.reasoner.llm.calls == []
    assert h.count("critic_score") == h.count("review_queue") == h.count("persona_ledger") == 0


def test_whispers_are_reviewed_only_in_the_owners_audit(h):
    whisper = h.say(h.T - 5, h.human, "A private detail.", channel="whisper", to=101)
    private = h.mem(101, "I heard a private detail.", scope="secret", scope_with=h.human,
                    evidence=(f"chat:{whisper}",))
    ids = sent(h, channel="whisper", memories=[private], text="A private synthetic reply")
    other = sent(h, 102, text="ANOTHER SPEAKERS PRIVATE WORDS", channel="whisper")
    before = h.count("memory"), h.count("utterance"), h.count("chat_line")
    ctx = context(h)
    run(critic.critic_member(ctx, 101, rows(h, ids[:2])))
    assert len(flags(h)) == 1 and h.count("critic_score") == 1
    assert (h.count("memory"), h.count("utterance"), h.count("chat_line")) == before
    prompt = str(ctx.reasoner.llm.calls)
    assert "A private synthetic reply" in prompt
    assert "ANOTHER SPEAKERS PRIVATE WORDS" not in prompt and "I heard a private detail" not in prompt
    assert not flags(h, 102) and other


def test_three_flags_roll_back_notes_and_undo_restores_them(h):
    sent(h)
    flag(h, h.T - 4 * 86400)
    flag(h, h.T - 3 * 86400)
    note(h)
    ctx = context(h)
    run(critic.stage_critic(ctx))
    assert h.store.get_state(101).self_notes == ()
    rolled, = [r for r in ledger_rows(h.store, 101) if r["kind"] == "note_rolled_back"]
    assert growth.undo(h.store, rolled["id"], now=h.T + 10)
    assert h.store.get_state(101).self_notes[0]["text"] == "I can be more patient."
    assert h.store.get_state(101).self_notes[0]["added"] == h.T - 2 * 86400
    assert h.store.conn.execute("SELECT resolved FROM review_queue WHERE id=?", (rolled["ref"],)).fetchone()[0] == h.T + 10
    old = h.count("persona_ledger"), h.count("review_queue")
    run(critic.stage_critic(ctx))
    assert (h.count("persona_ledger"), h.count("review_queue")) == old
    assert len(ctx.reasoner.llm.calls) == 1


@pytest.mark.parametrize("origin,rolled", [("2026-10-25", False), ("2026-10-26", True), ("2026-11-02", False)])
def test_drift_window_uses_seven_logical_nights_on_delayed_resume(h, origin, rolled):
    ctx = context(h, night="2026-11-01")
    at = night_bounds(ctx.night, ctx.tz)[0] + 3600
    sent(h, at=at)
    physical = at + 10 * 86400
    h.clock.set(physical)
    flag(h, physical - 100, night=origin)
    flag(h, physical - 90, night=origin)
    note(h, at=physical - 80)
    run(critic.stage_critic(ctx))
    assert (not h.store.get_state(101).self_notes) is rolled


def test_undone_drift_flag_does_not_trigger_a_new_rollback(h):
    sent(h)
    old = flag(h, h.T - 4 * 86400, with_review=True)
    flag(h, h.T - 3 * 86400)
    note(h)
    assert growth.undo(h.store, old, now=h.T - 1)
    run(critic.stage_critic(context(h)))
    assert h.store.get_state(101).self_notes
    assert not any(r["kind"] == "note_rolled_back" for r in ledger_rows(h.store, 101))


@pytest.mark.parametrize("reply", [
    {"scores": []},
    {"scores": [{"i": 0, "score": 1, "why": "Only one."}]},
    {"scores": [{"i": i, "score": 1, "why": "Duplicate."} for i in (0, 0)]},
    {"scores": [{"i": i, "score": 1, "why": "Unknown index."} for i in (0, 2)]},
    {"scores": [{"i": i, "score": 1, "why": "Negative index."} for i in (-1, 1)]},
    {"scores": [{"i": i, "score": 1, "why": "Boolean index."} for i in (False, 1)]},
    answer(0, 5), answer(6, 5), answer(True, 5), answer(1.5, 5),
])
def test_score_validation_requires_exact_full_index_coverage(h, monkeypatch, reply):
    sent(h)
    ctx = context(h)

    async def invalid(*args, **kwargs):
        return reply

    monkeypatch.setattr(ctx.reasoner, "json", invalid)
    run(critic.stage_critic(ctx))
    assert ctx.failures["critic"] == 1 and not ctx.done("critic", "101")
    assert h.count("critic_score") == h.count("review_queue") == h.count("persona_ledger") == 0


@pytest.mark.parametrize("changed", ["text", "dropped", "owner", "persona", "ancestor", "notes"])
def test_sources_and_uncached_persona_are_revalidated_after_model_await(h, monkeypatch, changed):
    event = h.boss(h.T - 10, 631, 11, "started", (101, 102))
    ancestor = h.mem(101, "A remembered pull.", evidence=(f"event:{event}",))
    source = h.mem(101, "A later recollection.", evidence=(f"memory:{ancestor}",))
    ids = sent(h, memories=[source])
    ctx = context(h)
    ctx.persona(101)
    ctx.card(101)  # intentionally prime both caches before the owner edits the persona

    async def mutate(*args, **kwargs):
        if changed == "text":
            h.store.conn.execute("UPDATE utterance SET text='Changed source.' WHERE guid=101")
        elif changed == "dropped":
            h.store.conn.execute("UPDATE utterance SET dropped='policy' WHERE guid=101")
        elif changed == "owner":
            h.store.conn.execute("UPDATE utterance SET guid=102 WHERE guid=101")
        elif changed == "persona":
            h.persona(101, player_gender="female")
        elif changed == "ancestor":
            h.store.conn.execute("UPDATE memory SET scope='private',scope_with=1 WHERE id=?", (ancestor,))
        else:
            note(h)
        return answer()

    monkeypatch.setattr(ctx.reasoner, "json", mutate)
    run(critic.critic_member(ctx, 101, rows(h, ids[:2])))
    assert ctx.failures["critic"] == 1 and not ctx.done("critic", "101")
    assert h.count("critic_score") == h.count("review_queue") == 0 and flags(h) == []


def test_a_failed_rollback_rolls_back_every_audit_write_and_retry_is_once(h, monkeypatch):
    sent(h)
    flag(h, h.T - 4 * 86400)
    flag(h, h.T - 3 * 86400)
    note(h)
    original = critic.rollback_week
    before = h.count("persona_ledger")

    def broken(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Synthetic rollback write failed")

    monkeypatch.setattr(critic, "rollback_week", broken)
    first = context(h)
    run(critic.stage_critic(first))
    assert not first.done("critic", "101") and h.store.get_state(101).self_notes
    assert h.count("persona_ledger") == before
    assert h.count("critic_score") == h.count("review_queue") == 0
    monkeypatch.setattr(critic, "rollback_week", original)
    retry = context(h)
    run(critic.stage_critic(retry))
    assert retry.done("critic", "101") and not h.store.get_state(101).self_notes
    final = h.count("persona_ledger"), h.count("review_queue")
    run(critic.stage_critic(retry))
    assert (h.count("persona_ledger"), h.count("review_queue")) == final
    assert len(retry.reasoner.llm.calls) == 1


def test_frozen_sample_keeps_cap_and_successful_siblings_across_retry(h, monkeypatch):
    sent(h, 101)
    sent(h, 102)
    ctx = context(h)
    ctx.settings = replace(ctx.settings, critic_lines=4, critic_per_member=2)
    calls = []

    async def first(system, user, *args, **kwargs):
        calls.append(user)
        if "Arkon" in system:
            raise ValueError("Synthetic single-member failure")
        return answer(5, 5)

    monkeypatch.setattr(ctx.reasoner, "json", first)
    run(critic.stage_critic(ctx))
    plan = h.store.get_meta(critic._plan_key(ctx.night))
    assert ctx.done("critic", "102") and not ctx.done("critic", "101")
    sent(h, 103)
    sent(h, 101, text="Newly inserted historical line")

    async def succeeding(system, user, *args, **kwargs):
        calls.append(user)
        return answer(5, 5)

    monkeypatch.setattr(ctx.reasoner, "json", succeeding)
    run(critic.stage_critic(ctx))
    run(critic.stage_critic(ctx))
    assert len(calls) == 3 and calls[-1] in calls[:2]
    assert h.store.get_meta(critic._plan_key(ctx.night)) == plan
    assert h.count("critic_score") == 2
    assert h.store.conn.execute("SELECT sum(n) FROM critic_score").fetchone()[0] == 4
    assert not ctx.done("critic", "103")


def test_count_pending_is_read_only_and_respects_frozen_completed_units(h):
    sent(h, 101)
    sent(h, 102)
    ctx = context(h, answer(5, 5), answer(5, 5))
    before = h.store.conn.total_changes
    assert budget.COUNTERS["critic"](ctx) == critic.count_pending(ctx) == 2
    assert h.store.conn.total_changes == before
    assert h.store.get_meta(critic._plan_key(ctx.night)) is None
    run(critic.stage_critic(ctx))
    sent(h, 103)
    assert budget.COUNTERS["critic"](ctx) == critic.count_pending(ctx) == 0
