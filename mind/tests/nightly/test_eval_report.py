import json
import random
from datetime import timezone

import pytest

from leeroy_mind.eval.audit import grounding_audit, night_health
from leeroy_mind.eval.report import load_prev, write_report
from leeroy_mind.nightly.settings import week_bounds
from leeroy_mind.nightly.store import log_grounding, run_begin, run_finish, stage_record

WEEK = "2026-W44"
BOUNDS = week_bounds(WEEK, timezone.utc)


def logs(h, *, n=10, night=None, ts=None):
    for i in range(n):
        log_grounding(h.store, night=night or h.night, stage="reflect", owner=101, kind="belief",
            text=f"A belief {i}", verdict="accepted" if i < 8 else "rejected", check="judge",
            reason="not supported" if i >= 8 else "", ts=h.T if ts is None else ts)


def test_grounding_audit_counts_rates_and_only_returns_actual_evidence(h):
    logs(h)
    h.mem(101, "A belief 0", t=h.T, kind="reflection", source="reflection", evidence=("event:7",))
    # An earlier identically worded belief is not evidence for this accepted claim.
    h.mem(101, "A belief 1", t=h.T - 20, kind="reflection", source="reflection", evidence=("event:99",))
    result = grounding_audit(h.store, BOUNDS, random.Random(4), sample=20, ident=h.ident)
    assert result["by_stage"] == {"reflect": {"accepted": 8, "rejected": 2, "reject_rate": .2}}
    assert result["top_reasons"] == [["not supported", 2]]
    assert len(result["rate_these"]) == 8
    by = {r["text"]: r for r in result["rate_these"]}
    assert by["A belief 0"]["evidence"] == ["event:7"]
    assert by["A belief 1"]["evidence"] == []


def test_audit_caps_and_is_seed_reproducible(h):
    logs(h)
    a = grounding_audit(h.store, BOUNDS, random.Random(3), sample=3)
    assert len(a["rate_these"]) == 3
    assert a == grounding_audit(h.store, BOUNDS, random.Random(3), sample=3)
    assert grounding_audit(h.store, BOUNDS, random.Random(3), sample=0)["rate_these"] == []


def test_owner_audit_screens_legacy_source_and_rejection_reason(h):
    log_grounding(h.store, night=h.night, stage="reflect", owner=101, kind="belief",
        text="|TQuillon|t Quillon trusts me", verdict="accepted", check="judge", reason="", ts=h.T)
    log_grounding(h.store, night=h.night, stage="reflect", owner=101, kind="belief",
        text="x", verdict="rejected", check="judge", reason="|TQuillon|t Quillon is hidden", ts=h.T)
    result = grounding_audit(h.store, BOUNDS, random.Random(1), ident=h.ident)
    assert "Quillon" not in json.dumps(result)
    assert "trusts me" in result["rate_these"][0]["text"]


def test_logical_run_window_keeps_delayed_sunday_audit_in_original_week(h):
    run_begin(h.store, "2026-11-01", BOUNDS[1] - 86400, BOUNDS[1], now=BOUNDS[1] + 100)
    logs(h, night="2026-11-01", ts=BOUNDS[1] + 100)
    assert grounding_audit(h.store, BOUNDS, random.Random(1))["by_stage"]["reflect"]["accepted"] == 8
    following = week_bounds("2026-W45", timezone.utc)
    assert grounding_audit(h.store, following, random.Random(1))["by_stage"] == {}


def test_night_health_uses_logical_windows_and_real_stage_elapsed_cost(h):
    for i, (night, status, foreign) in enumerate((("2026-10-26", "done", 3),
                                                ("2026-10-27", "done", 1),
                                                ("2026-10-28", "failed", 0))):
        run_begin(h.store, night, BOUNDS[0] + i * 86400, BOUNDS[0] + (i + 1) * 86400,
                  now=BOUNDS[1] + 100)
        run_finish(h.store, night, now=BOUNDS[1] + 400, status=status)
        with h.store.transaction():
            h.store.conn.execute("UPDATE night_run SET foreign_loads=?,error=?,plan=? WHERE night=?",
                (foreign, json.dumps({"reflect": 2}) if i == 2 else None,
                 json.dumps({"estimate_s": 120, "budget_s": 90, "steps": ["fewer", "smaller"]})
                 if i == 0 else "{}", night))
            h.store.conn.execute("INSERT INTO night_stage(night,stage,started,finished,calls,call_s) "
                "VALUES (?,'reflect',100,120,2,5)", (night,))
    result = night_health(h.store, BOUNDS)
    assert (result["runs"], result["failed"], result["foreign_loads"], result["unit_failures"]) == (3, 1, 4, 2)
    assert result["stage_seconds"] == {"reflect": 60.0}
    assert result["plans"][0] == {"night": "2026-10-26", "estimate_s": 120, "budget_s": 90,
                                 "steps": ["fewer", "smaller"]}
    assert result["audience_source"] == "column"
    assert night_health(h.store, week_bounds("2026-W45", timezone.utc))["runs"] == 0


