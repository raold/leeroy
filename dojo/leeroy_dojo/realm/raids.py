"""Run real raid mechanics on a disposable dojo world; never accepts a host or live GUID."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from . import mysql, world
from .layout import LayoutError, read_conf
from .ra import RaError

SCENARIOS = ("valithria", "twins", "poison")
LICH_KING_SCENARIOS = ("lk-terenas", "lk-defile")
ALL_SCENARIOS = SCENARIOS + LICH_KING_SCENARIOS + ("trigger-flame", "coldflame")


def verdict(observation, *, scenario, token, negative=False):
    """An action result is deliberately not an input. Missing evidence always fails."""
    o = observation
    plumbing, behavior = [], []

    def result():
        return dict(ok=not plumbing and not behavior, problems=plumbing + behavior,
                    plumbing_problems=plumbing, behavior_problems=behavior)

    for key, expected in (("format", 1), ("kind", "dojo-raid-observation"), ("scenario", scenario),
                          ("token", token), ("control", negative), ("state", "done"), ("setup", True), ("cleanup", True)):
        if key not in o or type(o[key]) is not type(expected) or o[key] != expected:
            plumbing.append(f"{key}: expected {expected!r}")
    if o.get("failure") != "":
        (behavior if isinstance(o.get("failure"), str) else plumbing).append("native failure or absent failure field")
    counts = ("instance", "samples", "max_gap_ms", "heal_events", "effective_healing", "light_samples",
              "dark_samples", "switch_ms", "crossings", "hazard_samples", "poison_hits")
    if any(type(o.get(key)) is not int or o[key] < 0 for key in counts):
        plumbing.append("missing, negative or non-integer observation counter")
        return result()
    if type(o.get("minimum_clearance")) not in (float, int) or not math.isfinite(o["minimum_clearance"]) or o["minimum_clearance"] < 0:
        plumbing.append("invalid clearance")
        return result()
    if o.get("map") != {"valithria": 631, "twins": 649, "poison": 533, "lk-terenas": 631, "lk-defile": 631, "trigger-flame": 603, "coldflame": 631}.get(scenario) or o.get("instance", 0) <= 0:
        plumbing.append("wrong or absent raid instance")
    if o.get("samples", 0) < 10:
        plumbing.append("insufficient runtime observations")
    problems = behavior
    if scenario == "valithria":
        if not o.get("reach_competitor") or not o.get("combat_observed"):
            plumbing.append("stock reach-to-heal competitor or combat was not exercised")
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
            plumbing.append("invalid Lich King observation counter")
            return result()
        if o["encounter_samples"] < 10:
            plumbing.append("native Lich King encounter was not active")
        if scenario == "lk-terenas":
            if (type(o.get("anchor_combat_samples")) is not int or
                    o["anchor_combat_samples"] != o["samples"]):
                plumbing.append("native outside anchor engagement was not continuous")
            if (type(o.get("anchor_armor")) is not int or o["anchor_armor"] < 20000 or
                    type(o.get("anchor_item_level")) not in (int, float) or
                    not math.isfinite(o["anchor_item_level"]) or o["anchor_item_level"] < 230):
                plumbing.append("outside anchor lacked native raid tank preparation")
            if any(type(o.get(k)) is not int or o[k] < 1 for k in ("harvest_samples", "strangulate_samples")):
                plumbing.append("native Harvest Soul and strangulate vehicle were not observed")
            if o["room_samples"] < 10 or o.get("reach_competitor") is not True:
                plumbing.append("Frostmourne room or outside follow competitor was not exercised")
            if negative and (o["heal_events"] > 0 or o["effective_healing"] > 0):
                problems.append("disabled Lich King strategy still healed Terenas")
            elif not negative and (o["heal_events"] < 2 or o["effective_healing"] <= 0):
                problems.append("fewer than two effective heals on real Terenas")
        else:
            clearance = o.get("final_hazard_clearance")
            if type(clearance) not in (int, float) or not math.isfinite(clearance):
                plumbing.append("invalid Defile clearance")
            elif not negative and clearance <= 0:
                problems.append("still inside the scaled Defile hazard")
            if o["hazard_samples"] < 20 or o["inside_samples"] < 1 or o["max_gap_ms"] > 200:
                plumbing.append("Defile exposure or observation cadence not proved")
            if negative and o["defile_hits"] < 1:
                problems.append("disabled Defile control did not take real damage")
            elif not negative and (not 0 < o["escape_ms"] <= 4000 or o["outside_samples"] < 20 or o["late_hits"] != 0):
                problems.append("Defile escape was late, unstable, or followed by further damage")
    elif scenario in ("trigger-flame", "coldflame"):
        for key in ("trigger_aura_samples", "trigger_hits", "inside_samples", "outside_samples", "escape_ms", "late_hits"):
            if type(o.get(key)) is not int or o[key] < 0:
                plumbing.append("invalid trigger observation counter")
        for key in ("trigger_radius", "trigger_displacement", "final_hazard_clearance"):
            if type(o.get(key)) not in (int, float) or not math.isfinite(o[key]):
                plumbing.append("invalid trigger geometry")
        if plumbing:
            return result()
        if (o["trigger_radius"] <= 0 or o["trigger_aura_samples"] < 20 or o["hazard_samples"] < 20 or
                o["inside_samples"] < 1 or o["max_gap_ms"] > 200 or o.get("reach_competitor") is not True):
            plumbing.append("native fire exposure, stock competitors or cadence not proved")
        if negative:
            if o["trigger_hits"] < 1:
                problems.append("disabled avoidance control did not take native fire damage")
        # Coldflame's real area lasts three seconds. Its probe ends at 2.4 s,
        # while that exact object remains active; expiry must never be escape.
        elif (not 0 < o["escape_ms"] <= (2000 if scenario == "coldflame" else 4000) or
              o["outside_samples"] < 20 or o["late_hits"] != 0 or
              o["final_hazard_clearance"] <= 0 or o["trigger_displacement"] <= o["trigger_radius"]):
            problems.append("trigger escape was late, unstable, stationary or followed by damage")
    elif scenario == "poison":
        if o.get("hazard_samples", 0) < 20:
            plumbing.append("Living Poison was not present long enough")
        if o.get("max_gap_ms", 100000) > 200:
            plumbing.append("sampling gap too large to certify a poison crossing")
        if negative:
            if o.get("poison_hits", 0) < 1:
                problems.append("negative control did not demonstrate a real poison hit")
        elif o.get("crossings", 0) < 2 or o.get("minimum_clearance", 0) <= 1.5 or o.get("poison_hits", -1) != 0:
            problems.append("crossing stalled, entered the trigger radius, or took poison damage")
    else:
        plumbing.append("unknown scenario")
    return result()


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


def run(layout, *, scenarios=SCENARIOS, log=print, before_case=None):
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
    seen = {}
    try:
        started = True
        world.start(layout, log=log)
        with world.ra(layout) as client:
            for scenario in scenarios:
                for negative in (True, False):
                    token = uuid.uuid4().hex
                    name = scenario + ("-negative" if negative else "-normal")
                    if before_case is not None:
                        before_case(name, out)
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
        if started and report["stopped"]:
            try:
                report["logs"] = {}
                for name in ("Server.log", "Playerbots.log"):
                    path = layout.logs / name
                    if path.is_file():
                        saved = out / name
                        shutil.copyfile(path, saved)
                        report["logs"][name] = fingerprint(saved)
            except OSError as error:
                report["log_capture_error"] = type(error).__name__
                report["ok"] = False
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


ENCOUNTER_RUNS = ("10n:after", "10n:turns", "10h:after", "10h:turns")
ENCOUNTER_EXTRA = ("10n:before", "10h:before", "10n:noportals", "10h:noportals", "10n:adds", "10h:adds")
TANK_SLOTS = (3, 4)
ENCOUNTER_DEADLINE_S = 25 * 60


def encounter_verdict(observation, *, token, difficulty, mode, owner_buffs=False):
    """A full Valithria pull: setup, real engagement and her own DONE state, or why it did not get there.

    `ok` means the harness ran and cleaned up; `killed` means she reached full health in that pull
    (the core's boss state DONE). Missing evidence is never a kill."""
    o = observation
    problems = []
    for key, expected in (("format", 1), ("kind", "dojo-valithria-encounter"), ("token", token),
                          ("difficulty", difficulty), ("mode", mode), ("focus", mode != "before"),
                          ("portals", mode != "noportals"), ("adds", mode in ("adds", "turns")),
                          ("portal_turns", mode == "turns"),
                          ("owner_buffs", owner_buffs),
                          ("state", "done"), ("setup", True), ("cleanup", True), ("engaged", True)):
        if key not in o or type(o[key]) is not type(expected) or o[key] != expected:
            problems.append(f"{key}: expected {expected!r}")
    if o.get("failure") != "":
        problems.append("native failure or absent failure field")
    wanted = {"10n": 0, "10h": 2}[difficulty]
    if o.get("map_difficulty") != wanted or type(o.get("instance")) is not int or o["instance"] <= 0:
        problems.append("wrong or absent raid instance or difficulty")
    members = o.get("members")
    if type(members) is not list or len(members) != 10 or sum(1 for m in members if m.get("healer")) != 3:
        problems.append("the fixed ten-player roster with three healers was not reported")
        members = []
    for m in members:
        if m.get("level") != 80 or (m.get("healer") and m.get("is_heal") is not True):
            problems.append(f"{m.get('name')}: wrong level or role")
    if o.get("outcome") not in {"done", "wipe", "reset", "timeout"}:
        problems.append("no encounter outcome")
    if type(o.get("samples")) is not int or o["samples"] < 20:
        problems.append("insufficient runtime observations")
    start = o.get("start_hp")
    if type(start) not in (int, float) or not 49.0 <= start <= 60.0:
        problems.append("she did not start near 50%")
    killed = (not problems and o["outcome"] == "done" and type(o.get("peak_hp")) in (int, float)
              and o["peak_hp"] >= 99.5)
    healers = [m for m in members if m.get("healer")]
    deaths = o.get("deaths") if type(o.get("deaths")) is list else []
    tanks = {members[i].get("name") for i in TANK_SLOTS if i < len(members)}
    summary = dict(
        outcome=o.get("outcome"), pull_s=round(o.get("pull_ms", 0) / 1000, 1), start_hp=start,
        end_hp=o.get("hp"), peak_hp=o.get("peak_hp"),
        deaths=sum(m.get("deaths", 0) for m in members),
        healer_deaths=sum(m.get("deaths", 0) for m in healers),
        portal_entries=sum(m.get("dream_entries", 0) for m in healers),
        dream_s=round(sum(m.get("dream_ms", 0) for m in healers) / 1000, 1),
        healing_on_her=sum(m.get("healing_on_her", 0) for m in healers),
        mana_cooldowns=sum(m.get("mana_cooldowns", 0) for m in healers),
        mana_potions=sum(m.get("mana_potions", 0) for m in healers),
        max_twisted_nightmares=max((m.get("max_twisted_nightmares", 0) for m in healers), default=0),
        max_emerald_vigor=max((m.get("max_emerald_vigor", 0) for m in healers), default=0),
        first_death_s=round(deaths[0].get("ms", 0) / 1000, 1) if deaths else None,
        two_in_dream_s=round(o.get("two_in_dream_ms", 0) / 1000, 1),
        none_on_her_s=round(o.get("none_on_her_ms", 0) / 1000, 1),
        tank_deaths=[[d.get("name"), round(d.get("ms", 0) / 1000, 1), d.get("killer")] for d in deaths
                     if d.get("name") in tanks])
    return {"ok": not problems, "killed": killed, "problems": problems, "summary": summary}


def parse_encounter(text):
    rows = [line[len("dojo-encounter "):] for line in text.splitlines() if line.startswith("dojo-encounter ")]
    if len(rows) != 1:
        raise world.WorldError("native encounter observer missing or ambiguous (build with RaidAcceptance)")
    return json.loads(rows[0])


def encounter(layout, runs=ENCOUNTER_RUNS, *, item_level=245, owner_buffs=False, log=print):
    """Full Valithria pulls of one fixed ten-player bot raid with the healer focus and adds switches."""
    for item in runs:
        if item not in ENCOUNTER_RUNS + ENCOUNTER_EXTRA:
            raise LayoutError(f"unknown encounter run {item!r}; use {', '.join(ENCOUNTER_RUNS + ENCOUNTER_EXTRA)}")
    if type(item_level) is not int or not 200 <= item_level <= 284:
        raise LayoutError("item level must be 200-284")
    if world.running(layout):
        raise world.WorldError("the encounter needs a stopped dedicated dojo instance")
    preflight = world.preflight(layout)
    mysql.verify_identity(layout, mysql.Client(layout, layout.server.root_cnf))
    source = provenance(layout)
    dojo = read_conf(layout.configs / "modules/mod_dojo.conf")
    leeroy = read_conf(layout.configs / "modules/mod_leeroy.conf")
    if dojo.get("Dojo.Scenario.Manifest") or any(leeroy.get(key) != "0" for key in (
            "Leeroy.Enable", "Leeroy.HumanGuid", "Leeroy.GuildId")):
        raise LayoutError("raid realm needs no arena manifest and Leeroy Enable/HumanGuid/GuildId = 0")
    out = layout.data / "runs" / (time.strftime("%Y%m%d_%H%M%S") + "-valithria-" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    results = []
    report = dict(kind="dojo-valithria-encounters", format=1, ok=False, source=source, preflight=preflight,
                  item_level=item_level, owner_buffs=owner_buffs, runs=results, stopped=False, out=str(out))
    started = False
    seen = {}
    try:
        started = True
        world.start(layout, log=log)
        with world.ra(layout) as client:
            for item in runs:
                difficulty, mode = item.split(":")
                token = uuid.uuid4().hex
                name = f"{difficulty}-{mode}" + ("-buffed" if owner_buffs else "")
                # A repeated run keeps its own files: 10n-after, 10n-after-2, ...
                seen[name] = seen.get(name, 0) + 1
                name += f"-{seen[name]}" if seen[name] > 1 else ""
                log(f"valithria {name}: starting")
                reply = client.command(f"dojo raid encounter start {token} {difficulty} {mode} {item_level}"
                                       + (" buffed" if owner_buffs else ""))
                if "dojo encounter: started" not in reply:
                    raise world.WorldError(f"native encounter start refused: {reply}")
                deadline = time.monotonic() + ENCOUNTER_DEADLINE_S
                last = None
                with (out / f"{name}.jsonl").open("w", encoding="utf-8") as trace:
                    while True:
                        state = parse_encounter(client.command("dojo raid encounter status"))
                        trace.write(json.dumps(state, sort_keys=True) + "\n")
                        trace.flush()
                        if state.get("token") != token:
                            raise world.WorldError("run token changed while observing")
                        if state.get("state") in {"done", "cleanup_failed"}:
                            break
                        progress = (state.get("state"), round(state.get("hp", 0)))
                        if progress != last:
                            last = progress
                            log(f"valithria {name}: {state.get('state')} hp={state.get('hp', 0):.1f} "
                                f"t={state.get('pull_ms', 0) // 1000}s")
                        if time.monotonic() >= deadline:
                            client.command("dojo raid encounter abort")
                            raise world.WorldError("native encounter/cleanup deadline exceeded")
                        time.sleep(1.0)
                final = parse_encounter(client.command("dojo raid encounter status full"))
                (out / f"{name}.json").write_text(json.dumps(final, indent=2) + "\n", encoding="utf-8")
                result = dict(name=name, observation=final,
                              **encounter_verdict(final, token=token, difficulty=difficulty, mode=mode,
                                                  owner_buffs=owner_buffs))
                results.append(result)
                log(f"valithria {name}: {final.get('outcome')} peak={final.get('peak_hp')} "
                    f"{'killed' if result['killed'] else 'not killed'}{'' if result['ok'] else ' (problems)'}")
                if not final.get("cleanup"):
                    raise world.WorldError("fixture cleanup unproved; refusing the next pull")
        report["ok"] = len(results) == len(runs) and all(r["ok"] for r in results)
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
