"""Plan Task 8: scripted diagnostic episodes on the training realm, through the worker's endpoint, and the G1 report.

A realtime diagnostic controller (milestone M1): it speaks the protocol over the worker's loopback endpoint, records
every decision (recording.EpisodeRecorder) and keeps the authoritative state at each decision boundary in a separate
debug stream. The world does not pause between steps, so every transition reports the game time that actually passed.

Episodes, each from a fresh reset: the authored frost_duelist against itself (several times), random_legal against
frost_duelist on both sides, both actors idle until the match limit (a draw), both killed on one tick (simultaneous
death, the harness's own kill), the controller's connection dropped right after a committed step and an actor's
session ended mid-episode (both aborted, recorded as aborted and excluded), and the native stock playerbots baseline
(both actors handed to stock behaviour, D25), outside the controlled comparison.

Each finished episode is reconciled with the core: damage and death events against the arena scoreboard and the
dojo's own tallies, completed casts against the core's completed-cast hook, the outcome against the scoreboard's
deaths and killing blows, and the shaping rewards against the health at each boundary. Every recording is replayed
without the server (replay.replay_episode). The trace is drained for the action-source audit.

Everything goes to <data root>/runs/<stamp>-g1/: g1-report.json and G1.md, scenario-description.json, episodes/
(recordings), debug/ (boundary state per episode), replays/, trace.jsonl, control-audit.jsonl, outcomes.jsonl, the
RA transcript and the worker's logs. No credential is written.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

from .. import __version__, actions, observation, outcome, policies, replay
from ..client import ConnectionLost, DojoClient, DojoError
from ..memory import EpisodeMemory
from ..protocol import encode
from ..recording import EpisodeRecorder, decision_record, verify_episode
from . import acceptance, control, mysql, world
from .layout import Layout, norm

RECEIPT = "dojo-endpoint.json"
RESET_TIMEOUT = 200.0
STEP_TIMEOUT = 10.0
STOCK_START_YARDS = 30.0

# (name, policies for actor 0 and 1, what happens mid-episode)
PLAN = [
    ("mirror-1", ("frost_duelist", "frost_duelist"), None),
    ("mirror-2", ("frost_duelist", "frost_duelist"), None),
    ("mirror-3", ("frost_duelist", "frost_duelist"), None),
    ("random-vs-script", ("random_legal", "frost_duelist"), None),
    ("script-vs-random", ("frost_duelist", "random_legal"), None),
    ("idle-draw", ("noop", "noop"), None),
    ("both-die", ("noop", "noop"), ("kill_all", 10)),
    ("dropped-connection", ("frost_duelist", "frost_duelist"), ("drop", 25)),
    ("actor-offline", ("frost_duelist", "frost_duelist"), ("actor_offline", 25)),
    ("stock-1", None, "stock"),
    ("stock-2", None, "stock"),
]
QUICK = [p for p in PLAN if p[0] in ("mirror-1", "both-die", "dropped-connection", "stock-1")]
STOCK_PRIVILEGES = [
    "information: playerbots reads the server's own unit state directly; no perceptual delay, no visibility or "
    "last-seen model, no masks",
    "reaction: playerbots' own update and reaction timing (Playerbots.ReactDelay) instead of the 200 ms decision, "
    "150 ms command delay and 100 ms perceptual delay",
    "actions: the whole spellbook, talents and pets (the controlled policies have only the v1 kit: Frostbolt, Ice "
    "Lance, Frost Nova, movement, facing, target and auto-attack)",
    "execution: playerbots' CastSpell (it turns to face its target and toggles pet autocast) and CanCastSpell, not "
    "the client-like executor; no bot cheats (Playerbots.BotCheats is empty in the training rules)",
    "start: both actors 30 yd apart on the line between the scenario's starts (harness placement), since stock "
    "engages nothing beyond Playerbots.SightDistance (100 yd) and the starts are 121 yd apart",
]


class G1Error(Exception):
    pass


def _lines(prefix: str, text: str) -> list[dict]:
    return [json.loads(line[len(prefix) + 1:]) for line in text.splitlines() if line.startswith(prefix + " ")]


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git(root: str) -> dict:
    def run(*args: str) -> str:
        return subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", root, *args], capture_output=True,
                              text=True, timeout=60).stdout.strip()
    return {"root": root, "head": run("rev-parse", "HEAD"), "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}


class Run:
    def __init__(self, layout: Layout, manifest: dict, out: Path, log=print):
        self.layout = layout
        self.manifest = manifest
        self.out = out
        self.log = log
        self.transcript: list = []
        self.console: control.Console | None = None
        self.client: DojoClient | None = None
        self.hello: dict = {}
        self.catalog: actions.Catalog | None = None
        self.schema: observation.Schema | None = None
        self.kit_doc: list = []
        self.seq = 0
        self.lost = 0
        self.trace: list[dict] = []
        self.files = {name: open(out / f"{name}.jsonl", "w", encoding="utf-8") for name in ("trace",)}
        (out / "episodes").mkdir()
        (out / "debug").mkdir()
        (out / "replays").mkdir()
        self.results: list[dict] = []
        self.versions: dict = {}

    def write(self, name: str, record: dict) -> None:
        self.files[name].write(json.dumps(record, sort_keys=True) + "\n")
        self.files[name].flush()

    def close(self) -> None:
        for f in self.files.values():
            f.close()
        if self.console:
            self.console.close()
            self.console = None
        if self.client:
            self.client.disconnect()

    def ra(self, text: str) -> str:
        return self.console(text)

    # --- the worker's console ---

    def drain(self) -> list[dict]:
        got: list[dict] = []
        while True:
            out = self.ra(f"dojo control trace since {self.seq} 2000")
            records = _lines("dojo-trace", out)
            last = out.splitlines()[-1] if out.splitlines() else ""
            if "LOST" in last:
                self.lost += int(last.split("LOST ")[1].split()[0])
            for r in records:
                self.write("trace", r)
                self.seq = max(self.seq, r["seq"])
            got += records
            if len(records) < 2000:
                break
        self.trace += got
        return got

    def episode_status(self) -> dict:
        return _lines("dojo-episode", self.ra("dojo episode status"))[0]

    def endpoint_status(self) -> dict:
        return _lines("dojo-endpoint", self.ra("dojo endpoint status"))[0]

    def score(self) -> dict:
        return _lines("dojo-score", self.ra("dojo episode score"))[0]

    def boundaries(self) -> list[dict]:
        out: list[dict] = []
        start = 0
        while True:
            got = _lines("dojo-boundary", self.ra(f"dojo endpoint boundaries {start} 50"))
            out += got
            if len(got) < 50:
                return out
            start = got[-1]["decision"] + 1

    def wait_episode(self, predicate, timeout: float, what: str, poll: float = 1.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            s = self.episode_status()
            if predicate(s):
                return s
            if time.monotonic() > deadline:
                raise G1Error(f"timed out after {timeout:.0f} s waiting for {what}: {s['phase']} {s['result']}")
            time.sleep(poll)

    # --- the endpoint ---

    def receipt(self) -> dict:
        path = self.layout.run_dir / RECEIPT
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("pid") == world.pid(self.layout):
                    return data
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise G1Error(f"no endpoint receipt from worldserver pid {world.pid(self.layout)} at {norm(path)}")

    def connect(self) -> dict:
        receipt = self.receipt()
        if receipt.get("host") != "127.0.0.1":
            raise G1Error(f"the endpoint is on {receipt.get('host')}, not loopback")
        self.layout.check_port(receipt["port"], "dojo endpoint")
        if self.client:
            self.client.disconnect()
        self.client = DojoClient(timeout=STEP_TIMEOUT, retries=2, id_prefix="g")
        self.client.connect("127.0.0.1", int(receipt["port"]))
        expect = {"observation_schema": f"{observation.SCHEMA}:{self.schema.sha256}",
                  "action_schema": f"{actions.SCHEMA}:{self.catalog.sha256}", "scenario": self.manifest["id"]}
        self.hello = self.client.hello(expect)
        return receipt

    def header(self, episode: str, reset: dict, players: list[policies.Policy], plan: str) -> dict:
        h = self.hello
        return {
            "plan": plan, "session": h["session"], "protocol": h["protocol"], "worker": h["worker"],
            "scenario": reset["scenario"], "scenario_seed": reset["scenario_seed"], "generation": reset["generation"],
            "origin_sim_ms": reset["sim_ms"], "schemas": h["schemas"], "actors": h["actors"], "actions": h["actions"],
            "clock": h["clock"], "rng": h["rng"], "bounds": h["bounds"], "catalog_kit": self.kit_doc,
            "scenario_manifest": self.manifest, "scenario_sha256": h["worker"].get("scenario_sha256"),
            "versions": self.versions, "client": {"package": "leeroy_dojo", "version": __version__},
            "policies": [{"actor": slot, **p.describe()} for slot, p in enumerate(players)],
            "seeds": {"scenario_seed": reset["scenario_seed"], "combat": None,
                      "policies": [p.seed for p in players]},
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    # --- one protocol episode ---

    def play(self, index: int, name: str, policy_names: tuple[str, str], event) -> dict:
        episode = f"g1-{self.out.name[:15]}-{index:02d}-{name}"
        players = [policies.POLICIES[n](self.schema, self.catalog, seed=100 + 10 * index + slot)
                   for slot, n in enumerate(policy_names)]
        for p in players:
            p.reset(episode)
        memory = EpisodeMemory(2, players)
        status_before = self.episode_status()
        began = time.time()
        result: dict = {"index": index, "name": name, "episode": episode, "policies": list(policy_names),
                        "event": event, "seq_before": self.seq}
        try:
            reset = self.client.request(self.client.reset_message(episode, index), timeout=RESET_TIMEOUT, retries=1)
        except DojoError as e:
            result["reset_failed"] = f"{e.code} ({e.reason}): {e.response.get('message', '')}"
            result["episode_status"] = self.episode_status()
            result["trace"] = self.drain()
            result["wall_seconds"] = round(time.time() - began, 1)
            return result
        memory.begin(reset)
        result["reset"] = {"generation": reset["generation"], "wall_seconds": round(time.time() - began, 1),
                           "previous_episode_generation": status_before["generation"]}
        recorder = EpisodeRecorder(self.out / "episodes", episode, self.header(episode, reset, players, name))
        observations = reset["observations"]
        aborted = None
        kind, at = event if event else (None, None)
        try:
            while True:
                decision = self.client.decision
                if kind == "kill_all" and decision == at:
                    result["harness"] = self.ra("dojo harness kill all")
                if kind == "actor_offline" and decision == at:
                    result["harness"] = self.ra("dojo harness disconnect 1")
                    time.sleep(0.3)
                chosen = [players[o["actor"]].act(o["obs"], o["mask"]) for o in observations]
                if kind == "drop" and decision == at:
                    # Committed, never read: the worker must abort rather than guess whether it was applied.
                    message = self.client.step_message(chosen)
                    self.client.send_line(encode(message))
                    self.client.disconnect()
                    aborted = "the controller's connection dropped right after a committed step"
                    break
                step = self.client.step(chosen)
                memory.record(step)
                recorder.add(decision_record(observations, step, reset["sim_ms"]))
                observations = step["observations"]
                if step["terminated"] or step["truncated"]:
                    o = step["outcome"]
                    recorder.finish("truncated" if step["truncated"] else "terminated", reason=o["reason"],
                                    winner=o["winner"], decisions=recorder.transitions)
                    result["outcome"] = o
                    break
        except DojoError as e:
            aborted = f"{e.code} ({e.reason}): {e.response.get('message', '')}"
        except (ConnectionLost, TimeoutError) as e:
            aborted = f"connection: {e}"
        if aborted is not None:
            memory.abort(aborted)
            recorder.finish("aborted", reason=aborted, decisions=recorder.transitions)
            result["aborted"] = aborted
        result["decisions"] = recorder.transitions
        result["wall_seconds"] = round(time.time() - began, 1)
        if kind == "drop":
            result.update(self.after_drop())
        result["episode_status"] = self.wait_episode(lambda s: not s["phase"].startswith("resetting")
                                                     and s["phase"] != "running", 30, "the episode to end")
        result["score"] = self.score()
        bounds = self.boundaries()
        with open(self.out / "debug" / f"{episode}.jsonl", "w", encoding="utf-8") as f:
            for b in bounds:
                f.write(json.dumps(b, sort_keys=True) + "\n")
        result["boundaries"] = len(bounds)
        result["trace"] = self.drain()
        result["endpoint"] = self.endpoint_status()
        return result

    def after_drop(self) -> dict:
        """After the dropped connection: the worker aborted the episode, and a new session refuses the old one."""
        old_session = self.hello["session"]
        time.sleep(1.0)
        self.connect()
        stale = {"v": 1, "type": "status", "id": self.client.next_id("q")}
        status = self.client.request(stale)
        try:
            self.client.request({"v": 1, "type": "step", "id": self.client.next_id("s"), "session": old_session,
                                 "episode": "gone", "decision": 0, "actions": [{"actor": 0, "action": 0},
                                                                             {"actor": 1, "action": 0}]})
            refused = ""
        except DojoError as e:
            refused = e.code or ""
        return {"reconnected": {"new_session": self.hello["session"], "old_session": old_session,
                                "state": status["state"], "old_session_refused": refused}}

    # --- the stock baseline ---

    def stock_start(self) -> list[dict]:
        """Stock playerbots engages nothing beyond its sight distance (Playerbots.SightDistance, 100 yd), and the
        scenario's starts are 121 yd apart, so the baseline starts both actors STOCK_START_YARDS apart on the line
        between the starts, through the arena's centre, facing each other. Each placement is traced as harness."""
        a, b = (self.manifest["actors"][i]["start"] for i in (0, 1))
        dx, dy = b["x"] - a["x"], b["y"] - a["y"]
        length = math.hypot(dx, dy)
        ux, uy = dx / length, dy / length
        cx, cy = (a["x"] + b["x"]) / 2, (a["y"] + b["y"]) / 2
        facing = math.atan2(dy, dx) % (2 * math.pi)
        placed = []
        for slot, sign in ((0, -1), (1, 1)):
            x, y = cx + sign * STOCK_START_YARDS / 2 * ux, cy + sign * STOCK_START_YARDS / 2 * uy
            probe = (x, y, a["z"] + 2.0)
            ground = self.console.los(probe, probe)["ground"][0]
            o = facing if slot == 0 else (facing + math.pi) % (2 * math.pi)
            self.ra(f"dojo harness place {slot} {x:.3f} {y:.3f} {ground + 0.1:.3f} {o:.4f}")
            placed.append({"actor": slot, "x": round(x, 3), "y": round(y, 3), "z": round(ground + 0.1, 3), "o": round(o, 4)})
        time.sleep(1.5)
        return placed

    def stock(self, index: int, name: str) -> dict:
        began = time.time()
        result: dict = {"index": index, "name": name, "stock": True, "seq_before": self.seq}
        started = _lines("dojo-episode-reset", self.ra(f"dojo episode reset g1-{name}"))
        if not started:
            raise G1Error(f"the stock baseline's reset did not start: {self.episode_status()['result']}")
        generation = started[0]["generation"]
        ready = self.wait_episode(lambda s: s["generation"] != generation or not s["phase"].startswith("resetting"),
                                  RESET_TIMEOUT, "the stock baseline's reset")
        result["reset"] = {"generation": generation, "phase": ready["phase"], "wall_seconds": round(time.time() - began, 1)}
        if ready["phase"] != "running":
            raise G1Error(f"the stock baseline's reset did not become ready: {ready['result']}")
        result["placed"] = self.stock_start()
        result["handed_over"] = self.ra("dojo episode stock")
        fight = time.time()
        end = self.wait_episode(lambda s: s["phase"] != "running", float(self.manifest["rules"]["match_seconds"]) + 60,
                                "the stock baseline to end")
        result["fight_seconds"] = round(time.time() - fight, 1)
        result["episode_status"] = end
        result["outcome"] = end.get("outcome")
        result["score"] = self.score()
        result["trace"] = self.drain()
        result["wall_seconds"] = round(time.time() - began, 1)
        return result


