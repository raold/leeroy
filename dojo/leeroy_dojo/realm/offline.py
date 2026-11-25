"""A real offline job, synthetic guild and observed boss state on the isolated realm."""
from __future__ import annotations

import json
import math
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
           "naxx-gothik-pass": ("naxx-gothik-pass", 533, None),
           "naxx-gothik-pass-control": ("naxx-gothik-pass-control", 533, None),
           "toc-full": ("toc-full", 649, None), "ulduar-full": ("ulduar-full", 603, None),
           "ulduar-route": ("ulduar-full", 603, None),
           "toc-slime-pools": ("toc", 649, None), "toc-slime-pools-control": ("toc", 649, None),
           "icc-deathwhisper": ("icc", 631, 1), "icc-gunship": ("icc", 631, 2),
           "icc-valithria": ("icc", 631, 10), "offline-reserves": ("toc-reserves", 649, None),
           "ulduar-restart": ("ulduar", 603, None), "ulduar-tank-restart": ("ulduar", 603, None),
           "ulduar-forced-tank-restart": ("ulduar", 603, None)}

# Ulduar's production offline route currently supports five encounters.
# The omitted nine remain explicitly unverified, never counted as kills.
FULL_ROUTES = {"toc-full": (5, tuple(range(5))), "ulduar-full": (14, tuple(range(14))),
               "ulduar-route": (14, (1, 2, 3, 5, 6))}


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
    if event.get("id") != token or type(instance) is not int or instance <= 0:
        return False
    if target in {"naxx-gothik-pass", "naxx-gothik-pass-control"}:
        from .offline_naxx import reached_naxx
        return reached_naxx(observation, control=target.endswith("-control"))
    if target in FULL_ROUTES:
        count, required = FULL_ROUTES[target]
        states = native.get("boss_states")
        reason = "cleared_reachable" if target == "ulduar-route" else "cleared"
        return (native.get("active") is False and native.get("cleanup") is True
                and native.get("map") == map_id and event.get("state") == "done"
                and event.get("reason") == reason and type(event.get("kills")) is int
                and event["kills"] >= len(required) and isinstance(states, list) and len(states) == count
                and all(type(s) is int and s in range(6) for s in states)
                and all(states[i] == 3 for i in required)
                and all(states[i] in (0, 3, 5) for i in range(count) if i not in required)
                and observation.get("scripted_wipe") is False)
    if native.get("stage") != "running":
        return False
    bodies = observation.get("bodies", [])
    if target in {"toc-slime-pools", "toc-slime-pools-control"}:
        slime = observation.get("slime", {})
        negative = target == "toc-slime-pools-control"
        counts = ("max_gap_ms", "inside_samples", "escapes", "outside_ms", "hits", "late_hits", "unknown_hits")
        if (any(type(slime.get(k)) is not int or slime[k] < 0 for k in counts)
                or slime.get("mechanics_enabled") is not (not negative)
                or not 0 < slime["max_gap_ms"] <= 200 or slime["unknown_hits"] != 0):
            return False
        actors = [b for b in bodies if b.get("slot") in range(1, 11)]
        if (len(actors) != 10 or len({b.get("guid") for b in actors}) != 10
                or any(type(b.get("guid")) is not int or b["guid"] <= 0 or b.get("alive") is not True
                       or b.get("owned") is not True or b.get("pinned") is not True
                       or b.get("map") != 649 or b.get("instance") != instance for b in actors)):
            return False
        pools = slime.get("pools")
        if not isinstance(pools, list) or not pools:
            return False
        grew = False
        for pool in pools:
            if (not isinstance(pool, dict)
                    or any(type(pool.get(k)) is not int or pool[k] < 0 for k in ("guid", "casts", "age_ms", "last_ms"))
                    or pool["guid"] <= 0 or pool["casts"] == 0
                    or any(type(pool.get(k)) not in (int, float) or not math.isfinite(pool[k]) or pool[k] <= 0
                           for k in ("minimum_radius", "maximum_radius"))):
                return False
            if pool["maximum_radius"] < pool["minimum_radius"]:
                return False
            if pool["casts"] >= 15 and pool["age_ms"] >= 20000 and pool["last_ms"] <= 1500:
                grew = grew or pool["maximum_radius"] - pool["minimum_radius"] >= 4
        if negative:
            return slime["hits"] > 0
        return (grew and slime["inside_samples"] > 0 and slime["escapes"] > 0
                and slime["outside_ms"] >= 3000 and slime["late_hits"] == 0)
    if target in {"ulduar-restart", "ulduar-tank-restart", "ulduar-forced-tank-restart"}:
        forced = target == "ulduar-forced-tank-restart"
        if (forced and observation.get("tank_swap_forced") is not True) or (not forced and observation.get("tank_swap_forced") is True):
            return False
        if target != "ulduar-restart" and not (
                observation.get("wipe_boss") == 2 and type(observation.get("main_tank_at_wipe")) is int
                and observation["main_tank_at_wipe"] > 0
                and observation["main_tank_at_wipe"] != native.get("leader")
                and observation.get("main_tank") == native.get("leader")):
            return False
        members = native.get("bodies", [])
        before = observation.get("restarts_at_wipe")
        expected = {b.get("guid") for b in bodies if b.get("slot") in range(1, 11)}
        return (observation.get("scripted_wipe") is True and observation.get("restarting_seen") is True
                and type(before) is int and before >= 0 and type(native.get("restarts")) is int and native["restarts"] == before + 1
                and type(native.get("stage_ms")) is int and native["stage_ms"] >= 5000
                and type(native.get("leader")) is int and native["leader"] > 0
                and native.get("driver") is True and native.get("elected") == native.get("leader")
                and len(expected) == 10 and None not in expected and len(members) == 10
                and {b.get("guid") for b in members} == expected
                and all(b.get("present") is True and b.get("alive") is True and b.get("map") == map_id
                        and b.get("instance") == instance for b in members))
    if target == "offline-reserves":
        reserve = next((b for b in bodies if b.get("slot") == 11), {})
        members = native.get("bodies", [])
        expected = {b.get("guid") for b in bodies if b.get("slot") in (*range(1, 10), 11)}
        return (type(native.get("stage_ms")) is int and native["stage_ms"] >= 5000 and len(members) == 10
                and len(expected) == 10 and None not in expected and {b.get("guid") for b in members} == expected
                and native.get("elected") == native.get("leader") and reserve.get("guid") is not None
                and any(b.get("guid") == reserve["guid"] for b in members)
                and reserve.get("alive") is True and reserve.get("owned") is True
                and reserve.get("grouped") is True and type(reserve.get("master")) is int and reserve["master"] > 0
                and all(b.get("present") is True and b.get("alive") is True and b.get("map") == map_id
                        and b.get("instance") == instance for b in members))
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
    # Startup clears type-1 assignments when MaxRandomBots=0. The native fixture
    # registers this one fixed account after startup, before ordinary pinning.