def test_night_health_fallback_and_malformed_error_are_explicit(h):
    run_begin(h.store, h.night, h.T, h.T + 100, now=h.T)
    with h.store.transaction():
        h.store.conn.execute("UPDATE night_run SET error='run-level exception',plan='[]'")
        h.store.conn.execute("ALTER TABLE chat_line DROP COLUMN audience")
    result = night_health(h.store, BOUNDS)
    assert result["audience_source"] == "fallback"
    assert result["unit_failures"] == 0
    assert result["plans"][0]["estimate_s"] is None


def test_report_files_and_numeric_trends(tmp_path):
    sections = {"memory_quiz": {"abstention_rate": .9, "false_memory_rate": .1, "n": 10,
                               "accuracy": {"temporal": None}}}
    prev = {"memory_quiz": {"abstention_rate": .8, "false_memory_rate": .2, "n": 10,
                           "accuracy": {"temporal": .5}}}
    md, js = write_report(tmp_path, WEEK, sections, prev)
    text = md.read_text(encoding="utf-8")
    assert "## Memory quiz" in text and "vs last week" in text
    assert "| abstention_rate | 0.9 | ▲ |" in text
    assert "| false_memory_rate | 0.1 | ▼ |" in text
    assert "| n | 10 | = |" in text
    assert "| accuracy.temporal | not measured | — |" in text
    assert json.loads(js.read_text(encoding="utf-8")) == sections


def test_skipped_and_checklist_are_honest_and_do_not_invent_trends(tmp_path):
    sections = {"secret_probe": {"skipped": True}, "grounding_audit": {
        "n": 1, "rate_these": [{"guid": 101, "text": "A | belief\n<script>x</script>",
                                  "evidence": ["chat:12"]}]}}
    md, _ = write_report(tmp_path, WEEK, sections, None)
    text = md.read_text(encoding="utf-8")
    assert "## Secret probe\n\n_skipped_" in text
    assert "### Owner ratings" in text
    assert "- [ ] 101: A \\| belief &lt;script&gt;x&lt;/script&gt;" in text
    assert "| n | 1 | — |" in text
    assert "chat:12" in text


def test_previous_report_follows_iso_year_boundary(tmp_path):
    sections = {"memory_quiz": {"n": 2}}
    write_report(tmp_path, "2026-W53", sections, None)
    assert load_prev(tmp_path, "2027-W01") == sections
    assert load_prev(tmp_path, "2026-W52") is None


@pytest.mark.parametrize("week", ["../2026-W44", "2026-W44/../x", "2026-W00", "2025-W53",
                                   "2026-W54", "2026-W1", "2026-W44.txt", "0000-W01"])
def test_invalid_report_week_never_creates_files(tmp_path, week):
    root = tmp_path / "reports"
    with pytest.raises(ValueError):
        write_report(root, week, {}, None)
    with pytest.raises(ValueError):
        load_prev(root, week)
    assert not root.exists()


def test_corrupt_previous_report_is_missing_not_fabricated_metrics(tmp_path):
    (tmp_path / "2026-W43.json").write_text("{", encoding="utf-8")
    assert load_prev(tmp_path, WEEK) is None


def test_failed_serialization_does_not_replace_existing_report(tmp_path):
    md, js = write_report(tmp_path, WEEK, {"good": {"n": 1}}, None)
    old = md.read_bytes(), js.read_bytes()
    with pytest.raises(ValueError):
        write_report(tmp_path, WEEK, {"bad": {"n": float("nan")}}, None)
    assert old == (md.read_bytes(), js.read_bytes())
    assert list(tmp_path.glob("*.tmp")) == []


def test_symlink_report_destination_is_refused(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("private", encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    try:
        (reports / f"{WEEK}.json").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable on this platform")
    with pytest.raises(ValueError, match="report path"):
        write_report(reports, WEEK, {}, None)
    assert outside.read_text(encoding="utf-8") == "private"
