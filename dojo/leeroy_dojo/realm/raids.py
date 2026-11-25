"""Run real raid mechanics on a disposable dojo world; never accepts a host or live GUID."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
import uuid

from . import mysql, world
from .layout import LayoutError, read_conf
from .ra import RaError

SCENARIOS = ("valithria", "twins", "poison")
LICH_KING_SCENARIOS = ("lk-terenas", "lk-defile")
ALL_SCENARIOS = SCENARIOS + LICH_KING_SCENARIOS


def verdict(observation, *, scenario, token, negative=False):
    """An action result is deliberately not an input. Missing evidence always fails."""
    o = observation
    problems = []
    for key, expected in (("format", 1), ("kind", "dojo-raid-observation"), ("scenario", scenario),
                          ("token", token), ("control", negative), ("state", "done"), ("setup", True), ("cleanup", True)):
        if key not in o or type(o[key]) is not type(expected) or o[key] != expected:
            problems.append(f"{key}: expected {expected!r}")
    if o.get("failure") != "":
        problems.append("native failure or absent failure field")
    counts = ("instance", "samples", "max_gap_ms", "heal_events", "effective_healing", "light_samples",
              "dark_samples", "switch_ms", "crossings", "hazard_samples", "poison_hits")
    if any(type(o.get(key)) is not int or o[key] < 0 for key in counts):
        return {"ok": False, "problems": problems + ["missing, negative or non-integer observation counter"]}
    if type(o.get("minimum_clearance")) not in (float, int) or not math.isfinite(o["minimum_clearance"]) or o["minimum_clearance"] < 0:
        return {"ok": False, "problems": problems + ["invalid clearance"]}
    if o.get("map") != {"valithria": 631, "twins": 649, "poison": 533, "lk-terenas": 631, "lk-defile": 631}.get(scenario) or o.get("instance", 0) <= 0:
        problems.append("wrong or absent raid instance")
    if o.get("samples", 0) < 10:
        problems.append("insufficient runtime observations")
    if scenario == "valithria":
        if not o.get("reach_competitor") or not o.get("combat_observed"):
            problems.append("stock reach-to-heal competitor or combat was not exercised")
        if negative and (o["heal_events"] > 0 or o["effective_healing"] > 0):
            problems.append("negative control healed Valithria")
        elif not negative and (o["heal_events"] < 2 or o["effective_healing"] <= 0):
            problems.append("fewer than two effective heals on Valithria")
    elif scenario == "twins":
        acquired = o.get("light_samples", 0) >= 5
        switched = o.get("dark_samples", 0) >= 10 and o.get("switch_ms", 0) > 0
        if negative and (o["light_samples"] > 0 or o["dark_samples"] > 0):
            problems.append("disabled Twin interaction still acquired an essence")
        elif not negative and not (acquired and switched):
            problems.append("real essence acquisition and colour change not both observed")
    elif scenario in LICH_KING_SCENARIOS:
        lk_counts = ("room_samples", "escape_ms", "outside_samples", "inside_samples", "late_hits", "encounter_samples", "defile_hits")
        if any(type(o.get(k)) is not int or o[k] < 0 for k in lk_counts):
            return {"ok": False, "problems": problems + ["invalid Lich King observation counter"]}
        if o["encounter_samples"] < 10:
            problems.append("native Lich King encounter was not active")
        if scenario == "lk-terenas":
            if any(type(o.get(k)) is not int or o[k] < 1 for k in ("harvest_samples", "strangulate_samples")):
                problems.append("native Harvest Soul and strangulate vehicle were not observed")
            if o["room_samples"] < 10 or o.get("reach_competitor") is not True:
                problems.append("Frostmourne room or outside follow competitor was not exercised")
            if negative and (o["heal_events"] > 0 or o["effective_healing"] > 0):
                problems.append("disabled Lich King strategy still healed Terenas")
            elif not negative and (o["heal_events"] < 2 or o["effective_healing"] <= 0):
                problems.append("fewer than two effective heals on real Terenas")
        else:
            clearance = o.get("final_hazard_clearance")
            if type(clearance) not in (int, float) or not math.isfinite(clearance):
                problems.append("invalid Defile clearance")
            elif not negative and clearance <= 0:
                problems.append("still inside the scaled Defile hazard")
            if o["hazard_samples"] < 20 or o["inside_samples"] < 1 or o["max_gap_ms"] > 200:
                problems.append("Defile exposure or observation cadence not proved")
            if negative and o["defile_hits"] < 1:
                problems.append("disabled Defile control did not take real damage")
            elif not negative and (not 0 < o["escape_ms"] <= 4000 or o["outside_samples"] < 20 or o["late_hits"] != 0):
                problems.append("Defile escape was late, unstable, or followed by further damage")
    elif scenario == "poison":
        if o.get("hazard_samples", 0) < 20:
            problems.append("Living Poison was not present long enough")
        if o.get("max_gap_ms", 100000) > 200:
            problems.append("sampling gap too large to certify a poison crossing")
        if negative:
            if o.get("poison_hits", 0) < 1:
                problems.append("negative control did not demonstrate a real poison hit")
        elif o.get("crossings", 0) < 2 or o.get("minimum_clearance", 0) <= 1.5 or o.get("poison_hits", -1) != 0:
            problems.append("crossing stalled, entered the trigger radius, or took poison damage")
    else:
        problems.append("unknown scenario")
    return {"ok": not problems, "problems": problems}


def parse_status(text):
    rows = [line[len("dojo-raid "):] for line in text.splitlines() if line.startswith("dojo-raid ")]
    if len(rows) != 1:
        raise world.WorldError("native raid observer missing or ambiguous (build with RaidAcceptance)")
    return json.loads(rows[0])


def fingerprint(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def provenance(layout):
    revisions = {}
    for name in ("source", "core", "playerbots") + (("dungeonclear",) if "dungeonclear" in layout.roots else ()):
        root = layout.roots[name]
        def git(*args):
            return subprocess.check_output(["git", "-C", root, *args], text=True, encoding="utf-8").strip()
        # A clean candidate commit is essential: an uncommitted harness fix must
        # never borrow the previous commit's apparently passing receipt.
        if git("status", "--porcelain", "--untracked-files=normal"):
            raise LayoutError(f"{name} checkout is dirty; commit the candidate before measuring it")
        revisions[name] = git("rev-parse", "HEAD")
    build = json.loads((Path(layout.roots["build"]) / "dojo-configure.json").read_text(encoding="utf-8"))
    if not build.get("raid_acceptance") or build.get("variant") != "training":
        raise LayoutError("configure receipt is not a raid acceptance training build")
    if build.get("clean_revisions") != revisions:
        raise LayoutError("configure receipt lacks matching build-time clean-source evidence")
    for name in revisions:
        if build[name]["head"] != revisions[name]:
            raise LayoutError(f"{name} revision differs from the configure receipt; rebuild and provision")
    binary = layout.bin / "worldserver.exe"
    built = layout.build_bin / "worldserver.exe"
    linked = json.loads((Path(layout.roots["build"]) / "dojo-build.json").read_text(encoding="utf-8"))
    if linked.get("clean_revisions") != revisions or (Path(layout.roots["build"]) / "dojo-build.pending.json").exists():
        raise LayoutError("link receipt lacks matching completed clean-source evidence")
    if linked.get("exit_code") != 0 or linked.get("problems") or any(linked.get(k) != v for k, v in revisions.items()):
        raise LayoutError("successful link receipt does not describe the candidate revisions")
    digest = fingerprint(binary)
    if digest != fingerprint(built) or digest != linked["artifacts"]["worldserver.exe"]["sha256"]:
        raise LayoutError("staged worldserver differs from the candidate build")
    configs = {name: fingerprint(layout.configs / name) for name in (
        "worldserver.conf", "modules/playerbots.conf", "modules/mod_dojo.conf", "modules/mod_leeroy.conf")}
    if "dungeonclear" in revisions:
        configs["modules/mod_dungeon_clear.conf"] = fingerprint(layout.configs / "modules/mod_dungeon_clear.conf")
    return dict(revisions=revisions, worldserver_sha256=digest, layout_sha256=fingerprint(layout.path), configs=configs)


def run(layout, *, scenarios=SCENARIOS, log=print):
    # Refuse to adopt another experiment's process. All commands below pass the
    # ordinary dojo root, marker, live-path, port and MySQL identity checks.
    if not scenarios or len(set(scenarios)) != len(scenarios) or any(s not in ALL_SCENARIOS for s in scenarios):
        raise LayoutError("select distinct, known raid scenarios")
    if world.running(layout):
        raise world.WorldError("raid acceptance needs a stopped dedicated dojo instance")
    preflight = world.preflight(layout)
    mysql.verify_identity(layout, mysql.Client(layout, layout.server.root_cnf))
    source = provenance(layout)
    dojo = read_conf(layout.configs / "modules/mod_dojo.conf")
    leeroy = read_conf(layout.configs / "modules/mod_leeroy.conf")
    if dojo.get("Dojo.Scenario.Manifest") or any(leeroy.get(key) != "0" for key in (
            "Leeroy.Enable", "Leeroy.HumanGuid", "Leeroy.GuildId")):
        raise LayoutError("raid realm needs no arena manifest and Leeroy Enable/HumanGuid/GuildId = 0")
    out = layout.data / "runs" / (time.strftime("%Y%m%d_%H%M%S") + "-raids-" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    results = []
    report = dict(kind="dojo-raid-acceptance", format=1, ok=False, source=source, preflight=preflight,
                  cases=results, stopped=False, out=str(out))
    started = False
    try:
        started = True
        world.start(layout, log=log)
        with world.ra(layout) as client:
            for scenario in scenarios:
                for negative in (True, False):
                    token = uuid.uuid4().hex
                    name = scenario + ("-negative" if negative else "-normal")
                    log(f"raid check {name}: starting")
                    reply = client.command(f"dojo raid start {scenario} {token} {'negative' if negative else 'normal'}")
                    if "dojo raid: started" not in reply:
                        raise world.WorldError(f"native raid start refused: {reply}")
                    deadline = time.monotonic() + 210
                    with (out / f"{name}.jsonl").open("w", encoding="utf-8") as trace:
                        while True:
                            state = parse_status(client.command("dojo raid status"))
                            trace.write(json.dumps(state, sort_keys=True) + "\n")
                            trace.flush()
                            if state.get("token") != token:
                                raise world.WorldError("run token changed while observing")
                            if state.get("state") in {"done", "cleanup_failed"}:
                                break
                            if time.monotonic() >= deadline:
                                client.command("dojo raid abort")
                                raise world.WorldError("native scenario/cleanup deadline exceeded")
                            time.sleep(0.5)
                    result = dict(name=name, observation=state, **verdict(state, scenario=scenario, token=token, negative=negative))
                    results.append(result)
                    log(f"raid check {name}: {'passed' if result['ok'] else 'FAILED'}")
                    if not state.get("cleanup"):
                        raise world.WorldError("fixture cleanup unproved; refusing the next scenario")
        report["ok"] = len(results) == len(scenarios) * 2 and all(r["ok"] for r in results)
    except (world.WorldError, RaError, OSError, ValueError) as error:
        report["error"] = str(error)
    finally:
        if started:
            try:
                if world.running(layout):
                    world.stop(layout, log=log)
                report["stopped"] = not world.running(layout)
            except (world.WorldError, RaError, OSError) as error:
                report["shutdown_error"] = str(error)
        report["ok"] = report["ok"] and report["stopped"]
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