def reconcile(result: dict, catalog: actions.Catalog, records: list[dict] | None, bounds: list[dict] | None) -> dict:
    """The episode's events against the core's scoreboard and the dojo's own tallies, its outcome against the
    scoreboard, and its shaping rewards against the health at each boundary."""
    problems: list[str] = []
    score = result["score"]
    core = score.get("core") or {}
    dojo = score.get("dojo") or []
    spell_of = {actions.ability_name(k["name"]): int(k["spell_id"]) for k in catalog.kit}
    rows = []
    counts = replay.count_events(records, catalog) if records is not None else None
    for slot in (0, 1):
        c = (core.get("actors") or [{}, {}])[slot]
        d = dojo[slot] if len(dojo) > slot else {}
        row = {"actor": slot, "core": c, "dojo": d}
        if c.get("damage_done") != d.get("damage_done"):
            problems.append(f"actor {slot}: core damage {c.get('damage_done')}, dojo tally {d.get('damage_done')}")
        if c.get("deaths") != d.get("deaths"):
            problems.append(f"actor {slot}: core deaths {c.get('deaths')}, dojo tally {d.get('deaths')}")
        if counts is not None:
            e = counts[slot]
            row["events"] = e
            if e["damage_done"] != d.get("damage_done"):
                problems.append(f"actor {slot}: events damage {e['damage_done']}, dojo tally {d.get('damage_done')}")
            if e["deaths"] != d.get("deaths"):
                problems.append(f"actor {slot}: {e['deaths']} death events, dojo tally {d.get('deaths')}")
            casts = {spell_of[k]: n for k, n in e["casts"].items() if k in spell_of}
            unknown = {k: n for k, n in e["casts"].items() if k not in spell_of}
            tally = {int(k): n for k, n in (d.get("casts") or {}).items()}
            if unknown:
                problems.append(f"actor {slot}: completed casts of no kit ability {unknown}")
            if casts != tally:
                problems.append(f"actor {slot}: completed casts by events {casts}, by the core's hook {tally}")
            row["casts_by_spell"] = casts
        rows.append(row)
    o = result.get("outcome")
    if o and o.get("reason") == "death":
        loser = 1 - o["winner"]
        deaths = [(core.get("actors") or [{}, {}])[s].get("deaths") for s in (0, 1)]
        if deaths[loser] != 1 or deaths[o["winner"]] != 0:
            problems.append(f"a death won by team {o['winner']}, but the core counts deaths {deaths}")
    elif o and o.get("reason") == "simultaneous_death":
        deaths = [(core.get("actors") or [{}, {}])[s].get("deaths") for s in (0, 1)]
        if deaths != [1, 1]:
            problems.append(f"simultaneous death, but the core counts deaths {deaths}")
    elif o and o.get("reason") == "time_limit":
        deaths = [(core.get("actors") or [{}, {}])[s].get("deaths") for s in (0, 1)]
        if any(deaths):
            problems.append(f"a time-limit draw, but the core counts deaths {deaths}")
    shaping = None
    if records is not None and bounds:
        by_decision = {b["decision"]: b for b in bounds}
        worst = 0.0
        checked = 0
        for r in records:
            before, after = by_decision.get(r["decision"]), by_decision.get(r["decision"] + 1)
            if before is None or after is None:
                problems.append(f"no boundary state around decision {r['decision']}")
                break
            stand = lambda b: [outcome.Standing(s["team"], s["alive"], s["health"]) for s in b["standings"]]  # noqa: E731
            got = outcome.decide(stand(after), False)
            ended = outcome.Outcome(r["outcome"]["reason"], r["outcome"]["winner"]) if r["outcome"] else outcome.Outcome()
            want = outcome.rewards(stand(after), outcome.potential(stand(before)), ended)
            for a, w in zip(r["actors"], want):
                worst = max(worst, abs(a["reward"]["shaping"] - w["shaping"]), abs(a["reward"]["terminal"] - w["terminal"]))
            if not ended.over and got.over:
                problems.append(f"decision {r['decision']}: the boundary shows {got.reason}, the step did not end")
            checked += 1
        shaping = {"decisions": checked, "max_difference": worst}
        if worst > 1e-6:
            problems.append(f"rewards differ from the boundary health by up to {worst}")
    return {"actors": rows, "shaping": shaping, "problems": problems, "ok": not problems}


