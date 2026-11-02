"""Independent regression checks for weekly receipts and legacy identity screening."""
import json
from types import SimpleNamespace

from conftest import run
from leeroy_mind.eval import interview as interviews, run_weekly, social


class FixedAsker:
    def __init__(self, h):
        self.ident = h.ident
        self.calls = []

    async def ask(self, guid, question):
        self.calls.append((guid, question))
        return SimpleNamespace(text="I like exploring with the guild.", memory_ids=[])


def test_interview_completed_row_with_changed_question_is_not_counted_complete(h):
    asker = FixedAsker(h)
    assert run(interviews.interview(h.store, asker, "2026-W44", [101]))["answered"] == 10
    with h.store.transaction():
        h.store.conn.execute("UPDATE eval_answer SET question='A different question' "
                             "WHERE week='2026-W44' AND kind='interview' AND qkey='q0'")
    before = tuple(h.store.conn.execute("SELECT * FROM eval_answer WHERE qkey='q0'").fetchone())
    result = run(interviews.interview(h.store, asker, "2026-W44", [101]))
    assert result["answered"] == 9 and result["pending"] == 1 and result["failed"] == 1
    assert len(asker.calls) == 10
    assert tuple(h.store.conn.execute("SELECT * FROM eval_answer WHERE qkey='q0'").fetchone()) == before


def test_interview_pending_row_with_changed_category_cannot_receive_a_new_answer(h):
    asker = FixedAsker(h)
    run(interviews.interview(h.store, asker, "2026-W44", [101]))
    with h.store.transaction():
        h.store.conn.execute("UPDATE eval_answer SET answer=NULL,category='people' "
                             "WHERE week='2026-W44' AND kind='interview' AND qkey='q0'")
    result = run(interviews.interview(h.store, asker, "2026-W44", [101]))
    assert result["pending"] == 1 and result["answered"] == 9
    assert len(asker.calls) == 10
    assert h.store.conn.execute("SELECT answer FROM eval_answer WHERE qkey='q0'").fetchone()[0] is None


def test_weekly_deterministic_report_screens_real_name_in_legacy_repeated_phrases(h):
    text = f"{h.ident.real_name} brings freshly baked bread"
    for day in range(3):
        h.say(h.T + day * 86400, 101, text)
    ctx = h.ctx()
    path = run(run_weekly(ctx, "2026-W44", use_llm=False))
    markdown = path.read_text(encoding="utf-8")
    raw = path.with_suffix(".json").read_text(encoding="utf-8")
    assert h.ident.real_name.casefold() not in markdown.casefold()
    assert h.ident.real_name.casefold() not in raw.casefold()
    report = json.loads(raw)
    assert report["noise"].get("status") != "failed"
    assert report["noise"]["repeated_phrases"]
    assert ctx.reasoner.llm.calls == []


def test_social_probe_report_uses_the_requested_model(h):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate,details) "
                             "VALUES (?,?,36,0,0,'[]')", (h.clock.now(), "another-live-model"))
    report = social.leaks(h.store, (h.T, h.T + 1), model=h.LIVE)
    assert report["probe"]["n"] == 36 and report["probe"]["rate"] == 0
    assert report["probe"]["gate_open"] is False


def test_weekly_probe_report_cannot_disagree_with_current_model_gate(h):
    with h.store.transaction():
        h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate,details) "
                             "VALUES (?,?,36,0,0,'[]')", (h.clock.now(), "another-live-model"))
    path = run(run_weekly(h.ctx(), "2026-W44", use_llm=False))
    report = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert report["leaks"]["probe"]["gate_open"] is False
