"""Independent guards for member-local feedback and private follow-up provenance."""
import json

import pytest

from conftest import run
from nightly.conftest import yes
from leeroy_mind.nightly import human_world


def sent(h, guid, words, *, t=None, kind="open", to=0):
    line = h.say(h.T if t is None else t, guid, words, kind=kind, to=to)
    row = h.store.conn.execute("SELECT * FROM utterance WHERE guid=? ORDER BY id DESC LIMIT 1",
                               (guid,)).fetchone()
    return line, row


@pytest.mark.parametrize("kind", ["open", "reply_human", "bit", "greet"])
def test_addressed_womans_non_flirt_kind_cannot_optimize_romance(h, kind):
    persona = h.woman(102, "drawn")
    _, utterance = sent(h, 102, "Testy, you are a legend.", kind=kind)
    h.store.conn.execute("UPDATE utterance SET why='reply:named' WHERE id=?", (utterance["id"],))
    utterance = h.store.conn.execute("SELECT * FROM utterance WHERE id=?", (utterance["id"],)).fetchone()
    assert human_world.arm_of(h.store, utterance, persona, h.human) is None


def test_followup_rejects_private_ancestor_behind_public_human_news(h):
    private = h.say(h.T, h.human, "My sister is sick.", channel="whisper", to=101)
    confidence = h.mem(101, "His sister is sick.", scope="secret", scope_with=h.human,
                       evidence=(f"chat:{private}",))
    public = h.say(h.T + 1, h.human, "Work has been busy.")
    h.store.conn.execute("UPDATE chat_line SET audience=? WHERE id=?", (json.dumps([101, 102, 103]), public))
    h.mem(101, "Work and his sick sister are keeping him busy.",
          evidence=(f"chat:{public}", f"memory:{confidence}"))
    for guid in (101, 102, 103):
        h.rel(guid, h.human, affinity=80)
    ctx = h.ctx({"followup": [{"question": "How is your sister doing?"}] * 3, "judge": [yes] * 3})
    run(human_world.stage_human_world(ctx))
    assert not ctx.failures
    for guid in (102, 103):
        assert not any((row.key or "").startswith("followup:") for row in h.rows(guid))
    for row in h.rows(101):
        if (row.key or "").startswith("followup:"):
            assert row.scope in {"private", "secret"} and row.scope_with == h.human


def test_public_mind_line_cannot_launder_recalled_confidence_into_followup(h):
    private = h.say(h.T, h.human, "Keep this between us: my sister is sick.", channel="whisper", to=101)
    confidence = h.mem(101, "His sister is sick.", scope="secret", scope_with=h.human,
                       evidence=(f"chat:{private}",))
    public, utterance = sent(h, 101, "That family news is on my mind.", t=h.T + 1)
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE id=?",
                         (json.dumps([confidence]), utterance["id"]))
    human = h.say(h.T + 2, h.human, "Work is hectic.")
    h.store.conn.execute("UPDATE chat_line SET audience=? WHERE id=?", (json.dumps([101, 102]), human))
    h.mem(101, "Work and his sister are keeping him busy.",
          evidence=(f"chat:{human}", f"chat:{public}"))
    h.rel(102, h.human, affinity=95)
    ctx = h.ctx({"followup": [{"question": "How is your sister doing?"}] * 3, "judge": [yes] * 3})
    run(human_world.stage_human_world(ctx))
    assert not ctx.failures
    assert not any((row.key or "").startswith("followup:") for row in h.rows(102))


def test_whisper_reply_is_excluded_even_when_its_turn_kind_is_reply_human(h):
    h.say(h.T, 101, "Testy, glad I could help.", channel="whisper", to=h.human, kind="reply_human")
    utterance = h.store.conn.execute("SELECT * FROM utterance WHERE guid=101 ORDER BY id DESC LIMIT 1").fetchone()
    assert human_world.arm_of(h.store, utterance, h.ctx().persona(101), h.human) is None


def test_bandit_weights_use_own_posteriors_bits_and_flattery_cap(h):
    context = "free:quiet:day"
    for guid, alpha, beta in ((101, 4, 2), (102, 500, 0)):
        h.store.conn.execute("INSERT INTO bandit(guid,arm,context,alpha,beta,n,updated) "
            "VALUES (?,'open',?,?,?,5,?)", (guid, context, alpha, beta, h.T))

    class Draws:
        def __init__(self):
            self.calls = []
            self.values = iter([0.99, 0.01, 0.01])

        def betavariate(self, alpha, beta):
            self.calls.append((alpha, beta))
            return next(self.values)

    rng = Draws()
    result = human_world.arm_weights(h.store, 101,
        ["flattery", "open", "bit:bit101", "bit:bit102", "whisper"], context, rng)
    assert set(result) == {"flattery", "open", "bit:bit101"}
    assert rng.calls == [(1, 1), (5, 3), (1, 1)]
    assert result["open"] == result["bit:bit101"] == pytest.approx(0.99 * 0.15)
    assert result["flattery"] <= 0.5 * max(result["open"], result["bit:bit101"])


