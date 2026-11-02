"""Life follow-ups, individual feedback, privacy, and atomic nightly receipts."""
import json
import random
from datetime import datetime, timezone

import pytest

from conftest import run
from nightly.conftest import no_real_name, yes
from leeroy_mind.nightly import human_world as hw, knowledge


def execute(h, replies=None, **kwargs):
    ctx = h.ctx(replies, default=yes, **kwargs)
    run(hw.stage_human_world(ctx))
    assert not ctx.failures
    return ctx


def heard(h, line, members=(101, 102, 103, 104, 105)):
    h.store.conn.execute("UPDATE chat_line SET audience=? WHERE id=?", (json.dumps(members), line))


def fact(h, *, text="My sister visits this weekend.", owner=101, channel="guild", to=0, at=None):
    line = h.say(h.T if at is None else at, h.human, text, channel=channel, to=to)
    heard(h, line)
    memory = h.mem(owner, text, evidence=(f"chat:{line}",),
                   scope="private" if channel == "whisper" else "open",
                   scope_with=h.human if channel == "whisper" else 0)
    return line, memory


def keyed(h, guid, key):
    return h.store.keyed_memory(guid, key)


def reaction(h, n, *, guid=101, kind="open", result="laugh", channel="guild", addressed=True,
             bit=None, at=None):
    at = h.T + n * 400 if at is None else at
    text = ("Testy, " if addressed else "") + f"that pull was something ({n})."
    bid = h.say(at, guid, text, kind=kind, channel=channel, to=h.human if channel == "whisper" else 0)
    heard(h, bid)
    utter = h.store.conn.execute("SELECT id FROM utterance ORDER BY id DESC LIMIT 1").fetchone()[0]
    if bit is not None:
        h.store.conn.execute("UPDATE utterance SET bit=? WHERE id=?", (bit, utter))
    if result != "none":
        words = "lol" if result == "laugh" else "stop it" if result == "rebuff" else "okay"
        reply_channel = "whisper" if result == "whisper" else channel
        to = guid if reply_channel == "whisper" else 0
        if result == "ignored":
            words, reply_channel, to = "Coros, one moment", "officer", 0
        answer = h.say(at + 2, h.human, words, channel=reply_channel, to=to)
        heard(h, answer)
    return bid, utter


def live_night(h):
    return h.session(h.T - 60, h.T + 8 * 3600)


def table(h, table, where="", params=()):
    return [dict(r) for r in h.store.conn.execute(f"SELECT * FROM {table} {where}", params)]


def test_life_facts_newest_active_one_per_root_and_human_only(h):
    first, _ = fact(h, at=h.T - 3)
    newer = h.mem(102, "His sister visits.", t=h.T + 1, evidence=(f"chat:{first}",))
    second, latest = fact(h, text="My job interview went well.", at=h.T + 2)
    h.store.conn.execute("UPDATE memory SET t_created=? WHERE id=?", (h.T + 2, latest))
    other = h.say(h.T, 103, "My sister is visiting.")
    h.mem(103, "A sister visits.", evidence=(f"chat:{other}",))
    h.mem(0, "My job interview.", evidence=(f"chat:{second}",))
    h.mem(104, "My job interview.", status="archived", evidence=(f"chat:{second}",))
    rows = hw.his_life_facts(h.store, h.ctx().window, h.human, limit=3)
    assert [r.id for r in rows] == [latest, newer]
    assert hw.his_life_facts(h.store, h.ctx().window, h.human, limit=0) == []


def test_followups_for_close_members_only_and_sent_followup_archives_next_night(h):
    line, _ = fact(h)
    h.mem(102, "His sister visits this weekend.", evidence=(f"chat:{line}",))
    h.rel(101, h.human, affinity=50)
    h.rel(104, h.human, affinity=70)
    ctx = execute(h, {"followup": [{"question": "how was your sister's visit?"}]})
    key = f"followup:chat:{line}"
    assert {g for g in range(101, 106) if keyed(h, g, key)} == {101, 104}
    for guid in (101, 104):
        row = keyed(h, guid, key)
        assert row.text == "Ask Testy: how was your sister's visit?"
        assert (row.kind, row.source, row.importance, row.scope) == ("plan", "reflection", 6, "open")
        assert row.embedding is not None
    run(hw.stage_human_world(ctx))
    assert ctx.reasoner.calls == 2  # generating and grounding, never repeated
    h.utter(h.T + 100, 101, "How was your sister's visit?", [keyed(h, 101, key).id])
    h.utter(h.T + 100, 104, "Unsent question", [keyed(h, 104, key).id], dropped="screen")
    execute(h, night="2026-10-30")
    assert keyed(h, 101, key).status == "archived"
    assert keyed(h, 104, key).status == "active"


