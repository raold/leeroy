"""Observe real queue membership for fixed synthetic bots on an isolated realm."""
from __future__ import annotations

import json
import shutil
import time
import uuid

from . import mysql, offline, raids, world
from .layout import LayoutError, read_conf
from .ra import RaError


def verdict(o, *, token):
    plumbing, behavior = [], []
    for key, expected in (("kind", "dojo-queue-observation"), ("format", 1),
                          ("token", token), ("state", "done"), ("cleanup", True)):
        if type(o.get(key)) is not type(expected) or o[key] != expected:
            plumbing.append("invalid " + key)
    if o.get("failure") != "":
        behavior.append("native failure or missing failure field")
    if o.get("control_queued") is not True or o.get("control_left") is not True:
        plumbing.append("eligible bot did not enter and leave the actual queue")
    if type(o.get("max_gap_ms")) is not int or not 0 < o["max_gap_ms"] <= 200:
        plumbing.append("native observation cadence not proved")
    cases = o.get("cases")
    wanted = ("raid", "pinned_solo", "pinned_party", "dungeon")
    if type(cases) is not list or len(cases) != 4 or any(type(c) is not dict for c in cases):
        plumbing.append("four protected contexts not proved")
        cases = []
    elif tuple(c.get("case") for c in cases) != wanted:
        plumbing.append("wrong or repeated protected contexts")
    for c in cases:
        if any(type(c.get(k)) is not int or c[k] < 0 for k in
               ("attempts", "samples", "queued_samples", "map", "instance", "group")):
            plumbing.append("invalid protected observation counters")
            continue
        if c["attempts"] < 10 or c["samples"] < 20:
            plumbing.append("insufficient protected queue attempts or observations")
        if c["queued_samples"] != 0:
            behavior.append("protected bot entered a queue")
        name = c.get("case")
        self_pin, peer_pin = name == "pinned_solo", name == "pinned_party"
        if c.get("pinned_self") is not self_pin or c.get("pinned_peer") is not peer_pin:
            plumbing.append("pin context not proved")
        if (name in ("raid", "pinned_party")) != (c["group"] > 0):
            behavior.append("group context not preserved")
        if name == "dungeon" and (c["map"] != 36 or c["instance"] <= 0):
            plumbing.append("dungeon context not proved")
    return dict(ok=not plumbing and not behavior, problems=plumbing + behavior,
                plumbing_problems=plumbing, behavior_problems=behavior)


def parse_status(text):
    rows = [line[len("dojo-queue "):] for line in text.splitlines() if line.startswith("dojo-queue ")]
    if len(rows) != 1:
        raise world.WorldError("native queue observer missing or ambiguous")
    return json.loads(rows[0])


def run(layout, *, log=print, before_case=None):
    if world.running(layout):
        raise world.WorldError("queue acceptance needs a stopped dedicated dojo instance")
    preflight = world.preflight(layout)
    mysql.verify_identity(layout, mysql.Client(layout, layout.server.root_cnf))
    source = raids.provenance(layout)
    dojo = read_conf(layout.configs / "modules/mod_dojo.conf")
    leeroy = read_conf(layout.configs / "modules/mod_leeroy.conf")
    if dojo.get("Dojo.Scenario.Manifest") or any(leeroy.get(k) != "0" for k in
            ("Leeroy.Enable", "Leeroy.HumanGuid", "Leeroy.GuildId")):
        raise LayoutError("queue realm requires disabled, unbound Leeroy and no arena manifest")
    token = uuid.uuid4().hex
    out = layout.data / "runs" / (time.strftime("%Y%m%d_%H%M%S") + "-queue-" + token[:8])
    out.mkdir(parents=True, exist_ok=False)
    report = dict(kind="dojo-queue-acceptance", format=1, ok=False, source=source,
                  preflight=preflight, token=token, out=str(out), stopped=False)
    started = False
    try:
        offline._fixture_accounts(layout, log)
        # Preparation may briefly start this owned realm if there is no usable
        # checkpoint. Its failures need the same shutdown guarantee as a trial.
        started = True
        prepared, checkpoint = offline.prepare_fixture(layout, log)
        report.update(fixture=prepared, fixture_checkpoint_sha256=checkpoint)
        if before_case is not None:
            before_case("queue", out)
        world.start(layout, log=log)
        with world.ra(layout) as client:
            reply = client.command(f"dojo queue start {token}")
            if "dojo queue: started" not in reply:
                raise world.WorldError("native queue start refused")
            deadline = time.monotonic() + 380
            with (out / "observations.jsonl").open("w", encoding="utf-8") as trace:
                while True:
                    state = parse_status(client.command("dojo queue status"))
                    trace.write(json.dumps(state, sort_keys=True) + "\n")
                    trace.flush()
                    if state.get("token") != token:
                        raise world.WorldError("queue token changed")
                    if state.get("state") in ("done", "cleanup_failed"):
                        break
                    if time.monotonic() >= deadline:
                        client.command("dojo queue abort")
                        raise world.WorldError("queue trial or cleanup timed out")
                    time.sleep(0.5)
            report.update(observation=state, **verdict(state, token=token))
    except (world.WorldError, RaError, OSError, ValueError) as error:
        # External errors may contain server text; retain their type only.
        report["error"] = type(error).__name__
    finally:
        if started and world.running(layout):
            try:
                world.stop(layout, log=log)
            except (world.WorldError, RaError, OSError) as error:
                report["shutdown_error"] = type(error).__name__
        report["stopped"] = not world.running(layout)
        report["ok"] = report["ok"] and report["stopped"]
        if started and report["stopped"]:
            for name in ("Server.log", "Playerbots.log"):
                if (layout.logs / name).is_file():
                    shutil.copyfile(layout.logs / name, out / name)
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
