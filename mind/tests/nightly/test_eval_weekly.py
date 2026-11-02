"""Weekly ordering, durable probes, partial measurements, cancellation and gate binding."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind import eval as weekly
from leeroy_mind.eval.asker import AskResult
from leeroy_mind.eval.probes import PROBE_SAMPLES, SECRET_PROBES, ProbeResult, record_probe
from leeroy_mind.eval.quiz import QuizItem
from leeroy_mind.llm.client import LLMError
from leeroy_mind.nightly.knowledge import secret_probe_passed

WEEK = "2026-W44"


class Asker:
    def __init__(self, ident, *, fail_first=False):
        self.ident, self.fail_first = ident, fail_first
        self.calls = []
        self.closed = False

    async def ask(self, guid, question):
        self.calls.append((guid, question))
        if self.fail_first:
            self.fail_first = False
            raise LLMError("private model response Quillon")
        return AskResult("3" if guid == 101 else "I don't remember that kill.", [])

    def close(self):
        self.closed = True


def setup(h, monkeypatch, *, replies=None, fail_first=False):
    ctx = h.ctx({"quiz_judge": replies or [{"verdict": "correct"}, {"verdict": "abstained"}]})
    # Task23-only cases disable the additional interview workload explicitly.
    ctx.settings = replace(ctx.settings, interview_members=0)
    ctx.asker = Asker(h.ident, fail_first=fail_first)
    models = []

    async def ensure(model):
        models.append(model)

    ctx.arbiter = SimpleNamespace(ensure_model=ensure)
    monkeypatch.setattr(weekly, "build_quiz", lambda *_: [
        QuizItem("single_session", 101, "one", "How many wipes on that pull?", "3"),
        QuizItem("abstention", 102, "two", "Remember that kill?", None)])
    return ctx, models


def sections(path):
    return json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))


def receipt(h):
    raw = h.store.get_meta(f"nightly:eval-result:{WEEK}")
    return None if raw is None else json.loads(raw)


def full_probe(leaks=0):
    details = [{"id": probe.id, "sample": sample, "leaked": False}
               for probe in SECRET_PROBES for sample in range(1, PROBE_SAMPLES + 1)]
    for detail in details[:leaks]:
        detail["leaked"] = True
    return ProbeResult(36, leaks, leaks / 36, details)


@pytest.mark.parametrize("use_llm,with_asker,reason,complete", [
    (False, True, "llm_disabled", True), (True, False, "asker_unavailable", False)])
def test_deterministic_run_marks_missing_model_sections_honestly(h, monkeypatch, use_llm, with_asker, reason, complete):
    ctx, models = setup(h, monkeypatch)
    if not with_asker:
        ctx.asker = None
    path = run(weekly.run_weekly(ctx, WEEK, use_llm=use_llm))
    assert path == h.settings.data_dir / "reports" / f"{WEEK}.md"
    assert "_skipped_" in path.read_text(encoding="utf-8")
    report = sections(path)
    assert set(report) == {"secret_probe", "memory_quiz", "interview", "consistency", "distinctiveness",
        "stability", "grounding_audit", "night_health", "texture", "callbacks", "noise", "cast_mix",
        "signals", "leaks", "drift"}
    assert report["secret_probe"]["reason"] == report["memory_quiz"]["reason"] == reason
    assert all(report[name]["reason"] == reason for name in ("interview", "consistency", "distinctiveness", "stability"))
    assert models == [] and ctx.reasoner.llm.calls == [] and h.count("probe_run") == 0
    assert receipt(h) == {"complete": complete, "status": "complete" if complete else "partial", "requested_llm": use_llm}


def test_weekly_orders_models_records_completed_probe_and_never_closes_asker(h, monkeypatch):
    ctx, models = setup(h, monkeypatch)
    path = run(weekly.run_weekly(ctx, WEEK))
    report = sections(path)
    assert models == [h.LIVE, h.REFL]
    assert len(ctx.reasoner.llm.calls) == 38  # 36 live probes and 2 reflection judgments
    assert report["secret_probe"] == {"status": "complete", "n": 36, "leaks": 0, "rate": 0,
        "gate_open": True, "reused": False, "leaked_probes": []}
    assert report["memory_quiz"]["status"] == "complete"
    assert report["memory_quiz"]["pending_n"] == 0 and report["memory_quiz"]["judged_n"] == 2
    assert receipt(h)["complete"] and not ctx.asker.closed
    assert "Quillon" not in path.with_suffix(".json").read_text(encoding="utf-8")
    run(weekly.run_weekly(ctx, WEEK))
    assert h.count("probe_run") == 1 and len(ctx.asker.calls) == 2 and len(ctx.reasoner.llm.calls) == 38
    assert sections(path)["secret_probe"]["reused"] is True


def test_partial_quiz_preserves_missing_denominators_and_resumes_without_new_probes(h, monkeypatch):
    ctx, _ = setup(h, monkeypatch, fail_first=True,
                   replies=[{"verdict": "abstained"}, {"verdict": "correct"}])
    path = run(weekly.run_weekly(ctx, WEEK))
    quiz = sections(path)["memory_quiz"]
    assert quiz["status"] == "partial" and quiz["pending_n"] == 1
    assert quiz["accuracy"]["single_session"] is None and quiz["accuracy_n"]["single_session"] == 0
    assert receipt(h)["complete"] is False and h.count("probe_run") == 1
    run(weekly.run_weekly(ctx, WEEK))
    assert sections(path)["memory_quiz"]["status"] == "complete" and receipt(h)["complete"]
    assert h.count("probe_run") == 1 and len(ctx.asker.calls) == 3


def test_probe_failure_does_not_write_a_gate_receipt_but_quiz_and_audits_continue(h, monkeypatch):
    ctx, models = setup(h, monkeypatch)

    async def failed(*_):
        raise LLMError("Quillon is private")

    monkeypatch.setattr(weekly, "run_secret_probes", failed)
    path = run(weekly.run_weekly(ctx, WEEK))
    report = sections(path)
    assert report["secret_probe"] == {"status": "failed", "error_type": "LLMError", "gate_open": False}
    assert report["memory_quiz"]["status"] == "complete" and models == [h.LIVE, h.REFL]
    assert h.count("probe_run") == 0 and h.store.get_meta(f"eval:secret-probe:{WEEK}") is None
    assert not receipt(h)["complete"] and "Quillon" not in json.dumps(report)


def test_incomplete_result_and_atomic_receipt_failure_cannot_open_gate(h, monkeypatch):
    ctx, _ = setup(h, monkeypatch)

    async def incomplete(*_):
        result = full_probe()
        result.n, result.details = 35, result.details[:-1]
        return result

    monkeypatch.setattr(weekly, "run_secret_probes", incomplete)
    path = run(weekly.run_weekly(ctx, WEEK))
    assert sections(path)["secret_probe"]["status"] == "failed" and h.count("probe_run") == 0
    monkeypatch.setattr(weekly, "run_secret_probes", lambda *_: async_result(full_probe()))
    original = h.store.set_meta

    def fail_meta(key, value):
        if key.startswith("eval:secret-probe:"):
            raise RuntimeError("receipt failure")
        return original(key, value)

    monkeypatch.setattr(h.store, "set_meta", fail_meta)
    run(weekly.run_weekly(ctx, WEEK))
    assert h.count("probe_run") == 0 and not secret_probe_passed(h.store, now=h.T)


async def async_result(value):
    return value


def test_changed_live_model_invalidates_probe_receipt(h, monkeypatch):
    ctx, _ = setup(h, monkeypatch)
    run(weekly.run_weekly(ctx, WEEK))
    ctx.settings = replace(ctx.settings, live_model="new-live")
    path = run(weekly.run_weekly(ctx, WEEK))
    assert h.count("probe_run") == 2 and not sections(path)["secret_probe"]["reused"]
    assert secret_probe_passed(h.store, now=h.T, model="new-live")
    assert not secret_probe_passed(h.store, now=h.T, model=h.LIVE)


def test_model_change_during_probe_cannot_record_result_under_new_model(h, monkeypatch):
    ctx, _ = setup(h, monkeypatch)

    async def changed(*_):
        ctx.settings = replace(ctx.settings, live_model="different-model")
        return full_probe()

    monkeypatch.setattr(weekly, "run_secret_probes", changed)
    path = run(weekly.run_weekly(ctx, WEEK))
    assert sections(path)["secret_probe"]["status"] == "failed" and h.count("probe_run") == 0


@pytest.mark.parametrize("bad", ["../2026-W44", "2026-W4", "2026-W00", "2026-W99", "2025-W53", "2026-W44/extra", None])
def test_invalid_week_has_no_side_effects(h, monkeypatch, bad):
    ctx, models = setup(h, monkeypatch)
    with pytest.raises(ValueError):
        run(weekly.run_weekly(ctx, bad))
    assert models == [] and h.count("probe_run") == h.count("eval_answer") == 0
    assert not (h.settings.data_dir / "reports").exists()


@pytest.mark.parametrize("stage", ["probe", "judge"])
def test_cancellation_propagates_without_report_or_completion_receipt(h, monkeypatch, stage):
    ctx, models = setup(h, monkeypatch)

    async def go():
        entered = asyncio.Event()

        async def held(*_):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(weekly, "run_secret_probes" if stage == "probe" else "judge_all", held)
        task = asyncio.create_task(weekly.run_weekly(ctx, WEEK))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(go())
    assert receipt(h) is None and not (h.settings.data_dir / "reports" / f"{WEEK}.md").exists()
    assert h.count("probe_run") == (0 if stage == "probe" else 1)
    assert not ctx.asker.closed


def test_model_admission_failure_still_produces_deterministic_partial_report(h, monkeypatch):
    ctx, _ = setup(h, monkeypatch)

    async def failed(_):
        raise RuntimeError("private local path")

    ctx.arbiter = SimpleNamespace(ensure_model=failed)
    path = run(weekly.run_weekly(ctx, WEEK))
    report = sections(path)
    assert report["secret_probe"]["status"] == "failed"
    assert report["memory_quiz"]["reason"] == "live_model_unavailable"
    assert "by_stage" in report["grounding_audit"] and not receipt(h)["complete"]
    assert ctx.asker.calls == [] and ctx.reasoner.llm.calls == []


def test_failed_deterministic_section_is_not_a_completed_explicit_non_llm_run(h, monkeypatch):
    ctx, _ = setup(h, monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(weekly, "grounding_audit", fail)
    path = run(weekly.run_weekly(ctx, WEEK, use_llm=False))
    assert sections(path)["grounding_audit"]["status"] == "failed"
    assert receipt(h) == {"complete": False, "status": "partial", "requested_llm": False}


def test_gate_requires_current_model_but_preserves_unbound_api(h):
    record_probe(h.store, full_probe(), model="registry:5000/guild", now=h.T)
    assert secret_probe_passed(h.store, now=h.T)
    assert secret_probe_passed(h.store, now=h.T, model="registry:5000/guild:latest")
    assert not secret_probe_passed(h.store, now=h.T, model="another-model")
    record_probe(h.store, full_probe(), model="another-model", now=h.T + 1)
    assert not secret_probe_passed(h.store, now=h.T + 1, model="registry:5000/guild")


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_probe_age_fails_closed(h, now):
    record_probe(h.store, full_probe(), model=h.LIVE, now=h.T)
    assert not secret_probe_passed(h.store, now=now, model=h.LIVE)


def test_future_latest_receipt_does_not_fall_back_to_older_success(h):
    record_probe(h.store, full_probe(), model=h.LIVE, now=h.T)
    record_probe(h.store, full_probe(), model=h.LIVE, now=h.T + 1)
    assert not secret_probe_passed(h.store, now=h.T, model=h.LIVE)
    assert secret_probe_passed(h.store, now=h.T + 1, model=h.LIVE)


def task24_setup(h, monkeypatch, consistency_replies):
    base, models = setup(h, monkeypatch)
    ctx = h.ctx({"quiz_judge": [{"verdict": "correct"}, {"verdict": "abstained"}],
                 "consistency": consistency_replies, "who_said": [{"pick": 1}] * 3})
    ctx.settings = replace(ctx.settings, interview_members=1)
    ctx.asker, ctx.arbiter = base.asker, base.arbiter
    for n in range(3):
        h.say(h.T + n, 101, f"The boots have opinions about pull {n}.")
    return ctx, models


def test_task24_live_interview_precedes_reflection_and_all_sections_are_reported(h, monkeypatch):
    ctx, models = task24_setup(h, monkeypatch, [{"scores": [{"i": i, "score": 5} for i in range(3)]}])
    ask = ctx.asker.ask

    async def live_only(guid, question):
        assert models == [h.LIVE]
        return await ask(guid, question)

    monkeypatch.setattr(ctx.asker, "ask", live_only)
    path = run(weekly.run_weekly(ctx, WEEK))
    report = sections(path)
    assert models == [h.LIVE, h.REFL] and len(ctx.asker.calls) == 12
    assert report["interview"]["status"] == "complete" and report["interview"]["answered"] == 10
    assert report["consistency"]["members"] == {"101": 1}
    assert report["consistency"]["sampled_lines"] == report["consistency"]["scored_lines"] == 3
    assert report["distinctiveness"]["trials_scored"] == 3
    assert report["stability"]["reason"] == "no_previous_answers"
    assert report["stability"]["trait_similarity"] is None
    assert report["drift"] == {"status": "complete", "rolled_back": []}
    assert report["callbacks"]["lines"] == 3 and report["noise"]["online_hours"] == 0
    assert report["cast_mix"]["rebalance_start"] == {"lust": 13, "drawn": 19, "fond": 26, "none": 6}
    assert receipt(h)["complete"] is True


def test_partial_consistency_does_not_freeze_drift_receipt_and_retry_rolls_back(h, monkeypatch):
    ctx, _ = task24_setup(h, monkeypatch, [{"scores": []},
        {"scores": [{"i": i, "score": 2} for i in range(3)]}])
    h.store.put_state(replace(h.store.get_state(101), self_notes=(
        {"text": "A recent synthetic note.", "evidence": [], "added": h.T - 100},)))
    path = run(weekly.run_weekly(ctx, WEEK))
    assert sections(path)["consistency"]["status"] == "partial" and not receipt(h)["complete"]
    assert h.store.get_meta(f"eval:drift:{WEEK}") is None and len(h.store.get_state(101).self_notes) == 1
    run(weekly.run_weekly(ctx, WEEK))
    assert sections(path)["consistency"]["status"] == "complete" and receipt(h)["complete"]
    assert sections(path)["drift"]["rolled_back"] == [101] and h.store.get_state(101).self_notes == ()
    assert len(ctx.asker.calls) == 12 and h.count("review_queue") == 1


def test_partial_interview_is_not_reported_as_complete_and_only_pending_answer_retries(h, monkeypatch):
    ctx, _ = setup(h, monkeypatch)
    ctx.settings = replace(ctx.settings, interview_members=1)
    ask = ctx.asker.ask
    failed = False

    async def fail_one(guid, question):
        nonlocal failed
        if question == "What do you do for a living?" and not failed:
            failed = True
            raise LLMError("private answer unavailable")
        return await ask(guid, question)

    monkeypatch.setattr(ctx.asker, "ask", fail_one)
    path = run(weekly.run_weekly(ctx, WEEK))
    report = sections(path)
    assert report["interview"]["status"] == "partial" and report["interview"]["pending"] == 1
    assert report["interview"]["expected"] == 10 and report["stability"]["status"] == "partial"
    assert not receipt(h)["complete"]
    run(weekly.run_weekly(ctx, WEEK))
    assert sections(path)["interview"]["pending"] == 0 and receipt(h)["complete"]
    assert len(ctx.asker.calls) == 12


def test_deterministic_task24_failure_prevents_complete_but_other_sections_survive(h, monkeypatch):
    ctx, _ = setup(h, monkeypatch)

    def fail(*args, **kwargs):
        raise ValueError("private data text")

    monkeypatch.setattr(weekly.social_eval, "signals", fail)
    path = run(weekly.run_weekly(ctx, WEEK, use_llm=False))
    report = sections(path)
    assert report["signals"] == {"status": "failed", "error_type": "ValueError"}
    assert "pairs" in report["texture"] and "secret_leaks" in report["leaks"]
    assert receipt(h)["complete"] is False and "private data text" not in json.dumps(report)


def test_task24_interview_cancellation_keeps_probe_but_no_report(h, monkeypatch):
    ctx, _ = setup(h, monkeypatch)
    ctx.settings = replace(ctx.settings, interview_members=1)

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(weekly.interview_eval, "interview", cancelled)
    with pytest.raises(asyncio.CancelledError):
        run(weekly.run_weekly(ctx, WEEK))
    assert h.count("probe_run") == 1 and receipt(h) is None
    assert not (h.settings.data_dir / "reports" / f"{WEEK}.md").exists()
