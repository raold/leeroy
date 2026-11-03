"""Weekly owner evaluation: resumable measurements, honest omissions, no live sending."""
import hashlib
import json
import math
import random
from pathlib import Path

from leeroy_mind.eval.audit import grounding_audit, night_health
from leeroy_mind.eval import interview as interview_eval, persona as persona_eval, social as social_eval
from leeroy_mind.eval.probes import (PROBE_SAMPLES, SECRET_PROBES, ProbeResult, probe_messages,
                                    record_probe, run_secret_probes, validate_probe)
from leeroy_mind.eval.quiz import ask_all, build_quiz, judge_all, safe_text, score
from leeroy_mind.eval.report import load_prev, validate_week, write_report
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.nightly.gpu import norm
from leeroy_mind.nightly.knowledge import secret_probe_passed
from leeroy_mind.nightly.settings import iso_week, night_of_ts, week_bounds


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _probe_context(ctx):
    return _hash({"model": norm(ctx.settings.live_model), "samples": PROBE_SAMPLES,
                  "options": dict(LIVE.options), "prompts": [probe_messages(p, ctx.ident) for p in SECRET_PROBES]})


def _probe_row(row):
    result = ProbeResult(row["n"], row["leaks"], row["rate"], json.loads(row["details"]))
    validate_probe(result)
    return result


def _saved_probe(ctx, week):
    """Only a completed, unchanged receipt for this exact test and model can be reused."""
    raw = ctx.store.get_meta(f"eval:secret-probe:{week}")
    if raw is None:
        return None
    try:
        receipt = json.loads(raw)
        if receipt["context"] != _probe_context(ctx) or type(receipt["id"]) is not int:
            return None
        with ctx.store.transaction():
            row = ctx.store.conn.execute("SELECT * FROM probe_run WHERE id=?", (receipt["id"],)).fetchone()
        if (row is None or norm(row["model"]) != norm(ctx.settings.live_model)
                or not math.isfinite(row["ts"]) or row["ts"] > ctx.clock.now()
                or _hash(dict(row)) != receipt["hash"]):
            return None
        return _probe_row(row)
    except (ValueError, TypeError, KeyError):
        return None


def _save_probe(ctx, week, result, *, expected_context):
    with ctx.store.transaction():
        if _probe_context(ctx) != expected_context:
            raise ValueError("secret probe identity or model changed while measuring")
        ident = record_probe(ctx.store, result, model=ctx.settings.live_model, now=ctx.clock.now())
        row = ctx.store.conn.execute("SELECT * FROM probe_run WHERE id=?", (ident,)).fetchone()
        ctx.store.set_meta(f"eval:secret-probe:{week}", json.dumps({"id": ident,
            "context": expected_context, "hash": _hash(dict(row))}))


def _gate(ctx):
    return secret_probe_passed(ctx.store, now=ctx.clock.now(), max_rate=ctx.settings.probe_max_rate,
                               model=ctx.settings.live_model)


def _failed(error, **fields):
    # Exception messages can quote model output or a local path. Reports retain type only.
    return {"status": "failed", "error_type": type(error).__name__, **fields}


def _skipped(reason):
    return {"status": "skipped", "skipped": True, "reason": reason}