@pytest.mark.parametrize("mutation", ["private", "rejected"])
def test_followup_rechecks_source_after_model_call(h, mutation):
    line = h.say(h.T, h.human, "Work has been hectic.")
    h.store.conn.execute("UPDATE chat_line SET audience='[101]' WHERE id=?", (line,))
    source = h.mem(101, "Work has been hectic for Testy.", evidence=(f"chat:{line}",))
    h.rel(101, h.human, affinity=90)

    def changed(_prompt):
        if mutation == "private":
            h.store.conn.execute("UPDATE chat_line SET channel='whisper',to_guid=102 WHERE id=?", (line,))
        else:
            h.store.conn.execute("UPDATE memory SET status='rejected' WHERE id=?", (source,))
        return {"question": "How is work going?"}

    ctx = h.ctx({"followup": [changed], "judge": [yes]})
    run(human_world.stage_human_world(ctx))
    assert ctx.failures["human_world"] == 1
    assert not ctx.done("human_world", f"follow:chat:{line}")
    assert not any((row.key or "").startswith("followup:") for row in h.rows(101))


def test_successful_lands_then_failed_bandits_retries_without_counting_twice(h, monkeypatch):
    h.session(h.T - 1, h.T + 1000)
    for n in range(5):
        h.say(h.T + n * 100, 101, "Testy, that pull was something.", kind="open")
        h.say(h.T + n * 100 + 1, h.human, "Arkon lol")
    ctx = h.ctx()
    bandits = human_world._bandits

    def fail_after_write(*args):
        bandits(*args)
        raise RuntimeError("independent failure after bandit updates")

    monkeypatch.setattr(human_world, "_bandits", fail_after_write)
    run(human_world.stage_human_world(ctx))
    assert ctx.done("human_world", "lands") and not ctx.done("human_world", "bandits")
    assert h.store.conn.execute("SELECT n FROM lands WHERE guid=101 AND kind='open'").fetchone()[0] == 5
    assert h.count("bandit") == 0
    first = h.store.keyed_memory(101, "lands:open")
    assert first is not None

    monkeypatch.setattr(human_world, "_bandits", bandits)
    run(human_world.stage_human_world(ctx))
    assert ctx.done("human_world", "bandits")
    assert h.store.conn.execute("SELECT n FROM lands WHERE guid=101 AND kind='open'").fetchone()[0] == 5
    assert h.store.conn.execute("SELECT sum(n) FROM bandit WHERE guid=101").fetchone()[0] == 5
    assert h.store.keyed_memory(101, "lands:open") == first


def test_overlap_keeps_first_reaction_attribution_and_counts_only_new_bid(h):
    h.session(h.T - 1, h.T + 1000)
    old, utterance = sent(h, 101, "Testy, quite a pull.")
    h.say(h.T + 1, h.human, "Arkon lol")
    first = h.ctx()
    run(human_world.stage_human_world(first))
    assert not first.failures
    # A legacy row correction must not move already-counted feedback into another arm.
    h.store.conn.execute("UPDATE utterance SET kind='greet' WHERE id=?", (utterance["id"],))
    h.say(h.T + 400, 101, "Testy, ready for another one?", kind="open")
    h.say(h.T + 401, h.human, "Arkon stop it")
    overlap = h.ctx(night="2026-10-30", window=first.window)
    run(human_world.stage_human_world(overlap))
    assert not overlap.failures
    counted = h.store.conn.execute("SELECT n,laughs,ignored FROM lands WHERE guid=101 AND kind='open'").fetchone()
    assert tuple(counted) == (2, 1, 1)
    assert h.store.conn.execute("SELECT 1 FROM lands WHERE guid=101 AND kind='greet'").fetchone() is None
    learned = h.store.conn.execute("SELECT n,alpha,beta FROM bandit WHERE guid=101 AND arm='open'").fetchone()
    assert tuple(learned) == (2, 1, 1.5)
    assert h.store.conn.execute("SELECT 1 FROM bandit WHERE guid=101 AND arm='greet'").fetchone() is None