def test_whisper_followup_goes_only_to_recipient_despite_public_audience(h):
    line, _ = fact(h, text="Got the job btw.", channel="whisper", to=101)
    for guid in (101, 104):
        h.rel(guid, h.human, affinity=90)
    execute(h, {"followup": [{"question": "how is the new job?"}]})
    key = f"followup:chat:{line}"
    row = keyed(h, 101, key)
    assert row is not None and (row.scope, row.scope_with) == ("private", h.human)
    assert keyed(h, 104, key) is None
    assert knowledge.scope_leaks(h.store) == []


def test_followup_other_cast_name_is_rejected_even_when_fact_owner(h):
    line, _ = fact(h)
    h.rel(101, h.human, affinity=80)
    ctx = execute(h, {"followup": [{"question": "Did Arkon meet your sister?"}]})
    assert keyed(h, 101, f"followup:chat:{line}") is None
    assert ctx.reasoner.calls == 1


def test_stale_followup_archived(h):
    mid = h.mem(101, "Ask Testy: how is work?", t=h.T - 8 * 86400,
                key="followup:chat:999", kind="plan")
    execute(h)
    assert h.store.get_memories([mid])[0].status == "archived"


def test_absence_noticed_and_updated_without_attraction_changes(h):
    h.session(h.T - 4 * 86400, h.T - 3.5 * 86400)
    h.rel(101, h.human, affinity=50, attraction=42)
    execute(h)
    row = next(r for r in h.rows(101) if (r.key or "").startswith("absent:"))
    assert row.text == "Testy hasn't been on in 3 days."
    assert (row.kind, row.source, row.valence, row.importance) == ("observation", "game", -.2, 5)
    assert not any((r.key or "").startswith("absent:") for r in h.rows(102))
    h.clock.advance(86400)
    execute(h, night="2026-10-30")
    updated = keyed(h, 101, row.key)
    assert updated.id == row.id and updated.text == "Testy hasn't been on in 4 days."
    assert h.store.get_relationship(101, h.human).attraction == 42


@pytest.mark.parametrize("online", [False, True])
def test_recent_or_open_session_suppresses_absence(h, online):
    h.rel(101, h.human, affinity=90)
    h.session(h.T - 5 * 86400, h.T - 4 * 86400)
    if online:
        h.store.start_session(h.T - 30)
    else:
        h.session(h.T - 3 * 86400, h.T - 2 * 86400)
    execute(h)
    assert not h.rows(101)


def test_lands_rates_and_memories_keep_private_feedback_private(h):
    live_night(h)
    for n in range(5):
        reaction(h, n, kind="bit", bit="bit101", result="laugh" if n < 3 else "none")
    for n in range(5, 13):
        reaction(h, n, guid=102, kind="whisper", channel="whisper", result="ignored")
    execute(h)
    counts = table(h, "lands", "WHERE guid=101 AND kind='bit'")[0]
    assert (counts["n"], counts["laughs"]) == (5, 3)
    assert keyed(h, 101, "lands:bit").text == "Testy usually laughs at my running jokes."
    private = keyed(h, 102, "lands:whisper")
    assert private.text == "My whispers don't land with Testy."
    assert (private.scope, private.scope_with) == ("private", h.human)
    assert len(private.evidence) == 3
    assert knowledge.scope_leaks(h.store) == []


def test_private_reply_to_public_bid_cannot_make_public_lands_memory(h):
    live_night(h)
    for n in range(5):
        reaction(h, n, result="whisper")
    execute(h)
    memory = keyed(h, 101, "lands:open")
    assert memory.text == "Testy usually answers my stories."
    assert (memory.scope, memory.scope_with) == ("private", h.human)
    assert table(h, "lands_profile") == []


def test_bandits_are_per_member_own_bits_and_none_does_not_teach(h):
    live_night(h)
    for n in range(4):
        reaction(h, n, kind="bit", bit="bit101")
    reaction(h, 4, result="rebuff")
    reaction(h, 5, result="ignored")
    reaction(h, 6, addressed=False, result="none")
    execute(h)
    bit = table(h, "bandit", "WHERE arm='bit:bit101'")[0]
    assert (bit["guid"], bit["alpha"], bit["n"]) == (101, 4, 4)
    open_rows = table(h, "bandit", "WHERE arm='open'")
    assert sum(r["beta"] for r in open_rows) == 2 and sum(r["n"] for r in open_rows) == 2
    assert hw.arm_weights(h.store, 102, ["open", "bit:bit101"], "free:quiet:day", random.Random(1)).keys() == {"open"}
    assert table(h, "bandit", "WHERE guid=102") == []