def audit(result: dict, records: list[dict] | None, catalog: actions.Catalog) -> tuple[dict, list[dict]]:
    """Who produced every action: the trace of the episode's window and, per request, its final outcome."""
    trace = result.get("trace", [])
    gen = None
    producers = Counter((r["producer"], r["kind"]) for r in trace)
    stock = [r for r in trace if r["producer"] == "stock"]
    released = [r for r in trace if r["producer"] == "stock (released)"]
    problems = []
    if not result.get("stock") and (stock or released):
        problems.append(f"{len(stock) + len(released)} stock records in a controlled episode")
    if result.get("stock") and stock:
        problems.append(f"{len(stock)} stock records while owned in the stock baseline")
    for r in trace:
        if r["producer"] == "executor" and r["kind"] == "command" and not result.get("stock"):
            m = re.match(r"step:(\d+):(\d+)$", r.get("origin", ""))
            if not m:
                problems.append(f"executor command {r['action_id']} from origin {r.get('origin')!r}")
            elif gen is None:
                gen = m.group(1)
    lines = []
    if records is not None:
        final: dict[int, dict] = {}
        for rec in records:
            for e in rec["events"]:
                aid = e.get("action_id")
                if aid is None or e.get("kind") != "command":
                    continue
                if e.get("outcome") in ("executed", "rejected", "superseded", "dropped", "refused"):
                    final.setdefault(aid, e)
        for rec in records:
            for a in rec["actors"]:
                if a["action"] == 0:
                    continue
                e = final.get(a["action_id"])
                line = {"episode": result["episode"], "decision": rec["decision"], "actor": a["actor"],
                        "action_id": a["action_id"], "action": catalog.entries[a["action"]].name,
                        "producer": "protocol", "outcome": e["outcome"] if e else "pending at the end",
                        "by": e.get("producer") if e else None, "input_t_ms": e.get("input_t_ms") if e else None,
                        "due_t_ms": e.get("due_t_ms") if e else None, "exec_t_ms": e.get("exec_t_ms") if e else None,
                        "detail": e.get("detail") if e else None}
                lines.append(line)
    outcomes = Counter(line["outcome"] for line in lines)
    return {"producers": {f"{p}/{k}": n for (p, k), n in sorted(producers.items())}, "stock": len(stock),
            "stock_released": len(released), "requests": len(lines), "outcomes": dict(outcomes),
            "problems": problems, "ok": not problems}, lines


