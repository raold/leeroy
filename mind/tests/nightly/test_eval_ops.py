"""The weekly evaluation traces an arc root, names what failed and survives one bad member."""
import logging
from types import SimpleNamespace

from conftest import run
from leeroy_mind.eval import interview as interviews
from leeroy_mind.eval.asker import ScratchRefused
from leeroy_mind.nightly import critic
from leeroy_mind.nightly.forget import _node
from leeroy_mind.nightly.knowledge import add_fact
from leeroy_mind.storage.provenance import ProvenanceError
from leeroy_mind.storage.rows import pack_vec

ROOT = "arc:102:weekend:2026-10-28"
PANEL = [101, 102, 103, 104, 105]


def noticed(h, root=ROOT):
    return add_fact(h.store, kind="noticed", root=root, about=102, text="The guild can tell.",
                    secret=False, now=h.T)


def test_an_arc_root_with_its_noticed_fact_is_a_complete_open_leaf(h):
    assert _node(h.store, ROOT) is None  # no fact: the root's record is missing
    noticed(h)
    value, children, holder, scope, parties = _node(h.store, ROOT)
    assert children == () and holder is None and scope == ("open", 0) and parties == frozenset()
    assert _node(h.store, "arc:102:weekend:yesterday") is None
    assert _node(h.store, "arc:102:weekend:2026-10-28:extra") is None


def test_a_memory_resting_on_an_arc_root_is_traceable(h):
    noticed(h)
    first = h.mem(101, "They noticed.", evidence=(ROOT,))
    second = h.mem(101, "A thought about it.", evidence=(f"memory:{first}",))
    saved = dict(critic._provenance(h.store, [f"memory:{second}"]))
    assert set(saved) == {ROOT, f"memory:{first}", f"memory:{second}"}


class Asker:
    """Member `bad` recalls a memory whose evidence cannot be traced; `flaky` loses the model."""

    def __init__(self, h, *, bad=(), flaky=(), refused=()):
        self.ident, self.bad, self.flaky, self.refused = h.ident, set(bad), set(flaky), set(refused)
        self.good = {g: h.mem(g, f"A remembered raid of {g}.", evidence=()) for g in PANEL}
        self.broken = {g: h.mem(g, f"An untraceable thought of {g}.", evidence=("arc:9:after:2026-10-01",))
                       for g in self.bad}

    async def ask(self, guid, question):
        if guid in self.flaky:
            raise RuntimeError("synthetic model outage with a private payload")
        if guid in self.refused:
            raise ScratchRefused("prompt_budget", "synthetic")
        memory = self.broken[guid] if guid in self.bad else self.good[guid]
        return SimpleNamespace(text="an answer", memory_ids=[memory])


def test_one_untraceable_member_is_named_and_does_not_fail_the_week(h, caplog):
    asker = Asker(h, bad=[103])
    with caplog.at_level(logging.WARNING):
        result = run(interviews.interview(h.store, asker, "2026-W44", PANEL))
    assert (result["expected"], result["answered"], result["pending"], result["failed"]) == (50, 40, 10, 10)
    assert result["excluded"] == [103] and result["excused"] == 10
    assert result["failed_codes"] == {"provenance": 10}
    assert result["failed_members"] == {"103": {"provenance": 10}}
    assert interviews.excluded(h.store, "2026-W44") == {103}
    lines = [r.getMessage() for r in caplog.records if "eval_interview_failed" in r.getMessage()]
    assert len(lines) == 1 and "guid=103" in lines[0] and "provenance" in lines[0]
    assert ProvenanceError.__name__ in lines[0] and "untraceable" not in lines[0]


def test_a_fault_that_hits_many_members_is_not_excused(h):
    result = run(interviews.interview(h.store, Asker(h, bad=[102, 103]), "2026-W44", PANEL))
    assert result["excluded"] == [] and result["excused"] == 0 and result["pending"] == 20
    assert set(result["failed_members"]) == {"102", "103"}


def test_a_model_outage_is_coded_and_never_excuses_the_member(h, caplog):
    with caplog.at_level(logging.WARNING):
        result = run(interviews.interview(h.store, Asker(h, flaky=[104]), "2026-W44", PANEL))
    assert result["excluded"] == [] and result["failed_codes"] == {"error": 10}
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in text and "private payload" not in text


def test_a_refusal_carries_its_own_code_and_a_later_night_can_clear_the_exclusion(h):
    asker = Asker(h, refused=[105])
    first = run(interviews.interview(h.store, asker, "2026-W44", PANEL))
    assert first["failed_codes"] == {"prompt_budget": 10} and first["excluded"] == [105]
    asker.refused.clear()
    second = run(interviews.interview(h.store, asker, "2026-W44", PANEL))
    assert second["pending"] == 0 and second["excluded"] == []
    assert interviews.excluded(h.store, "2026-W44") == frozenset()


def test_stability_measures_neither_week_of_an_excluded_member(h, monkeypatch):
    run(interviews.interview(h.store, Asker(h), "2026-W43", PANEL))
    run(interviews.interview(h.store, Asker(h, bad=[103]), "2026-W44", PANEL))
    ctx = h.ctx()

    async def embed(texts):
        return [pack_vec([1, 0]) for _ in texts]

    monkeypatch.setattr(ctx, "embed", embed)
    result = run(interviews.stability(ctx, "2026-W44", "2026-W43"))
    assert result["excluded_members"] == [103]
    assert result["trait_pairs"] == result["expected_trait_pairs"] == 20
    assert result["people_pairs"] == result["expected_people_pairs"] == 20
    assert result["expected_trait_answers"] == 20 and result["people_answers"] == 20
