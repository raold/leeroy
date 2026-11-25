"""Real vehicle boarding and combat, using only dedicated synthetic identities."""
import json
import math
from pathlib import Path
import shutil
import time
import uuid

from . import mysql, offline, raids, world
from .layout import LayoutError, read_conf
from .ra import RaError


def verdict(o, *, token, negative=False, scenario="leviathan"):
    problems = []
    expected = dict(kind="dojo-vehicle-observation", format=1, scenario=scenario, token=token,
                    state="done", negative=negative, ready=True, human_boarded=True, cleanup=True)
    for key, value in expected.items():
        if type(o.get(key)) is not type(value) or o[key] != value:
            problems.append(f"invalid {key}")
    allowed_failures = {""}
    if negative and scenario == "malygos":
        allowed_failures |= {"drake_driver_or_mount_lost", "drake_encounter_lost"}
    if not isinstance(o.get("failure"), str) or o["failure"] not in allowed_failures:
        problems.append("invalid failure")
    if scenario not in {"malygos", "leviathan"}:
        problems.append("unsupported scenario")
    for key in ("samples", "max_gap_ms", "human_death_ms", "boss_start_health", "boss_least_health"):
        if type(o.get(key)) is not int or o[key] < 0:
            problems.append(f"invalid {key}")
    for key in ("driver_entries", "damage_hits", "moving_hits", "after_death_hits", "distance"):
        rows = o.get(key)
        if not isinstance(rows, list) or len(rows) != 2 or any(
            type(v) not in ((int, float) if key == "distance" else (int,)) or not math.isfinite(v) or v < 0 for v in rows
        ):
            problems.append(f"invalid {key}")
    if problems:
        return dict(ok=False, problems=problems)
    if o["samples"] < 20 or o["max_gap_ms"] > 200:
        problems.append("continuous fixture observation missing")
    if scenario == "malygos":
        for key in ("setup_add_kills", "heal_events", "effective_healing", "dot_samples", "field_samples"):
            if type(o.get(key)) is not int or o[key] < 0:
                problems.append(f"invalid {key}")
        for key in ("surge_samples", "shield_samples", "safe_ring_samples", "field_escapes", "longest_field_ms"):
            if not isinstance(o.get(key), list) or len(o[key]) != 2 or any(type(v) is not int or v < 0 for v in o[key]):
                problems.append(f"invalid {key}")
        if problems:
            return dict(ok=False, problems=problems)
        if o["driver_entries"] != [30161, 30161] or o["setup_add_kills"] != 6:
            problems.append("native phase-three drakes were not established")
        if o["field_samples"] < 1 or sum(o["surge_samples"]) < 1:
            problems.append("actual static fields and surge exposure missing")
        if negative:
            if o["heal_events"] or o["dot_samples"] or any(o["damage_hits"]) or any(o["shield_samples"]):
                problems.append("strategy-disabled drakes still caused vehicle effects")
        else:
            if o["heal_events"] < 2 or o["effective_healing"] < 1 or o["dot_samples"] < 20 or o["damage_hits"][1] < 2:
                problems.append("actual drake healing, damage and Engulf in Flames missing")
            if any(n < 1 for n in o["shield_samples"]) or any(n < 20 for n in o["safe_ring_samples"]):
                problems.append("both drakes require shield during real surge and safe ring movement")
            if any(n < 20 for n in o["distance"]) or o["boss_least_health"] >= o["boss_start_health"]:
                problems.append("drake movement and native boss health loss missing")
            if sum(o["field_escapes"]) < 1 or any(n > 4000 for n in o["longest_field_ms"]):
                problems.append("an actual static field must be escaped within four seconds")
    elif negative:
        if any(o[key] != [0, 0] for key in ("driver_entries", "damage_hits", "moving_hits", "after_death_hits", "distance")):
            problems.append("strategy-disabled control had vehicle effects")
    else:
        if o["driver_entries"] != [33060, 33109]:
            problems.append("melee siege driver and ranged demolisher driver were not observed")
        if o["samples"] < 20 or o["max_gap_ms"] > 200 or o["human_death_ms"] < 20000:
            problems.append("continuous combat and human death stimulus were not observed")
        if o["boss_start_health"] <= 0 or o["boss_least_health"] >= o["boss_start_health"]:
            problems.append("native boss health did not decrease")
        for key, minimum in (("distance", 20), ("damage_hits", 2), ("moving_hits", 1), ("after_death_hits", 2)):
            if any(value < minimum for value in o[key]):
                problems.append(f"both actual vehicles require {key}")
    return dict(ok=not problems, problems=problems)