def run(layout: Layout, manifest: dict, *, quick: bool = False, reset_run: Path | None = None,
        control_run: Path | None = None, log=print) -> dict:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = layout.data / "runs" / f"{stamp}-g1"
    out.mkdir(parents=True)
    report: dict = {"kind": "leeroy-dojo-g1", "format": 1, "gate": "G1", "started": stamp, "instance": layout.instance,
                    "scenario": manifest["id"], "plan": [p[0] for p in (QUICK if quick else PLAN)]}
    root = mysql.Client(layout, layout.server.root_cnf)
    report["mysql"] = mysql.verify_identity(layout, root)
    report["live_before"] = acceptance.live_fingerprint(layout)
    names = [a["name"] for a in manifest["actors"]]
    guids = acceptance.fixture_guids(root, layout, names)
    if len(guids) != len(names):
        raise world.WorldError("the fixtures do not exist; run dojo fixture create and build first")
    r = Run(layout, manifest, out, log)
    r.versions = {name: _git(layout.roots[name]) for name in ("source", "core", "playerbots")}
    build_receipt = Path(layout.roots["build"]) / "dojo-build.json"
    r.versions["build"] = {"receipt": norm(build_receipt), "sha256": _sha256_file(build_receipt),
                           "worldserver_sha256": _sha256_file(layout.bin / "worldserver.exe")}
    report["versions"] = r.versions
    baseline = None
    try:
        if world.running(layout):
            world.stop(layout, log=log)
        world.start(layout, log=log)
        r.console = control.Console(layout, r.transcript, log)
        r.ra("dojo fixture restore")
        report["restore_before"] = r.console.wait_job()
        baseline = acceptance.snapshot(root, layout, guids)
        r.seq = r.console.seq()
        cat_doc = json.loads(r.ra("dojo control actions").splitlines()[0][len("dojo-catalog "):])
        r.kit_doc = cat_doc["kit"]
        r.catalog = actions.build_catalog(r.kit_doc)
        r.schema = observation.build_schema(r.catalog)
        report["receipt"] = r.connect()
        report["hello"] = {k: v for k, v in r.hello.items() if k != "actions"}
        report["hello"]["action_count"] = len(r.hello["actions"])
        for index, (name, policy_names, event) in enumerate(QUICK if quick else PLAN, start=1):
            log(f"episode {index}: {name}")
            if event == "stock":
                result = r.stock(index, name)
            else:
                result = r.play(index, name, policy_names, event)
            r.results.append(result)
            log(f"  {result.get('outcome') or result.get('aborted')} after {result.get('decisions', '-')} decisions, "
                f"{result['wall_seconds']} s")
        r.ra("dojo arena close")
        report["close_result"] = r.console.wait_job()
    finally:
        try:
            if r.client and r.client.sock:
                r.client.close("g1 done")
        except Exception:   # noqa: BLE001 - the worker is stopped next either way
            pass
        r.close()
        world.stop(layout, log=log)
    logs = out / "worker-logs"
    logs.mkdir()
    for name in ("Server.log", "Playerbots.log"):
        if (layout.logs / name).exists():
            shutil.copy2(layout.logs / name, logs / name)
    after = acceptance.snapshot(root, layout, guids)
    report["side_effects"] = acceptance.compare(baseline, after) if baseline else {"ok": False, "progression": ["no baseline"]}
    report["live_after"] = acceptance.live_fingerprint(layout)
    lb, la = report["live_before"], report["live_after"]
    report["live_untouched"] = (lb["configs"] == la["configs"] and lb["data"] == la["data"]
                                and {k: v for k, v in lb["processes"].items() if "dojo-runtime" not in v["path"]}
                                == {k: v for k, v in la["processes"].items() if "dojo-runtime" not in v["path"]}
                                and lb["mysql84_service"] == la["mysql84_service"])
    (out / "ra-transcript.jsonl").write_text("".join(json.dumps(t) + "\n" for t in r.transcript), encoding="utf-8")
    return finish(out, report, r, layout, manifest, reset_run, control_run)