def prepare_fixture(layout, log=print):
    """Restore a proven synthetic checkpoint or prepare it through the native factory."""
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
    if type(prepared.get("guild")) is not int or prepared["guild"] <= 0 or type(prepared.get("owner")) is not int or prepared["owner"] <= 0:
        raise world.WorldError("preparation did not identify the synthetic guild")
    return prepared, checkpoint.digest(checkpoint.receipt)


def run(layout, *, target, toc_worm_support=False, log=print):
    if target not in TARGETS:
        raise LayoutError("unsupported offline target")
    if toc_worm_support and TARGETS[target][0] not in {"toc", "toc-full"}:
        raise LayoutError("worm support requires a ToC encounter target")
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
                  token=token, target=target, toc_worm_support=toc_worm_support,
                  out=str(out), reached=False, cleanup=False, stopped=False)
    if target in FULL_ROUTES:
        count, required = FULL_ROUTES[target]
        report["route"] = dict(required_bosses=required, unverified_bosses=[i for i in range(count) if i not in required],
                               scope="supported_offline_route" if target == "ulduar-route" else "all_encounters")
    feed = None
    try:
        prepared, report["fixture_checkpoint_sha256"] = prepare_fixture(layout, log)
        report["fixture"] = prepared
        # The native driver creates its character, but account creation queues
        # an async write and its immediate lookup can fail. Provision the fixed
        # account while stopped, after restoring any older fixture checkpoint.
        provision.ensure_accounts(layout, "DOJODRIVER", log=log)
        _set(layout, "modules/mod_leeroy.conf", {"Leeroy.Enable": "1", "Leeroy.GuildId": str(prepared["guild"]),
                                                "Leeroy.HumanGuid": str(prepared["owner"]),
                                                "Leeroy.Mechanics.Enable": "0" if target == "toc-slime-pools-control" else "1",
                                                "Leeroy.Hazards.Enable": "1"})
        if target == "icc-valithria":
            _set(layout, "modules/mod_dungeon_clear.conf", {"DungeonClear.OfflineValithria": "1"})
        report["run_source"] = raids.provenance(layout)
        feed = MechanicConnection(layout, guild=prepared["guild"], map_id=TARGETS[target][1])
        feed.__enter__()
        # The adapter's free-port preflight must finish before the world binds
        # those ports. Its real bridge client waits for the listener to open.
        world.start(layout, log=log)
        deadline = time.monotonic() + 30
        while not feed.ready and not feed.error and time.monotonic() < deadline:
            time.sleep(0.2)
        if not feed.ready:
            raise world.WorldError(feed.error or "candidate mechanic feed timed out")
        report["mechanic_feed"] = dict(features=feed.features, rules=feed.rules)
        with world.ra(layout) as client:
            scenario = TARGETS[target][0]
            if toc_worm_support:
                scenario = "toc-full-worms" if target == "toc-full" else "toc-worms"
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
            deadline = time.monotonic() + (11100 if target in FULL_ROUTES else 3000)
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
                    if observed.get("error") and not report.get("gate_failure"):
                        report["error"] = observed["error"]
                        report["gate_failure"] = "native_fixture_error"
                        report["failure_observation"] = observed
                        client.command("dojo offline stop")
                        # Keep observing ordinary teardown so even a failed
                        # fixture has a verified cleanup receipt.
                    native = observed.get("native", {})
                    if observed.get("token") != token or native.get("event", {}).get("id") != token:
                        raise world.WorldError("offline run token changed")
                    stage = native.get("stage")
                    if target.startswith("naxx-gothik-pass") and not report.get("gate_failure"):
                        gate = observed.get("naxx_pass", {})
                        if gate.get("measured_ms", 0) > 600000 or native.get("event", {}).get("wipes", 0) > 0:
                            report["gate_failure"] = "naxx_pass_deadline_or_wipe"
                            report["failure_observation"] = observed
                            client.command("dojo offline stop")
                    if target == "toc-slime-pools" and not report.get("gate_failure"):
                        slime = observed.get("slime", {})
                        if slime.get("late_hits", 0) > 0 or slime.get("unknown_hits", 0) > 0:
                            report["gate_failure"] = "slime_damage_after_reaction_window_or_missing_cast"
                            report["failure_observation"] = observed
                            client.command("dojo offline stop")
                    if stage != last_stage:
                        log(f"offline {target}: {stage or native.get('event', {}).get('state', 'unknown')}")
                        last_stage = stage
                    if target in {"ulduar-restart", "ulduar-tank-restart", "ulduar-forced-tank-restart"} and not observed.get("scripted_wipe") and stage == "running":
                        leader = next((b for b in observed.get("bodies", []) if b.get("slot") == 1), {})
                        states = leader.get("bosses", [])
                        boss = 3 if target == "ulduar-restart" else 2
                        swapped = (type(observed.get("main_tank")) is int and observed["main_tank"] > 0
                                   and observed["main_tank"] != native.get("leader"))
                        if len(states) > boss and states[boss] == 1 and (target != "ulduar-tank-restart" or swapped):
                            # Admission rechecks live ownership, the exact engaged
                            # boss and every actor before the native test stimulus.
                            command = "wipe-tank-swap" if target == "ulduar-tank-restart" else "wipe"
                            if target == "ulduar-forced-tank-restart":
                                command = "wipe-force-tank-swap"
                            reply = client.command(f"dojo offline {command} {token}")
                            if "dojo offline: wiped" in reply:
                                log(f"offline {target}: applied scripted full-wipe stimulus")
                    if not report.get("gate_failure") and reached(observed, target=target, token=token) and not report["reached"]:
                        feature = observed.get("toc_worm_support", {})
                        if toc_worm_support and (feature.get("requested") is not True
                                or type(feature.get("active_samples")) is not int or feature["active_samples"] < 10):
                            raise world.WorldError("requested worm support was not active during measurement")
                        report["reached"] = True
                        report["milestone"] = observed
                        if target not in FULL_ROUTES:
                            client.command("dojo offline stop")
                    if not native.get("active", True):
                        report["terminal"] = observed
                        report["cleanup"] = native.get("cleanup") is True
                        if target.startswith("naxx-gothik-pass"):
                            report["cleanup"] = report["cleanup"] and observed.get("naxx_pass", {}).get("strategies_restored") is True
                        if toc_worm_support:
                            report["cleanup"] = report["cleanup"] and observed.get("toc_worm_support", {}).get("restored") is True
                        if target == "offline-reserves":
                            reserve = next((b for b in observed.get("bodies", []) if b.get("slot") == 11), {})
                            report["reserve_returned"] = (reserve.get("map") == 0 and reserve.get("alive") is True
                                and reserve.get("grouped") is False and reserve.get("owned") is False and reserve.get("master") == 0)
                            report["cleanup"] = report["cleanup"] and report["reserve_returned"]
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
