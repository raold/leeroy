"""A real offline job, synthetic guild and observed boss state on the isolated realm."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import time
import uuid

from . import conf, mysql, provision, raids, world
from .layout import LayoutError, read_conf
from .ra import RaError
from .mechanics import MechanicConnection
from .offline_fixture import FixtureCheckpoint

TARGETS = {"toc-beasts": ("toc", 649, 0), "ulduar-xt": ("ulduar", 603, 3),
           "icc-deathwhisper": ("icc", 631, 1), "icc-gunship": ("icc", 631, 2),
           "icc-valithria": ("icc", 631, 10)}


def parse_status(reply):
    rows = [line.removeprefix("dojo-offline ") for line in reply.splitlines() if line.startswith("dojo-offline ")]
    if len(rows) != 1:
        raise world.WorldError("offline native observation missing or ambiguous")
    value = json.loads(rows[0])
    if value.get("kind") != "dojo-offline-observation" or value.get("format") != 1:
        raise world.WorldError("unsupported offline observation")
    return value


def reached(observation, *, target, token):
    """Only the actual instance boss state counts. A request or kill count cannot stand in for it."""
    _, map_id, boss = TARGETS[target]
    if observation.get("token") != token or observation.get("error") or type(observation.get("samples")) is not int or observation["samples"] < 10:
        return False
    native = observation.get("native", {})
    event = native.get("event", {})
    instance = event.get("instance_id", 0)
    if event.get("id") != token or type(instance) is not int or instance <= 0 or native.get("stage") != "running":
        return False
    bodies = observation.get("bodies", [])
    return any(b.get("slot") == 1 and b.get("map") == map_id and b.get("instance") == instance
               and len(b.get("bosses", [])) > boss and b["bosses"][boss] == 3 for b in bodies)


def _set(layout, name, values):
    path = layout.configs / name
    path.write_text(conf.render(path.read_text(encoding="utf-8"), values), encoding="utf-8")


def _fixture_accounts(layout, log):
    provision.ensure_accounts(layout, "DOJORAID", log=log)
    accounts = provision.ensure_accounts(layout, "RNDBOTDOJO", log=log)
    account = accounts["RNDBOTDOJO"]
    if type(account) is not int or account <= 0:
        raise LayoutError("synthetic random-bot account was not identified")
    root = mysql.Client(layout, layout.server.root_cnf)
    mysql.verify_identity(layout, root)
    # MaxRandomBots stays zero: no population is created. The fixed synthetic
    # account still needs the ordinary type-1 assignment used by cast pinning.
    root.query(f"INSERT INTO playerbots_account_type (account_id, account_type) VALUES ({account}, 1) "
               "ON DUPLICATE KEY UPDATE account_type=1;", database=layout.schemas["playerbots"])


def run(layout, *, target, log=print):
    if target not in TARGETS:
        raise LayoutError("unsupported offline target")
    if world.running(layout):
        raise world.WorldError("offline acceptance requires a stopped dedicated dojo realm")
    preflight = world.preflight(layout)
    mysql.verify_identity(layout, mysql.Client(layout, layout.server.root_cnf))
    configured = json.loads((Path(layout.roots["build"]) / "dojo-configure.json").read_text(encoding="utf-8"))
    if not configured.get("offline_acceptance") or "dungeonclear" not in layout.roots:
        raise LayoutError("requires an attested OfflineAcceptance build and isolated dungeonclear checkout")
    source = raids.provenance(layout)
    settings = read_conf(layout.configs / "modules/mod_leeroy.conf")
    if settings.get("Leeroy.Enable") != "0" or settings.get("Leeroy.Bridge.Host") != "127.0.0.1" or settings.get("Leeroy.Bridge.Port") != str(layout.ports.get("leeroy")):
        raise LayoutError("provision the disabled isolated guild and separate bridge endpoint first")
    _fixture_accounts(layout, log)
    token = "dojo-" + uuid.uuid4().hex
    out = layout.data / "runs" / (time.strftime("%Y%m%d_%H%M%S") + "-offline-" + token)
    out.mkdir(parents=True, exist_ok=False)
    report = dict(kind="dojo-offline-acceptance", format=1, ok=False, source=source, preflight=preflight,
                  token=token, target=target, out=str(out), reached=False, cleanup=False, stopped=False)
    feed = None
    try:
        checkpoint = FixtureCheckpoint(layout)
        prepared = checkpoint.restore()
        if prepared is None:
            world.start(layout, log=log)
            with world.ra(layout) as client:
                if "dojo offline: started" not in client.command("dojo offline prepare"):
                    raise world.WorldError("synthetic guild preparation refused")
                deadline = time.monotonic() + 360
                while time.monotonic() < deadline:
                    prepared = parse_status(client.command("dojo offline status"))
                    if prepared.get("error"):
                        raise world.WorldError(prepared["error"])
                    if prepared.get("state") == "prepared":
                        break
                    time.sleep(0.5)
                else:
                    raise world.WorldError("synthetic guild preparation timed out")
            world.stop(layout, log=log)
            checkpoint.save(prepared)
        report["fixture_checkpoint_sha256"] = checkpoint.digest(checkpoint.receipt)
        if type(prepared.get("guild")) is not int or prepared["guild"] <= 0 or type(prepared.get("owner")) is not int or prepared["owner"] <= 0:
            raise world.WorldError("preparation did not identify the synthetic guild")
        report["fixture"] = prepared
        _set(layout, "modules/mod_leeroy.conf", {"Leeroy.Enable": "1", "Leeroy.GuildId": str(prepared["guild"]),
                                                "Leeroy.HumanGuid": str(prepared["owner"]),
                                                "Leeroy.Mechanics.Enable": "1", "Leeroy.Hazards.Enable": "1"})
        if target == "icc-valithria":
            _set(layout, "modules/mod_dungeon_clear.conf", {"DungeonClear.OfflineValithria": "1"})
        report["run_source"] = raids.provenance(layout)
        world.start(layout, log=log)
        feed = MechanicConnection(layout, guild=prepared["guild"], map_id=TARGETS[target][1])
        feed.__enter__()
        deadline = time.monotonic() + 30
        while not feed.ready and not feed.error and time.monotonic() < deadline:
            time.sleep(0.2)
        if not feed.ready:
            raise world.WorldError(feed.error or "candidate mechanic feed timed out")
        report["mechanic_feed"] = dict(features=feed.features, rules=feed.rules)
        with world.ra(layout) as client:
            scenario = TARGETS[target][0]
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                reply = client.command(f"dojo offline start {scenario} {token}")
                if "dojo offline: started" in reply:
                    break
                if not any(code in reply for code in ("actors_pending", "driver_pending")):
                    raise world.WorldError(f"offline admission refused: {reply}")
                time.sleep(1)
            else:
                raise world.WorldError("offline admission timed out")
            log(f"offline {target}: observing {token}")
            deadline = time.monotonic() + 3000
            last_stage = None
            with (out / "observations.jsonl").open("w", encoding="utf-8") as trace:
                while time.monotonic() < deadline:
                    observed = parse_status(client.command("dojo offline status"))
                    if not feed.ready or feed.error:
                        client.command("dojo offline stop")
                        raise world.WorldError("candidate mechanic connection lost; comparison is invalid")
                    observed["observed_at"] = time.time()
                    trace.write(json.dumps(observed, sort_keys=True) + "\n")
                    trace.flush()
                    native = observed.get("native", {})
                    if observed.get("token") != token or native.get("event", {}).get("id") != token:
                        raise world.WorldError("offline run token changed")
                    stage = native.get("stage")
                    if stage != last_stage:
                        log(f"offline {target}: {stage or native.get('event', {}).get('state', 'unknown')}")
                        last_stage = stage
                    if reached(observed, target=target, token=token) and not report["reached"]:
                        report["reached"] = True
                        report["milestone"] = observed
                        client.command("dojo offline stop")
                    if not native.get("active", True):
                        report["terminal"] = observed
                        report["cleanup"] = native.get("cleanup") is True
                        break
                    time.sleep(1)
                else:
                    client.command("dojo offline stop")
                    raise world.WorldError("offline runtime deadline exceeded")
        report["ok"] = report["reached"] and report["cleanup"]
    except (world.WorldError, RaError, OSError, ValueError) as error:
        report["error"] = str(error)
    finally:
        if feed is not None:
            feed.__exit__()
        try:
            if world.running(layout):
                world.stop(layout, log=log)
            report["stopped"] = not world.running(layout)
        except (world.WorldError, RaError, OSError) as error:
            report["shutdown_error"] = str(error)
        if report["stopped"]:
            _set(layout, "modules/mod_leeroy.conf", {"Leeroy.Enable": "0", "Leeroy.GuildId": "0", "Leeroy.HumanGuid": "0"})
            _set(layout, "modules/mod_dungeon_clear.conf", {"DungeonClear.OfflineValithria": "0"})
            # Preserve the actual isolated native evidence before the next run rotates it.
            for name in ("Server.log", "DungeonClear.log", "Playerbots.log"):
                path = layout.logs / name
                if path.is_file():
                    shutil.copyfile(path, out / name)
        report["ok"] = report["ok"] and report["stopped"]
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
