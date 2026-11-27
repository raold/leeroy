"""Observe the production lead handler and ordinary bot movement on the isolated world."""
import json
import math
from pathlib import Path
import shutil
import time
import uuid

from . import mysql, offline, raids, world
from .layout import LayoutError, read_conf
from .ra import RaError


def verdict(o, *, token, negative=False):
    problems = []
    expected = dict(kind="dojo-lead-observation", format=1, token=token, state="done", ready=True,
                    cleanup=True, restored=True, failure="off" if negative else "", refused_off=negative)
    for key, value in expected.items():
        if type(o.get(key)) is not type(value) or o[key] != value:
            problems.append(f"invalid {key}")
    for key in ("samples", "follow_samples", "wait_ms", "elapsed_ms", "max_gap_ms"):
        if type(o.get(key)) is not int or o[key] < 0:
            problems.append(f"invalid {key}")
    for key in ("leader_distance", "follower_distance", "resumed_distance"):
        if type(o.get(key)) not in (float, int) or not math.isfinite(o[key]) or o[key] < 0:
            problems.append(f"invalid {key}")
    if problems:
        return dict(ok=False, problems=problems)
    if not negative and (o["samples"] < 10 or o["leader_distance"] < 20 or o["follower_distance"] < 10
                         or o["follow_samples"] < 10 or o["wait_ms"] < 3000 or o.get("waited") is not True
                         or o["resumed_distance"] < 10 or o["max_gap_ms"] > 200):
        problems.append("walking, following, waiting and resuming were not all observed")
    return dict(ok=not problems, problems=problems)


def parse_status(reply):
    rows = [line.removeprefix("dojo-lead ") for line in reply.splitlines() if line.startswith("dojo-lead ")]
    if len(rows) != 1:
        raise world.WorldError("native lead observation missing or ambiguous")
    return json.loads(rows[0])


def run(layout, log=print):
    if world.running(layout):
        raise world.WorldError("lead acceptance requires a stopped dedicated dojo realm")
    preflight = world.preflight(layout)
    mysql.verify_identity(layout, mysql.Client(layout, layout.server.root_cnf))
    configured = json.loads((Path(layout.roots["build"]) / "dojo-configure.json").read_text(encoding="utf-8"))
    if not configured.get("offline_acceptance"):
        raise LayoutError("lead acceptance requires the synthetic-guild OfflineAcceptance build")
    source = raids.provenance(layout)
    before = read_conf(layout.configs / "modules/mod_leeroy.conf")
    if before.get("Leeroy.Enable") != "0":
        raise LayoutError("provision the disabled synthetic guild first")
    offline._fixture_accounts(layout, log)
    out = layout.data / "runs" / (time.strftime("%Y%m%d_%H%M%S") + "-lead-" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    report = dict(kind="dojo-lead-acceptance", format=1, ok=False, source=source, preflight=preflight,
                  cases=[], stopped=False, out=str(out), scope="native command decoding, handler and ordinary AI")
    try:
        for negative in (True, False):
            fixture, digest = offline.prepare_fixture(layout, log)
            offline._set(layout, "modules/mod_leeroy.conf", {"Leeroy.Enable": "1", "Leeroy.GuildId": str(fixture["guild"]),
                "Leeroy.HumanGuid": str(fixture["owner"]), "Leeroy.Lead.Enable": "0" if negative else "1"})
            token = uuid.uuid4().hex
            name = "negative" if negative else "normal"
            candidate = raids.provenance(layout)
            world.start(layout, log=log)
            with world.ra(layout) as client:
                reply = client.command(f"dojo lead start {token}")
                if "dojo lead: started" not in reply:
                    raise world.WorldError(f"lead fixture admission refused: {reply}")
                deadline = time.monotonic() + 260
                with (out / f"{name}.jsonl").open("w", encoding="utf-8") as trace:
                    while time.monotonic() < deadline:
                        observed = parse_status(client.command("dojo lead status"))
                        trace.write(json.dumps(observed | {"observed_at": time.time()}, sort_keys=True) + "\n")
                        trace.flush()
                        if observed.get("token") != token:
                            raise world.WorldError("lead run token changed")
                        if observed.get("state") in {"done", "cleanup_failed"}:
                            break
                        time.sleep(0.5)
                    else:
                        client.command("dojo lead abort")
                        raise world.WorldError("lead fixture deadline exceeded")
                case = dict(name=name, fixture_checkpoint_sha256=digest, source=candidate, observation=observed,
                            **verdict(observed, token=token, negative=negative))
                report["cases"].append(case)
                log(f"lead {name}: {'passed' if case['ok'] else 'FAILED'}")
                if not observed.get("cleanup"):
                    raise world.WorldError("lead cleanup unproved")
            world.stop(layout, log=log)
            for filename in ("Server.log", "Playerbots.log"):
                if (layout.logs / filename).is_file():
                    shutil.copyfile(layout.logs / filename, out / f"{name}-{filename}")
            offline._set(layout, "modules/mod_leeroy.conf", {"Leeroy.Enable": "0", "Leeroy.GuildId": "0", "Leeroy.HumanGuid": "0"})
        report["ok"] = len(report["cases"]) == 2 and all(case["ok"] for case in report["cases"])
    except (world.WorldError, RaError, OSError, ValueError) as error:
        report["error"] = str(error)
    finally:
        try:
            if world.running(layout):
                world.stop(layout, log=log)
            report["stopped"] = not world.running(layout)
        except (world.WorldError, RaError, OSError) as error:
            report["shutdown_error"] = str(error)
        if report["stopped"]:
            offline._set(layout, "modules/mod_leeroy.conf", {"Leeroy.Enable": "0", "Leeroy.GuildId": "0", "Leeroy.HumanGuid": "0",
                "Leeroy.Lead.Enable": before.get("Leeroy.Lead.Enable", "0")})
        report["ok"] = report["ok"] and report["stopped"]
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
