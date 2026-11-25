"""Observe a native Yogg portal wave, with real brain-team effects and exits."""
import json
from pathlib import Path
import shutil
import time
import uuid

from . import mysql, offline, raids, world
from .layout import LayoutError, read_conf
from .ra import RaError


def verdict(o, *, token, negative=False):
    problems = []
    expected = dict(kind="dojo-yogg-observation", format=1, token=token, state="done", failure="",
                    negative=negative, ready=True, cleanup=True, portals=4, initial_sanity_members=9)
    for key, value in expected.items():
        if type(o.get(key)) is not type(value) or o[key] != value:
            problems.append(f"invalid {key}")
    counts = ("samples", "max_gap_ms", "illusion", "max_below", "max_healers_below", "healer_entrants", "min_healers_above",
              "tank_below_samples", "influence_deaths", "brain_hits", "brain_start_health", "brain_least_health",
              "exit_remaining_ms", "native_spell_teleports")
    for key in counts:
        if type(o.get(key)) is not int or o[key] < 0:
            problems.append(f"invalid {key}")
    for key in ("entered", "exited", "portal_casts", "exit_casts", "minimum_sanity"):
        row = o.get(key)
        kind = bool if key in {"entered", "exited"} else int
        if not isinstance(row, list) or len(row) != 9 or any(type(v) is not kind or v < 0 for v in row):
            problems.append(f"invalid {key}")
    if problems:
        return dict(ok=False, problems=problems)
    if o["illusion"] not in {1, 2, 3} or o["samples"] < 20 or o["max_gap_ms"] > 200:
        problems.append("native illusion and continuous observation missing")
    if any(n < 1 or n > 100 for n in o["minimum_sanity"]):
        problems.append("every actor must retain sanity")
    if negative:
        if any(o["entered"]) or any(o["portal_casts"]) or o["brain_hits"] or o["influence_deaths"]:
            problems.append("strategy-disabled control entered or affected the brain room")
    else:
        if sum(o["entered"]) != 4 or o["entered"] != o["exited"]:
            problems.append("exactly four actual entrants must return alive")
        if o["max_below"] != 4 or o["max_healers_below"] != 1 or o["healer_entrants"] != 1 or o["min_healers_above"] < 1 or o["tank_below_samples"]:
            problems.append("brain-team roles or upstairs healer reserve invalid")
        for i, entered in enumerate(o["entered"]):
            if entered and (o["portal_casts"][i] < 1 or o["exit_casts"][i] < 1):
                problems.append("arrival requires real portal and exit spell effects")
        if not o["influence_deaths"] or not o["brain_hits"] or o["brain_least_health"] >= o["brain_start_health"]:
            problems.append("native illusion kills and brain damage missing")
        if o["exit_remaining_ms"] < 1000:
            problems.append("team did not return before Induce Madness")
    return dict(ok=not problems, problems=problems)


def parse_status(reply):
    rows = [line.removeprefix("dojo-yogg ") for line in reply.splitlines() if line.startswith("dojo-yogg ")]
    if len(rows) != 1:
        raise world.WorldError("native Yogg observation missing or ambiguous")
    return json.loads(rows[0])


def run(layout, log=print):
    if world.running(layout):
        raise world.WorldError("Yogg acceptance requires a stopped dedicated dojo realm")
    preflight = world.preflight(layout)
    mysql.verify_identity(layout, mysql.Client(layout, layout.server.root_cnf))
    configured = json.loads((Path(layout.roots["build"]) / "dojo-configure.json").read_text(encoding="utf-8"))
    if not configured.get("offline_acceptance"):
        raise LayoutError("Yogg acceptance requires the synthetic-guild OfflineAcceptance build")
    source = raids.provenance(layout)
    if read_conf(layout.configs / "modules/mod_leeroy.conf").get("Leeroy.Enable") != "0":
        raise LayoutError("provision the disabled synthetic guild first")
    offline._fixture_accounts(layout, log)
    out = layout.data / "runs" / (time.strftime("%Y%m%d_%H%M%S") + "-yogg-" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True, exist_ok=False)
    report = dict(kind="dojo-yogg-acceptance", format=1, ok=False, source=source, preflight=preflight,
                  cases=[], stopped=False, out=str(out), scope="one native portal wave; report names the actual illusion")
    try:
        for negative in (True, False):
            _, digest = offline.prepare_fixture(layout, log)
            token = uuid.uuid4().hex
            name = "negative" if negative else "normal"
            candidate = raids.provenance(layout)
            world.start(layout, log=log)
            with world.ra(layout) as client:
                reply = client.command(f"dojo yogg start {token} {name}")
                if "dojo yogg: started" not in reply:
                    raise world.WorldError(f"Yogg fixture admission refused: {reply}")
                deadline = time.monotonic() + 350
                with (out / f"{name}.jsonl").open("w", encoding="utf-8") as trace:
                    while time.monotonic() < deadline:
                        observed = parse_status(client.command("dojo yogg status"))
                        trace.write(json.dumps(observed | {"observed_at": time.time()}, sort_keys=True) + "\n")
                        trace.flush()
                        if observed.get("token") != token:
                            raise world.WorldError("Yogg run token changed")
                        if observed.get("state") in {"done", "cleanup_failed"}:
                            break
                        time.sleep(0.5)
                    else:
                        client.command("dojo yogg abort")
                        raise world.WorldError("Yogg fixture deadline exceeded")
                case = dict(name=name, fixture_checkpoint_sha256=digest, source=candidate, observation=observed,
                            **verdict(observed, token=token, negative=negative))
                report["cases"].append(case)
                log(f"Yogg {name}: {'passed' if case['ok'] else 'FAILED'}")
                if not observed.get("cleanup"):
                    raise world.WorldError("Yogg cleanup unproved")
            world.stop(layout, log=log)
            for filename in ("Server.log", "Playerbots.log"):
                if (layout.logs / filename).is_file():
                    shutil.copyfile(layout.logs / filename, out / f"{name}-{filename}")
        report["ok"] = len(report["cases"]) == 2 and all(case["ok"] for case in report["cases"])
        report["illusions_passed"] = sorted({case["observation"]["illusion"] for case in report["cases"]
                                            if case["ok"] and case["name"] == "normal"})
        report["all_illusions_covered"] = set(report["illusions_passed"]) == {1, 2, 3}
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