def _safe_section(value, ident):
    """Legacy deterministic rows must pass the same identity screen as generated text."""
    if isinstance(value, str):
        return safe_text(value, ident)
    if isinstance(value, dict):
        return {_safe_section(key, ident): _safe_section(item, ident) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe_section(item, ident) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite evaluation metric")
    return value


async def run_weekly(ctx, week, *, use_llm=True):
    """Write a report even when ordinary measurements fail; cancellation always propagates.

    A report path means an artifact was written, not that every section succeeded. Pending
    quiz rows and section statuses remain visible. The caller owns its asker and GPU lease;
    this function neither closes the asker nor attaches any live application.
    """
    validate_week(week)  # before database writes, model administration or filesystem access
    bounds = week_bounds(week, ctx.tz)
    previous = iso_week(night_of_ts(bounds[0] - 1, ctx.tz))
    model_sections = ("secret_probe", "memory_quiz", "interview", "consistency", "distinctiveness", "stability")
    why = "llm_disabled" if not use_llm else "asker_unavailable"
    sections = {name: _skipped(why) for name in model_sections}
    ask_counts, errors = {}, []
    live_ok = reflection_ok = False
    if not use_llm or ctx.asker is None:
        pass
    else:
        try:
            await ctx.arbiter.ensure_model(ctx.settings.live_model)
        except Exception as error:
            sections["secret_probe"] = _failed(error, gate_open=_gate(ctx))
            for name in model_sections[1:]:
                sections[name] = _skipped("live_model_unavailable")
        else:
            live_ok = True
            try:
                result = _saved_probe(ctx, week)
                reused = result is not None
                if result is None:
                    context = _probe_context(ctx)
                    result = await run_secret_probes(ctx.reasoner.llm, ctx.settings.live_model, ctx.ident)
                    _save_probe(ctx, week, result, expected_context=context)
                sections["secret_probe"] = {"status": "complete", "n": result.n, "leaks": result.leaks,
                    "rate": result.rate, "gate_open": _gate(ctx), "reused": reused,
                    "leaked_probes": sorted({d["id"] for d in result.details if d["leaked"]})}
            except Exception as error:
                sections["secret_probe"] = _failed(error, gate_open=_gate(ctx))

            try:
                items = build_quiz(ctx.store, bounds, ctx.tz, random.Random(f"quiz:{week}"))
                ask_counts = await ask_all(ctx.store, ctx.asker, items, week, ident=ctx.ident)
            except Exception as error:
                errors.append(type(error).__name__)
            # Both live-answer workloads finish before admitting the reflection model.
            try:
                panel = interview_eval.interview_panel(ctx)
                counts = await interview_eval.interview(ctx.store, ctx.asker, week, panel, ident=ctx.ident)
                sections["interview"] = ({"status": "partial" if counts["pending"] else "complete"}
                    if counts["expected"] else _skipped("no_eligible_items")) | counts
            except Exception as error:
                expected = ctx.store.conn.execute("SELECT count(*) FROM eval_answer WHERE week=? AND kind='interview'", (week,)).fetchone()[0]
                answered = ctx.store.conn.execute("SELECT count(*) FROM eval_answer WHERE week=? AND kind='interview' AND trim(coalesce(answer,''))!=''", (week,)).fetchone()[0]
                sections["interview"] = _failed(error, expected=expected, answered=answered, pending=expected - answered)

    if live_ok:
        judge_counts = {}
        try:
            await ctx.arbiter.ensure_model(ctx.settings.reflection_model)
            reflection_ok = True
        except Exception as error:
            errors.append(type(error).__name__)
            for name in ("consistency", "distinctiveness", "stability"):
                sections[name] = _skipped("reflection_model_unavailable")
        if reflection_ok:
            try:
                judge_counts = await judge_all(ctx, week)
            except Exception as error:
                errors.append(type(error).__name__)
        measured = score(ctx.store, week)
        if measured["n"] == 0 and not errors and not ask_counts.get("failed", 0):
            sections["memory_quiz"] = _skipped("no_eligible_items") | measured
        else:
            partial = (measured["pending_n"] or errors or ask_counts.get("failed", 0)
                       or judge_counts.get("failed", 0))
            sections["memory_quiz"] = {"status": "partial" if partial else "complete", **measured,
                "asking": ask_counts, "judging": judge_counts, "error_types": errors}

    if reflection_ok:
        try:
            values = await persona_eval.consistency(ctx, bounds, random.Random(f"consistency:{week}"), week)
            coverage = persona_eval.consistency_coverage(ctx.store, week)
            status = ({"status": "complete" if coverage["scored_lines"] == coverage["sampled_lines"] else "partial"}
                      if coverage["sampled_lines"] else _skipped("no_eligible_items"))
            sections["consistency"] = status | {"members": values, **coverage}
        except Exception as error:
            sections["consistency"] = _failed(error, **persona_eval.consistency_coverage(ctx.store, week))
        try:
            measured = await persona_eval.distinctiveness(ctx, bounds, random.Random(f"distinctiveness:{week}"))
            n = measured["trials_sampled"]
            complete = (measured["trials_scored"] == n and
                        measured["cross_member_pairs"] + measured["within_member_pairs"] == n * (n - 1) // 2)
            status = ({"status": "complete" if complete else "partial"} if n else _skipped("no_eligible_items"))
            sections["distinctiveness"] = status | measured
        except Exception as error:
            sections["distinctiveness"] = _failed(error)
        try:
            measured = await interview_eval.stability(ctx, week, previous)
            if sections["interview"].get("status") in ("failed", "partial"):
                status = {"status": "partial"}
            elif not measured["expected_trait_answers"]:
                status = _skipped("no_eligible_items")
            elif not measured["expected_trait_pairs"]:
                status = _skipped("no_previous_answers")
            else:
                complete = (measured["trait_pairs"] == measured["expected_trait_pairs"]
                            and measured["people_pairs"] == measured["expected_people_pairs"])
                status = {"status": "complete" if complete else "partial"}
            sections["stability"] = status | measured
        except Exception as error:
            sections["stability"] = _failed(error)

    names = {guid: member.name for guid, member in ctx.store.cast().items()}
    deterministic = {
        "grounding_audit": lambda: grounding_audit(ctx.store, bounds, random.Random(f"audit:{week}"), ident=ctx.ident),
        "night_health": lambda: night_health(ctx.store, bounds),
        "texture": lambda: social_eval.texture(ctx.store, bounds, human=ctx.human, speed=ctx.settings.relationship_speed),
        "callbacks": lambda: social_eval.callbacks(ctx.store, bounds),
        "noise": lambda: social_eval.noise(ctx.store, bounds, chattiness=ctx.settings.chattiness, names=names, ident=ctx.ident),
        "cast_mix": lambda: social_eval.cast_mix(ctx),
        "signals": lambda: social_eval.signals(ctx.store, bounds, human=ctx.human),
        "leaks": lambda: social_eval.leaks(ctx.store, bounds, names=(ctx.ident.called,), model=ctx.settings.live_model),
    }
    for name, measure in deterministic.items():
        try:
            sections[name] = _safe_section(measure(), ctx.ident)
        except Exception as error:
            sections[name] = _failed(error)
    # A partial consistency sample must not freeze the week's rollback receipt before its
    # missing batches can finish on retry. The rollback itself is atomic and undoable.
    if sections["consistency"].get("status") == "complete":
        try:
            sections["drift"] = {"status": "complete", "rolled_back": persona_eval.drift_check(
                ctx.store, week, previous, since=bounds[0], now=ctx.clock.now())}
        except Exception as error:
            sections["drift"] = _failed(error)
    else:
        sections["drift"] = _skipped("no_eligible_items" if sections["consistency"].get("reason") == "no_eligible_items"
                                     else "consistency_incomplete" if use_llm else "llm_disabled")
    reports = Path(ctx.settings.data_dir) / "reports"
    markdown, _ = write_report(reports, week, sections, load_prev(reports, week))
    complete = all(sections[name].get("status") != "failed" for name in deterministic)
    if use_llm:
        complete = complete and all(sections[name].get("status") == "complete" or
            sections[name].get("reason") in ("no_eligible_items", "no_previous_answers")
            for name in (*model_sections, "drift"))
    with ctx.store.transaction():
        ctx.store.set_meta(f"nightly:eval-result:{week}", json.dumps({"complete": bool(complete),
            "status": "complete" if complete else "partial", "requested_llm": bool(use_llm)}))
    return markdown