def test_exploration_floor_and_flattery_cap_over_seeded_draws(h):
    for arm, alpha, beta in (("open", 40, 0), ("aside", 0, 40), ("flattery", 100, 0)):
        h.store.conn.execute("INSERT INTO bandit(guid,arm,context,alpha,beta,n,updated) VALUES (101,?,?,?,?,50,0)",
                             (arm, "free:quiet:day", alpha, beta))
    for seed in range(100):
        weights = hw.arm_weights(h.store, 101, ["open", "aside", "flattery"], "free:quiet:day", random.Random(seed))
        assert weights["aside"] >= hw.EXPLORE_FLOOR * max(weights.values())
        assert weights["flattery"] <= hw.FLATTERY_CAP * max(weights["open"], weights["aside"])
    assert hw.arm_weights(h.store, 101, ["flattery"], "free:quiet:day", random.Random(0)) == {"flattery": 0}


def test_flattery_and_romance_exclusions_use_actual_addressing(h):
    live_night(h)
    woman = h.woman(102, "lust")
    for n, kind in enumerate(("flirt", "whisper", "open", "reply_human")):
        _, utter = reaction(h, n, guid=102, kind=kind)
        assert hw.arm_of(h.store, utter, woman, h.human, called=h.ident.called) is None
    reaction(h, 4, guid=102, addressed=False)
    bid = h.say(h.T + 5 * 400, 101, "Testy, you're the best gm")
    heard(h, bid)
    utter = h.store.conn.execute("SELECT id FROM utterance ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert hw.arm_of(h.store, utter, h.persona(101), h.human, called=h.ident.called) == "flattery"
    execute(h)
    rows = table(h, "bandit", "WHERE guid=102")
    assert len(rows) == 1 and rows[0]["arm"] == "open" and rows[0]["n"] == 1


def test_context_uses_historical_group_chat_and_local_hour(h):
    late = datetime(2026, 10, 30, 3, tzinfo=timezone.utc).timestamp()
    h.group(late - 100, [h.human, 101], raid=True)
    for n in range(3):
        h.say(late - 30 + n, h.human, "ready")
    h.group(late + 1, [], raid=False)
    assert hw.context_key(h.store, late, h.human) == "raid:busy:late"
    assert hw.context_key(h.store, late + 700, h.human) == "free:quiet:late"
    assert hw.context_key(h.store, h.T - 100, h.human) == "free:quiet:day"


def test_profile_once_with_cited_public_lines(h):
    live_night(h)
    for n in range(12):
        reaction(h, n, guid=101 if n < 6 else 102)
    ctx = execute(h, {"lands_profile": [{"profile": "Testy answers Arkon's stories.", "evidence": [1, 2, 3, 4]}]})
    row = table(h, "lands_profile")[0]
    assert row["night"] == ctx.night and row["text"] == "Testy answers Arkon's stories."
    refs = json.loads(row["evidence"])
    assert len([r for r in refs if r.startswith("chat:")]) == 24
    run(hw.stage_human_world(ctx))
    assert len(table(h, "lands_profile")) == 1 and ctx.reasoner.calls == 2
    assert no_real_name(h.store) == []


def test_profile_rejects_member_absent_from_evidence(h):
    live_night(h)
    for n in range(12):
        reaction(h, n)
    execute(h, {"lands_profile": [{"profile": "Testy likes Coros's stories.", "evidence": [1]}]})
    assert table(h, "lands_profile") == []


@pytest.mark.parametrize("bad", ["dropped", "missing", "private_nested"])
def test_reactions_reject_unsent_or_incomplete_sources(h, bad):
    live_night(h)
    bid, utter = reaction(h, 0)
    if bad == "dropped":
        h.store.conn.execute("UPDATE utterance SET dropped='screen' WHERE id=?", (utter,))
    elif bad == "missing":
        h.store.conn.execute("UPDATE utterance SET memory_ids='[99999]' WHERE id=?", (utter,))
    else:
        private = h.say(h.T - 10, h.human, "Secret job news.", channel="whisper", to=101)
        memory = h.mem(101, "Secret job news.", evidence=(f"chat:{private}",), scope="private", scope_with=1)
        h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE id=?", (json.dumps([memory]), utter))
    execute(h)
    if bad == "private_nested":
        assert table(h, "lands")[0]["n"] == 1  # aggregate counts are allowed, public profile is not
        assert table(h, "lands_profile") == []
    else:
        assert table(h, "lands") == [] and table(h, "bandit") == []


def test_failed_memory_write_rolls_back_counts_and_receipts_then_retry_counts_once(h, monkeypatch):
    live_night(h)
    for n in range(5):
        reaction(h, n)
    ctx = h.ctx(default=yes)
    original = ctx.writer.put
    monkeypatch.setattr(ctx.writer, "put", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("write failed")))
    run(hw.stage_human_world(ctx))
    assert not ctx.done(hw.STAGE, "lands") and table(h, "lands") == []
    assert h.store.get_meta(hw._PREFIX + "lands:1") is None
    monkeypatch.setattr(ctx.writer, "put", original)
    run(hw.stage_human_world(ctx))
    assert ctx.done(hw.STAGE, "lands")
    assert table(h, "lands", "WHERE kind='open'")[0]["n"] == 5
    assert table(h, "bandit")[0]["n"] == 5