def parse_status(reply):
    rows = [line.removeprefix("dojo-vehicle ") for line in reply.splitlines() if line.startswith("dojo-vehicle ")]
    if len(rows) != 1:
        raise world.WorldError("native vehicle observation missing or ambiguous")
    return json.loads(rows[0])


def run(layout, *, scenarios=("leviathan", "malygos"), log=print):
    if not scenarios or len(set(scenarios)) != len(scenarios) or any(s not in {"leviathan", "malygos"} for s in scenarios):
        raise LayoutError("choose distinct supported vehicle scenarios")
    if world.running(layout):
        raise world.WorldError("vehicle acceptance requires a stopped dedicated dojo realm")
    preflight = world.preflight(layout)
    mysql.verify_identity(layout, mysql.Client(layout, layout.server.root_cnf))
    configured = json.loads((Path(layout.roots["build"]) / "dojo-configure.json").read_text(encoding="utf-8"))
    if not configured.get("offline_acceptance"):
        raise LayoutError("vehicle acceptance requires the synthetic-guild OfflineAcceptance build")
    source = raids.provenance(layout)
    if read_conf(layout.configs / "modules/mod_leeroy.conf").get("Leeroy.Enable") != "0":
        raise LayoutError("provision the disabled synthetic guild first")
    offline._fixture_accounts(layout, log)
    out = layout.data / "runs" / (time.strftime("%Y%m%d_%H%M%S") + "-vehicles-" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    report = dict(kind="dojo-vehicle-acceptance", format=1, ok=False, source=source, preflight=preflight,
                  cases=[], stopped=False, out=str(out), scenarios=list(scenarios),
                  scope="Flame Leviathan drivers after the gauntlet; Malygos phase-three drake mechanics")
    try:
        for scenario, negative in ((s, n) for s in scenarios for n in (True, False)):
            _, digest = offline.prepare_fixture(layout, log)
            token = uuid.uuid4().hex
            mode = "negative" if negative else "normal"
            name = scenario + "-" + mode
            candidate = raids.provenance(layout)
            world.start(layout, log=log)
            with world.ra(layout) as client:
                reply = client.command(f"dojo vehicle start {scenario} {token} {mode}")
                if "dojo vehicle: started" not in reply:
                    raise world.WorldError(f"vehicle fixture admission refused: {reply}")
                deadline = time.monotonic() + 380
                with (out / f"{name}.jsonl").open("w", encoding="utf-8") as trace:
                    while time.monotonic() < deadline:
                        observed = parse_status(client.command("dojo vehicle status"))
                        trace.write(json.dumps(observed | {"observed_at": time.time()}, sort_keys=True) + "\n")
                        trace.flush()
                        if observed.get("token") != token:
                            raise world.WorldError("vehicle run token changed")
                        if observed.get("state") in {"done", "cleanup_failed"}:
                            break
                        time.sleep(0.5)
                    else:
                        client.command("dojo vehicle abort")
                        raise world.WorldError("vehicle fixture deadline exceeded")
                case = dict(name=name, fixture_checkpoint_sha256=digest, source=candidate, observation=observed,
                            **verdict(observed, token=token, negative=negative, scenario=scenario))
                report["cases"].append(case)
                log(f"vehicles {name}: {'passed' if case['ok'] else 'FAILED'}")
                if not observed.get("cleanup"):
                    raise world.WorldError("vehicle cleanup unproved")
            world.stop(layout, log=log)
            for filename in ("Server.log", "Playerbots.log"):
                if (layout.logs / filename).is_file():
                    shutil.copyfile(layout.logs / filename, out / f"{name}-{filename}")
        report["ok"] = len(report["cases"]) == len(scenarios) * 2 and all(case["ok"] for case in report["cases"])
    except (world.WorldError, RaError, OSError, ValueError) as error:
        report["error"] = str(error)
    finally:
        try:
            if world.running(layout):
                world.stop(layout, log=log)
            report["stopped"] = not world.running(layout)
        except (world.WorldError, RaError, OSError) as error:
            report["shutdown_error"] = str(error)
        report["ok"] = report["ok"] and report["stopped"]
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