def _latest(layout: Layout, suffix: str) -> Path | None:
    runs = sorted((layout.data / "runs").glob(f"*-{suffix}"))
    return runs[-1] if runs else None


def finish(out: Path, report: dict, r: Run, layout: Layout, manifest: dict, reset_run: Path | None,
           control_run: Path | None) -> dict:
    episodes = []
    audit_ok = True
    for result in r.results:
        entry = {k: v for k, v in result.items() if k not in ("trace",)}
        records = None
        bounds = None
        if not result.get("stock") and not result.get("reset_failed"):
            path = out / "episodes" / result["episode"]
            rep = replay.replay_episode(path)
            (out / "replays" / f"{result['episode']}.json").write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")
            entry["replay"] = {k: rep.get(k) for k in ("ok", "usable", "outcome", "transitions", "reproduced", "problems",
                                                       "returns", "duration_ms", "excluded")}
            if rep.get("usable"):
                records = replay.read_records(path, verify_episode(path).valid_chunks)
                with open(out / "debug" / f"{result['episode']}.jsonl", encoding="utf-8") as f:
                    bounds = [json.loads(line) for line in f if line.strip()]
        if records is not None or result.get("stock"):
            entry["reconciliation"] = reconcile(result, r.catalog, records, bounds)
        summary, lines = audit(result, records, r.catalog)
        for line in lines:
            _append(out, "control-audit", line)
        entry["audit"] = summary
        audit_ok = audit_ok and summary["ok"]
        _append(out, "outcomes", {"episode": result.get("episode") or result["name"], "name": result["name"],
                               "outcome": result.get("outcome"), "aborted": result.get("aborted"),
                               "decisions": result.get("decisions"), "stock": bool(result.get("stock")),
                               "score": result.get("score")})
        episodes.append(entry)
    report["episodes"] = episodes
    report["trace_records"] = len(r.trace)
    report["trace_lost"] = r.lost

    controlled = [e for e in episodes if not e.get("stock")]
    usable = [e for e in controlled if e.get("replay", {}).get("usable")]
    aborted = [e for e in controlled if e.get("aborted")]
    reasons = Counter((e.get("outcome") or {}).get("reason") for e in usable)
    checks = []

    def check(name: str, ok: bool, **detail) -> None:
        checks.append({"check": name, "ok": bool(ok), **detail})

    check("every reset in this run completed ready",
          all(e.get("reset", {}).get("generation") and not e.get("reset_failed") for e in episodes), resets=len(episodes),
          failed=[{"episode": e["name"], "why": e.get("reset_failed")} for e in episodes if e.get("reset_failed")])
    check("every controlled episode that ended was recorded complete and replays without the server",
          all(e["replay"]["ok"] for e in usable) and len(usable) == len(controlled) - len(aborted),
          usable=len(usable), aborted=len(aborted))
    check("deterministic policies reproduce every recorded request from the recorded observations and masks",
          all(e["replay"]["reproduced"] and e["replay"]["reproduced"]["decisions"] == e["replay"]["reproduced"]["of"]
              for e in usable))
    check("terminal outcomes seen: death, simultaneous death and a time-limit draw",
          reasons.get("death", 0) >= 1 and reasons.get("simultaneous_death", 0) >= 1 and reasons.get("time_limit", 0) >= 1,
          reasons=dict(reasons))
    check("aborted episodes are recorded as aborted and excluded from the learning stream",
          len(aborted) >= (2 if len(controlled) > 4 else 1) and all(e["replay"]["ok"] and not e["replay"]["usable"]
                                                                    for e in aborted),
          aborted=[e["name"] for e in aborted])
    dropped = next((e for e in episodes if e["name"] == "dropped-connection"), None)
    if dropped:
        rc = dropped.get("reconnected", {})
        check("a dropped connection aborts the episode and a new session refuses the old one",
              dropped["episode_status"]["phase"] == "aborted" and rc.get("old_session_refused") == "bad_session"
              and rc.get("state") in ("ready", "closing"), reconnected=rc)
    check("events, outcomes and rewards reconcile with the core for every finished episode",
          all(e["reconciliation"]["ok"] for e in episodes if "reconciliation" in e),
          failed=[{"episode": e["name"], "problems": e["reconciliation"]["problems"][:5]} for e in episodes
                  if "reconciliation" in e and not e["reconciliation"]["ok"]])
    check("action-source audit: no stock action in a controlled episode, every executor command from a protocol step",
          audit_ok)
    check("no trace record was lost", r.lost == 0)
    stock = [e for e in episodes if e.get("stock")]
    check("the stock playerbots baseline ran to an outcome the dojo decided",
          bool(stock) and all((e.get("outcome") or {}).get("reason") for e in stock),
          outcomes=[e.get("outcome") for e in stock])
    check("no progression side effects", report["side_effects"]["ok"], progression=report["side_effects"].get("progression"))
    check("live install untouched", report["live_untouched"])

    reset_run = reset_run or _latest(layout, "reset-acceptance")
    control_run = control_run or _latest(layout, "control-acceptance")
    report["reset_reliability"] = _external(reset_run, "reset-acceptance")
    report["control"] = _external(control_run, "control-acceptance")
    check("100 consecutive clean resets with interrupted episodes (reset acceptance)",
          report["reset_reliability"].get("ok") and (report["reset_reliability"].get("summary") or {})
          .get("consecutive_ok", 0) >= 100, run=report["reset_reliability"].get("path"))
    check("exclusive control and ordinary execution (control acceptance)", report["control"].get("ok"),
          run=report["control"].get("path"))
    hidden = ((report["reset_reliability"].get("summary") or {}).get("by_case") or {}).get("hidden") or {}
    report["observation_mask"] = {
        "decisions_replayed": sum(e["replay"]["transitions"] for e in usable),
        "observations_checked": sum(e["replay"]["reproduced"]["of"] for e in usable if e["replay"]["reproduced"]),
        "replay_problems": sum(len(e["replay"]["problems"]) for e in usable),
        "hidden_change_resets": hidden}
    check("observations and masks: every recorded observation inside its schema, every mask with the noop and the "
          "requested action open, hidden changes reaching neither before they are observable",
          usable and report["observation_mask"]["replay_problems"] == 0 and hidden.get("resets", 0) >= 1
          and hidden.get("ok") == hidden.get("resets"), detail=report["observation_mask"])
    report["checks"] = checks
    report["ok"] = all(c["ok"] for c in checks)
    report["stock_privileges"] = STOCK_PRIVILEGES
    report["inputs"] = inputs(r, layout, manifest)
    (out / "scenario-description.json").write_text(json.dumps(scenario_description(r, layout, manifest), indent=2) + "\n",
                                                   encoding="utf-8")
    (out / "g1-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "G1.md").write_text(markdown(report), encoding="utf-8")
    report["out"] = norm(out)
    return report


def _append(out: Path, name: str, line: dict) -> None:
    with open(out / f"{name}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, sort_keys=True) + "\n")


def _external(path: Path | None, kind: str) -> dict:
    if path is None:
        return {"ok": False, "path": None, "why": f"no {kind} run"}
    try:
        rep = json.loads((Path(path) / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"ok": False, "path": norm(path), "why": str(e)}
    keep = {"ok": rep.get("ok"), "path": norm(path), "started": rep.get("started"), "summary": rep.get("summary"),
            "checks": [{"check": c.get("check") or c.get("case"), "ok": c.get("ok")} for c in
                       rep.get("checks", rep.get("cases", []))]}
    if "cases" in rep and "checks" not in rep:
        keep["checks"] = [{"check": c.get("case"), "ok": c.get("ok")} for c in rep["cases"]]
    return keep


def inputs(r: Run, layout: Layout, manifest: dict) -> dict:
    rules = Path(layout.roots["source"]) / "scripts" / "dojo" / "training-rules.json"
    return {"scenario": {"id": manifest["id"], "sha256": (r.hello.get("worker") or {}).get("scenario_sha256")},
            "observation_schema": r.hello.get("schemas", {}).get("observation"),
            "action_schema": r.hello.get("schemas", {}).get("action"),
            "training_rules_sha256": _sha256_file(rules), "versions": r.versions}


def scenario_description(r: Run, layout: Layout, manifest: dict) -> dict:
    """One complete, reproducible description of the scenario the episodes ran."""
    rules = Path(layout.roots["source"]) / "scripts" / "dojo" / "training-rules.json"
    return {
        "kind": "leeroy-dojo-scenario-description", "gate": "G1", "scenario": manifest,
        "scenario_sha256": (r.hello.get("worker") or {}).get("scenario_sha256"),
        "catalog": r.catalog.document if r.catalog else None, "catalog_sha256": r.catalog.sha256 if r.catalog else None,
        "observation_schema_sha256": r.schema.sha256 if r.schema else None,
        "observation_size": r.schema.size if r.schema else None,
        "training_rules": json.loads(rules.read_text(encoding="utf-8")), "training_rules_sha256": _sha256_file(rules),
        "worker": r.hello.get("worker"), "clock": r.hello.get("clock"), "rng": r.hello.get("rng"),
        "bounds": r.hello.get("bounds"), "actors": r.hello.get("actors"), "versions": r.versions,
        "policies": {name: {"kind": cls.kind, "version": cls.version} for name, cls in policies.POLICIES.items()},
        "plan": [{"name": n, "policies": list(p) if p else None, "event": e} for n, p, e in PLAN],
        "reproduce": [
            "worktrees of the recorded source, core and playerbots commits; scripts/dojo/configure.ps1 -Variant training "
            "and build.ps1 -Jobs 4 into a fresh build root",
            "python -m leeroy_dojo realm --layout <layout> provision --scenario dojo/scenarios/frost_mirror_small_v1.json",
            "python -m leeroy_dojo realm --layout <layout> db-start, then g1 --scenario <the same manifest>",
            "python -m leeroy_dojo replay <run>/episodes/<episode> replays a recording without the server",
        ],
    }


def markdown(report: dict) -> str:
    lines = [f"# Dojo G1 report, {report['started']}", "",
             f"Scenario `{report['scenario']}`, instance `{report['instance']}`. Overall: "
             f"**{'passed' if report['ok'] else 'NOT passed'}**. No learned policy is claimed: the policies are "
             "random_legal, the authored frost_duelist and noop, plus the native stock playerbots system baseline.", "",
             "## Checks", ""]
    for c in report["checks"]:
        lines.append(f"- {'ok' if c['ok'] else 'FAIL'}: {c['check']}")
    lines += ["", "## Episodes", "", "| # | episode | policies | outcome | decisions | reconciled |", "|---|---|---|---|---|---|"]
    for e in report["episodes"]:
        o = e.get("outcome") or {}
        what = (f"{o.get('reason')}" + (f", team {o['winner']} wins" if o.get("winner") is not None else "")) if o else \
            f"aborted: {e.get('aborted', '')[:60]}"
        rec = e.get("reconciliation")
        lines.append(f"| {e['index']} | {e['name']} | {'stock playerbots' if e.get('stock') else ' vs '.join(e['policies'])} "
                     f"| {what} | {e.get('decisions', '-')} | {'-' if rec is None else ('ok' if rec['ok'] else 'FAIL')} |")
    rr = report.get("reset_reliability", {})
    lines += ["", "## Reset reliability", "", f"Run `{rr.get('path')}`: {json.dumps(rr.get('summary'))}", "",
              "## Observations and masks", "", f"`{json.dumps(report.get('observation_mask'))}`", "",
              f"Control acceptance: `{(report.get('control') or {}).get('path')}`", "",
              "## Stock baseline privileges (not a controlled comparison)", ""]
    lines += [f"- {p}" for p in report["stock_privileges"]]
    lines += ["", "## Inputs", "", "```json", json.dumps(report["inputs"], indent=2), "```", ""]
    return "\n".join(lines)
