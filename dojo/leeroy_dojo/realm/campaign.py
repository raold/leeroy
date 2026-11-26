"""Consecutive complete gate pairs on one immutable candidate; never retry failures."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from . import raids, vehicles, world, yogg
from .layout import LayoutError
from .ra import RaError

GATES = {
    "valithria": ("raid", ("valithria",)),
    "twins": ("raid", ("twins",)),
    "poison": ("raid", ("poison",)),
    "lich-king": ("raid", ("lk-terenas", "lk-defile")),
    "leviathan-reset": ("vehicle", ("leviathan-reset",)),
    "malygos": ("vehicle", ("malygos",)),
    "yogg": ("yogg", ()),
}


def _execute(layout, gate, log):
    family, scenarios = GATES[gate]
    if family == "raid":
        return raids.run(layout, scenarios=scenarios, log=log)
    if family == "vehicle":
        return vehicles.run(layout, scenarios=scenarios, log=log)
    return yogg.run(layout, log=log)


def _assess(child, gate, source):
    plumbing, behavior = [], []
    family, scenarios = GATES[gate]
    expected = ([mode for mode in ("negative", "normal")] if family == "yogg" else
                [f"{scenario}-{mode}" for scenario in scenarios for mode in ("negative", "normal")])
    if child.get("kind") != f"dojo-{family}-acceptance" or type(child.get("format")) is not int or child["format"] != 1:
        plumbing.append("invalid child report")
    if child.get("stopped") is not True:
        plumbing.append("shutdown unproved")
    if child.get("source") != source:
        plumbing.append("candidate changed in child report")
    if any(child.get(key) for key in ("error", "shutdown_error", "log_capture_error")):
        plumbing.append("child infrastructure failure")
    cases = child.get("cases")
    if (not isinstance(cases, list) or any(not isinstance(c, dict) for c in cases)
            or [c.get("name") for c in cases] != expected):
        plumbing.append("complete ordered control and enabled cases missing")
        return plumbing, behavior
    for case in cases:
        name = case["name"]
        if case.get("source", source) != source:
            plumbing.append(f"{name}: candidate changed")
        observed = case.get("observation")
        if not isinstance(observed, dict) or not isinstance(observed.get("token"), str) or not observed["token"]:
            plumbing.append(f"{name}: native observation missing")
            continue
        negative = name.endswith("negative")
        if family == "yogg":
            checked = yogg.verdict(observed, token=observed["token"], negative=negative)
        else:
            scenario = name.rsplit("-", 1)[0]
            validator = raids.verdict if family == "raid" else vehicles.verdict
            checked = validator(observed, scenario=scenario, token=observed["token"], negative=negative)
        if any(case.get(key) != value for key, value in checked.items()):
            plumbing.append(f"{name}: saved verdict does not match native evidence")
        plumbing.extend(f"{name}: {p}" for p in checked["plumbing_problems"])
        behavior.extend(f"{name}: {p}" for p in checked["behavior_problems"])
    if child.get("ok") is not True and not plumbing and not behavior:
        plumbing.append("unexplained failed child report")
    return plumbing, behavior


def run(layout, *, gate, runs=3, log=print):
    if gate not in GATES or type(runs) is not int or not 3 <= runs <= 100:
        raise LayoutError("choose a supported gate and 3-100 consecutive runs")
    if world.running(layout):
        raise world.WorldError("gate campaign requires a stopped dedicated dojo realm")
    runs_dir = layout.data / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.resolve() for p in runs_dir.iterdir()}
    out = runs_dir / (time.strftime("%Y%m%d_%H%M%S") + "-campaign-" + uuid.uuid4().hex[:8])
    out.mkdir(exist_ok=False)
    existing.add(out.resolve())
    report = dict(kind="dojo-gate-campaign", format=1, gate=gate, requested_runs=runs,
                  ok=False, out=str(out), runs=[], plumbing_problems=[], behavior_problems=[],
                  illusion_counts={str(i): 0 for i in (1, 2, 3)}, stopped=False)
    plumbing, behavior = report["plumbing_problems"], report["behavior_problems"]
    tokens = set()
    try:
        source = report["source"] = raids.provenance(layout)
        for index in range(1, runs + 1):
            if world.running(layout) or raids.provenance(layout) != source:
                plumbing.append("candidate or realm state changed before run")
                break
            log(f"campaign {gate}: run {index}/{runs}")
            child = _execute(layout, gate, log)
            child_dir = Path(child["out"]).resolve()
            if (child_dir.parent != runs_dir.resolve() or child_dir in existing):
                plumbing.append("child receipt is outside the realm or was reused")
                break
            existing.add(child_dir)
            receipt = child_dir / "report.json"
            if json.loads(receipt.read_text(encoding="utf-8")) != child:
                plumbing.append("saved child receipt differs from returned report")
                break
            trial_plumbing, trial_behavior = _assess(child, gate, source)
            for case in child.get("cases", []) if isinstance(child.get("cases"), list) else []:
                observed = case.get("observation") if isinstance(case, dict) else None
                token = observed.get("token") if isinstance(observed, dict) else None
                if not isinstance(token, str) or not token or token in tokens:
                    trial_plumbing.append("native observation token missing or reused")
                else:
                    tokens.add(token)
            entry = dict(index=index, out=str(child_dir), report_sha256=raids.fingerprint(receipt),
                         plumbing_problems=trial_plumbing, behavior_problems=trial_behavior, ok=False)
            report["runs"].append(entry)
            if world.running(layout):
                trial_plumbing.append("realm still running after child")
            elif raids.provenance(layout) != source:
                trial_plumbing.append("candidate changed during run")
            entry["ok"] = not trial_plumbing and not trial_behavior
            plumbing.extend(f"run {index}: {p}" for p in trial_plumbing)
            behavior.extend(f"run {index}: {p}" for p in trial_behavior)
            if not entry["ok"]:
                break
            if gate == "yogg":
                illusion = child["cases"][1]["observation"].get("illusion")
                if type(illusion) is not int or illusion not in (1, 2, 3):
                    plumbing.append("native Yogg room coverage missing")
                    break
                report["illusion_counts"][str(illusion)] += 1
            (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if gate == "yogg" and not all(report["illusion_counts"].values()):
            plumbing.append("all three native Yogg rooms must be covered")
        report["ok"] = len(report["runs"]) == runs and not plumbing and not behavior
    except (LayoutError, world.WorldError, RaError, OSError, ValueError, KeyError, TypeError) as error:
        plumbing.append(f"campaign infrastructure failure: {type(error).__name__}")
    finally:
        report["stopped"] = not world.running(layout)
        if not report["stopped"]:
            plumbing.append("campaign shutdown unproved")
        report["ok"] = report["ok"] and report["stopped"] and not plumbing and not behavior
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