def test_resume_freezes_input_window_and_new_arrivals(h):
    live_night(h)
    reaction(h, 0)
    ctx = execute(h)
    reaction(h, 1)
    run(hw.stage_human_world(ctx))
    assert table(h, "lands", "WHERE kind='open'")[0]["n"] == 1
    with pytest.raises(ValueError, match="window changed"):
        run(hw.stage_human_world(h.ctx(window=(ctx.window[0] - 1, ctx.window[1]))))


def test_followup_mutation_during_embedding_is_revalidated(h, monkeypatch):
    line, memory = fact(h)
    h.rel(101, h.human, affinity=90)
    ctx = h.ctx({"followup": [{"question": "how was your sister's visit?"}]}, default=yes)
    embed = ctx.embed

    async def changing(texts):
        result = await embed(texts)
        h.store.conn.execute("UPDATE memory SET status='archived' WHERE id=?", (memory,))
        return result

    monkeypatch.setattr(ctx, "embed", changing)
    run(hw.stage_human_world(ctx))
    assert ctx.failures[hw.STAGE] == 1
    assert keyed(h, 101, f"followup:chat:{line}") is None
    assert not ctx.done(hw.STAGE, f"follow:chat:{line}")


def test_profile_revalidates_current_romance_eligibility_after_model(h):
    live_night(h)
    h.woman(102, "none")
    for n in range(12):
        reaction(h, n, guid=102)

    def answer(_):
        h.woman(102, "drawn")
        return {"profile": "Testy answers Belra's stories.", "evidence": [1]}

    ctx = h.ctx({"lands_profile": [answer]}, default=yes)
    run(hw.stage_human_world(ctx))
    assert ctx.failures[hw.STAGE] == 1 and not ctx.done(hw.STAGE, "profile")
    assert table(h, "lands_profile") == []


def pass_probe(h):
    h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate) VALUES (?,?,36,0,0)",
                         (h.clock.now(), h.LIVE))


def test_secret_followup_waits_without_prompt_then_resumes_after_probe(h):
    line, _ = fact(h, text="Don't tell anyone, I got the job.", channel="whisper", to=101)
    h.rel(101, h.human, affinity=90)
    ctx = h.ctx({"followup": [{"question": "how is the job?"}]}, default=yes)
    run(hw.stage_human_world(ctx))
    assert not ctx.failures and ctx.reasoner.calls == 0
    assert not ctx.done(hw.STAGE, f"follow:chat:{line}")
    assert keyed(h, 101, f"followup:chat:{line}") is None
    pass_probe(h)
    run(hw.stage_human_world(ctx))
    assert ctx.done(hw.STAGE, f"follow:chat:{line}") and ctx.reasoner.calls == 2
    row = keyed(h, 101, f"followup:chat:{line}")
    assert (row.scope, row.scope_with) == ("secret", h.human)


def test_probe_revoked_during_followup_prevents_judging_and_commit(h):
    line, _ = fact(h, text="Don't tell anyone, I got the job.", channel="whisper", to=101)
    h.rel(101, h.human, affinity=90)
    pass_probe(h)

    def revoke(_):
        h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate) VALUES (?,?,36,36,1)",
                             (h.clock.now() + 1, h.LIVE))
        return {"question": "how is the job?"}

    ctx = h.ctx({"followup": [revoke]}, default=yes)
    run(hw.stage_human_world(ctx))
    assert ctx.failures[hw.STAGE] == 1 and ctx.reasoner.calls == 1
    assert keyed(h, 101, f"followup:chat:{line}") is None


def test_secret_lands_memory_defers_without_recounting_after_probe(h):
    live_night(h)
    for n in range(5):
        reaction(h, n, result="whisper")
    h.store.conn.execute("UPDATE chat_line SET text='Don''t tell anyone, okay' "
                         "WHERE origin='human' AND channel='whisper'")
    execute(h)
    assert keyed(h, 101, "lands:open") is None
    assert table(h, "lands", "WHERE kind='open'")[0]["n"] == 5
    pass_probe(h)
    execute(h, night="2026-10-30")
    row = keyed(h, 101, "lands:open")
    assert (row.scope, row.scope_with) == ("secret", h.human)
    assert table(h, "lands", "WHERE kind='open'")[0]["n"] == 5


def test_unmapped_kinds_count_only_toward_all(h):
    live_night(h)
    reaction(h, 0, kind="callout")
    execute(h)
    assert [(r["kind"], r["n"]) for r in table(h, "lands")] == [("*", 1)]
