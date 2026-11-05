"""Task 7 acceptance: consecutive episode resets on the training realm, against a restart-based reference.

Each episode is put into a condition before it is reset: idle, mid-combat, an actor dead, a cast in progress, an
actor rooted, a command still inside its command delay, a projectile in flight, an actor's session disconnected, the
console connection dropped mid-reset, a teardown fault (the old arena given no time to close, so the reset fails and
the next must recover; or the actors given no time to leave, so they are logged out where they stand), an abort while
the reset tears down (its completion must then be ignored), and hidden changes (an actor's own cooldown, the enemy's
cooldown, the enemy's position out of sight). Every reset that completes must be clean: the worker's own checks pass,
its clean state and both initial observations equal those of a reset straight after a worldserver restart (the
reference), the initial observations equal what the manifest alone says they must be, nothing from the old
generation executes or is traced after the reset began, no stock behaviour appears and no progression row changes.

Everything goes to <data root>/runs/<stamp>-reset-acceptance/: report.json; resets.jsonl (one record per reset);
observations.jsonl (actor observations only: what each actor perceived, its mask, decision and percept times);
debug.jsonl (the worker's authoritative state, kept apart from the observations); trace.jsonl (every trace record,
drained from the worker's buffer); the RA transcript; worker logs; fixture snapshots. No credential is written.
"""

from __future__ import annotations

import json
import math
import shutil
import time
from pathlib import Path

from .. import actions, observation
from . import acceptance, control, mysql, world
from .layout import Layout, norm

READY = "running"
RESETTING = "resetting"
CYCLE = ("plain", "combat", "death", "mid_cast", "cc", "delayed_action", "projectile", "disconnect",
         "console_disconnect", None)
TENTH = ("arena_fault", "leave_fault", "hidden", "abort_mid_reset")
VOLATILE_CLEAN = ()   # every clean-state field is compared with the reference


class ResetError(Exception):
    pass


def _lines(prefix: str, text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        if line.startswith(prefix + " "):
            out.append(json.loads(line[len(prefix) + 1:]))
    return out


class Run:
    def __init__(self, layout: Layout, manifest: dict, out_dir: Path, log=print):
        self.layout = layout
        self.manifest = manifest
        self.out = out_dir
        self.log = log
        self.transcript: list = []
        self.console: control.Console | None = None
        self.seq = 0
        self.trace: list[dict] = []
        self.lost = 0
        self.files = {name: open(out_dir / f"{name}.jsonl", "w", encoding="utf-8")
                      for name in ("resets", "observations", "debug", "trace", "decisions")}
        self.catalog: actions.Catalog | None = None
        self.schema: observation.Schema | None = None
        self.context: dict = {}
        self.positions: dict = {}
        self.results: list[dict] = []

    # --- plumbing ---

    def close_files(self) -> None:
        for f in self.files.values():
            f.close()

    def write(self, name: str, record: dict) -> None:
        self.files[name].write(json.dumps(record, sort_keys=True) + "\n")
        self.files[name].flush()

    def connect(self) -> control.Console:
        if self.console:
            self.console.close()
        self.console = control.Console(self.layout, self.transcript, self.log)
        return self.console

    def __call__(self, text: str) -> str:
        return self.console(text)

    def drain(self) -> list[dict]:
        """Every trace record since the last drain, into trace.jsonl; a gap is lost evidence."""
        got: list[dict] = []
        while True:
            out = self(f"dojo control trace since {self.seq} 2000")
            records = _lines("dojo-trace", out)
            if "LOST" in out.splitlines()[-1]:
                self.lost += int(out.splitlines()[-1].split("LOST ")[1].split()[0])
            for r in records:
                self.write("trace", r)
                self.seq = max(self.seq, r["seq"])
            got += records
            if len(records) < 2000:
                break
        self.trace += got
        return got

    def status(self) -> dict:
        return _lines("dojo-episode", self("dojo episode status"))[0]

    def evidence(self) -> dict:
        return _lines("dojo-reset-evidence", self("dojo episode evidence"))[0]

    def observe(self, label: str) -> list[dict]:
        obs = _lines("dojo-observation", self("dojo episode observe full"))
        for o in obs:
            self.write("observations", {"label": label, **o})
        return obs

    def debug(self, label: str, last: int = 1) -> list[dict]:
        records = _lines("dojo-debug", self(f"dojo episode debug {last}"))
        for r in records:
            self.write("debug", {"label": label, **r})
        return records

    def decide(self, a0: str, a1: str, force: bool = False) -> dict:
        out = self(f"dojo episode decide {a0} {a1}{' force' if force else ''}")
        found = _lines("dojo-decision", out)
        if not found:
            raise ResetError(f"decision refused: {out[:300]}")
        d = found[0]
        self.write("decisions", d)
        for o in d["observations"]:
            self.write("observations", {"label": f"decision {d['decision']}", **o})
        return d

    def value(self, obs: dict, name: str) -> float:
        return obs["values"][self.schema.index(name)]

    def bit(self, obs: dict, action: str) -> int:
        return int(obs["mask"][self.catalog.index(action)])

    def wait_status(self, predicate, timeout: float, what: str, poll: float = 1.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            s = self.status()
            if predicate(s):
                return s
            if time.monotonic() > deadline:
                raise ResetError(f"timed out after {timeout:.0f} s waiting for {what}: {s['phase']} {s['result']}")
            time.sleep(poll)

    def place(self, slot: int, pos, facing: float) -> None:
        self(f"dojo harness place {slot} {pos[0]:.3f} {pos[1]:.3f} {pos[2]:.3f} {facing:.4f}")

    # --- one reset ---

    def reset(self, reason: str, *, drop_console: bool = False, abort_after: float | None = None) -> dict:
        before = self.status()
        began_seq = before["trace_seq"]
        out = self(f"dojo episode reset {reason}")
        started = _lines("dojo-episode-reset", out)
        if not started:
            raise ResetError(f"reset not started: {out[:300]}")
        generation = started[0]["generation"]
        record = {"reason": reason, "generation": generation, "token": started[0]["token"],
                  "previous_generation": before["generation"], "previous_phase": before["phase"],
                  "previous_first_action_id": before["first_action_id"], "began_seq": began_seq,
                  "wall_began": round(time.time(), 3)}
        if drop_console:
            # The console connection drops mid-reset; the world-side reset goes on and is found again by generation.
            self.console.close()
            self.console = None
            time.sleep(2.0)
            self.connect()
            record["console_reconnected"] = True
        if abort_after is not None:
            time.sleep(abort_after)
            record["abort"] = self(f"dojo episode abort {reason}")
        final = self.wait_status(lambda s: not s["phase"].startswith(RESETTING) or s["generation"] != generation,
                                 240, f"reset {generation}")
        record["status"] = final
        record["wall_seconds"] = round(time.time() - record["wall_began"], 1)
        record["evidence"] = self.evidence()
        record["trace"] = self.drain()
        if final["phase"] == READY:
            record["ready_debug"] = self.debug(f"ready {generation}", 1)
            for o in record["evidence"]["initial"]:
                self.write("observations", {"label": f"initial {generation}", **o})
        return record

    # --- the conditions an episode is reset from ---

    def visible_pair(self, near: bool = False) -> None:
        clear = self.positions["clear"]
        self.place(0, clear["a"], clear["a_facing"])
        self.place(1, clear["near"] if near else clear["b"], clear["b_facing"])
        time.sleep(1.2)   # the near teleports' acks, then the perceptual delay

    def condition(self, case: str) -> dict:
        """Puts the running episode into the case's state and says what it did."""
        info: dict = {"case": case}
        if case == "plain":
            info["decisions"] = [self.decide("noop", "noop") for _ in range(3)]
        elif case == "combat":
            self.visible_pair()
            info["decisions"] = [self.decide("target_enemy/hold/toward", "target_enemy/hold/toward")]
            time.sleep(0.3)
            info["decisions"].append(self.decide("ice_lance/hold/toward", "ice_lance/hold/toward"))
            time.sleep(1.8)
            info["decisions"].append(self.decide("frostbolt/hold/toward", "none/left/keep"))
            time.sleep(1.5)
        elif case == "death":
            slot = len([r for r in self.results if r.get("case") == "death"]) % 2
            info["kill"] = self(f"dojo harness kill {slot}")
            time.sleep(0.5)
            info["debug"] = self.debug("death", 1)
            info["dead"] = [not t["self"]["alive"] for t in info["debug"][-1]["truth"]]
        elif case == "mid_cast":
            self.visible_pair()
            seq = self.status()["trace_seq"]
            d = self.decide("frostbolt/hold/toward", "noop")
            info["decision"] = d
            bolt = d["requested"][0]["action_id"]
            ran = self.console.wait_record(seq, lambda r: r["producer"] == "executor" and r["kind"] == "command"
                                           and r["action_id"] == bolt, 3, "the frostbolt command to execute")
            info["executed"] = ran
            info["casting"] = ran["outcome"] == "executed" and "casting" in ran["detail"]
            time.sleep(0.6)
        elif case == "cc":
            self.visible_pair(near=True)
            info["decision"] = self.decide("noop", "frost_nova/hold/keep")
            time.sleep(0.6)
            info["debug"] = self.debug("cc", 1)
            info["rooted"] = info["debug"][-1]["truth"][0]["self"]["rooted"]
        elif case == "delayed_action":
            self.visible_pair()
            info["decision"] = self.decide("frostbolt/hold/toward", "ice_lance/hold/toward")
        elif case == "projectile":
            self.visible_pair()
            seq = self.status()["trace_seq"]
            d = self.decide("frostbolt/hold/toward", "noop")
            info["decision"] = d
            bolt = d["requested"][0]["action_id"]
            info["completed"] = self.console.wait_record(seq, lambda r: r["kind"] == "cast" and r["action_id"] == bolt
                                                         and r["outcome"] in ("completed", "cancelled", "failed"), 6,
                                                         "the frostbolt to leave its caster")
        elif case == "disconnect":
            slot = len([r for r in self.results if r.get("case") == "disconnect"]) % 2
            info["disconnect"] = self(f"dojo harness disconnect {slot}")
            info["aborted"] = self.wait_status(lambda s: s["phase"] == "aborted", 10, "the episode to abort")
        elif case in ("console_disconnect", "arena_fault", "leave_fault", "abort_mid_reset"):
            info["decisions"] = [self.decide("noop", "noop")]
        elif case == "hidden":
            info.update(self.hidden())
        return info

    # --- hidden changes ---

    def probe_until(self, until: float) -> list[list[dict]]:
        out = []
        while time.monotonic() < until:
            out.append(self.observe("probe"))
        return out

    def hidden(self) -> dict:
        """Hidden changes reach an actor's observation and masks only once observable, and never sooner than the
        perceptual delay: its own cooldown cleared, the enemy's cooldown cleared, the enemy moved out of sight."""
        delay = self.context["perceptual_delay_ms"]
        result: dict = {}
        # 1. Own cooldown: actor 0 casts Frost Nova, then its cooldowns are cleared at t0.
        self.visible_pair(near=True)
        self.decide("frost_nova/hold/keep", "noop")
        time.sleep(0.6)
        harness = _lines("dojo-harness", self("dojo harness cooldowns 0"))[0]
        t0 = harness["at_ms"]
        probes = [p[0] for p in self.probe_until(time.monotonic() + 0.6)]
        before = [p for p in probes if p["percept_ms"] < t0]
        after = [p for p in probes if p["percept_ms"] >= t0]
        own_ok = (bool(after) and all(self.value(p, "self.kit.frost_nova.cooldown") > 0
                                      and self.bit(p, "frost_nova/hold/keep") == 0 for p in before)
                  and all(self.value(p, "self.kit.frost_nova.cooldown") == 0
                          and self.bit(p, "frost_nova/hold/keep") == 1 for p in after)
                  and all(p["decision_ms"] - t0 >= delay for p in after))
        result["own_cooldown"] = {"ok": own_ok, "changed_at_ms": t0, "before": len(before), "after": len(after),
                                  "first_seen_decision_ms": after[0]["decision_ms"] if after else None}
        # 2. The enemy's cooldown: actor 1 casts Frost Nova where actor 0 sees it, then its cooldowns are cleared.
        time.sleep(1.6)   # the global cooldown
        self.decide("noop", "frost_nova/hold/keep")
        time.sleep(0.6)
        seen = self.observe("enemy use")[0]
        harness = _lines("dojo-harness", self("dojo harness cooldowns 1"))[0]
        t1 = harness["at_ms"]
        probes = self.probe_until(time.monotonic() + 0.6)
        base = next(k["base_cooldown_ms"] for k in self.context["kit"] if k["name"] == "frost_nova")
        inferred_ok = self.value(seen, "enemy.kit.frost_nova.unknown") == 0
        for p0, p1 in probes:
            remaining = self.value(p0, "enemy.kit.frost_nova.cooldown") * 30000
            # It keeps counting down from the use actor 0 saw; the clear is not observable.
            inferred_ok = inferred_ok and remaining > base - 3000 and self.value(p0, "enemy.kit.frost_nova.unknown") == 0
        enemy_after = [p1 for _, p1 in probes if p1["percept_ms"] >= t1]
        enemy_before = [p1 for _, p1 in probes if p1["percept_ms"] < t1]
        own1_ok = (bool(enemy_after) and all(self.bit(p, "frost_nova/hold/keep") == 0 for p in enemy_before)
                   and all(self.bit(p, "frost_nova/hold/keep") == 1 for p in enemy_after)
                   and all(p["decision_ms"] - t1 >= delay for p in enemy_after))
        result["enemy_cooldown"] = {"ok": inferred_ok and own1_ok, "changed_at_ms": t1, "inferred_unchanged": inferred_ok,
                                    "owner_sees_it_after_the_delay": own1_ok, "probes": len(probes)}
        # 3. The enemy's position out of sight: seen at one spot, then moved twice behind a pillar.
        blocked = self.positions["blocked"]
        self.place(0, blocked["a"], blocked["a_facing"])
        self.place(1, self.positions["seen_from_blocked_a"], blocked["b_facing"])
        time.sleep(1.2)
        seen = self.observe("seen")[0]
        self.place(1, blocked["b"], blocked["b_facing"])
        time.sleep(1.2)
        hidden1 = self.observe("hidden 1")[0]
        out = self.console(f"dojo harness place 1 {self.positions['hidden_b2'][0]:.3f} {self.positions['hidden_b2'][1]:.3f} "
                           f"{self.positions['hidden_b2'][2]:.3f} {blocked['b_facing']:.4f}")
        t2 = int(out.rsplit(" at ", 1)[1].split()[0])
        probes = [p[0] for p in self.probe_until(time.monotonic() + 0.8)]
        rel = ("enemy.forward", "enemy.left", "enemy.up", "enemy.distance", "enemy.visible", "enemy.present")
        same = all(all(abs(self.value(p, n) - self.value(hidden1, n)) < 1e-6 for n in rel)
                   and p["mask"] == hidden1["mask"] for p in probes)
        position_ok = (self.value(seen, "enemy.visible") == 1 and self.value(hidden1, "enemy.visible") == 0
                       and self.value(hidden1, "enemy.present") == 1 and same)
        result["enemy_position"] = {"ok": position_ok, "moved_at_ms": t2, "probes": len(probes), "unchanged": same,
                                    "visible_before": self.value(seen, "enemy.visible"),
                                    "visible_hidden": self.value(hidden1, "enemy.visible")}
        # 4. Back into sight: visible again only after the delay.
        out = self.console(f"dojo harness place 1 {self.positions['seen_from_blocked_a'][0]:.3f} "
                           f"{self.positions['seen_from_blocked_a'][1]:.3f} {self.positions['seen_from_blocked_a'][2]:.3f} "
                           f"{blocked['b_facing']:.4f}")
        t3 = int(out.rsplit(" at ", 1)[1].split()[0])
        probes = [p[0] for p in self.probe_until(time.monotonic() + 1.5)]
        visible = [p for p in probes if self.value(p, "enemy.visible") == 1]
        back_ok = bool(visible) and all(p["decision_ms"] - t3 >= delay for p in visible)
        result["back_in_sight"] = {"ok": back_ok, "moved_at_ms": t3,
                                   "first_visible_decision_ms": visible[0]["decision_ms"] if visible else None}
        result["ok"] = all(v["ok"] for v in result.values() if isinstance(v, dict))
        return result

    # --- checks of a completed reset ---

    def check(self, record: dict, reference: dict | None, baseline: dict | None, root: mysql.Client,
              guids: list[int]) -> list[str]:
        problems: list[str] = []
        status, ev = record["status"], record["evidence"]
        if status["phase"] != READY:
            return [f"not ready: {status['phase']}: {status['result']}"]
        if status["generation"] != record["generation"] or ev["generation"] != record["generation"]:
            problems.append(f"generation {status['generation']} is not the reset's {record['generation']}")
        if record["generation"] != record["previous_generation"] + 1:
            problems.append(f"generation jumped from {record['previous_generation']} to {record['generation']}")
        if ev["clean"].get("problems"):
            problems.append("worker clean-state problems: " + "; ".join(ev["clean"]["problems"]))
        first_new = status["first_action_id"]
        old_first = record["previous_first_action_id"]
        for r in record["trace"]:
            if r["seq"] <= record["began_seq"]:
                continue
            if r["producer"].startswith("stock"):
                problems.append(f"stock record {r['seq']}: {r['kind']} {r['detail']}")
            old = r["action_id"] and r["action_id"] < first_new
            if old and not (r["kind"] == "command" and r["outcome"] == "dropped"):
                problems.append(f"record {r['seq']} of action {r['action_id']} from before generation "
                                f"{record['generation']}: {r['producer']} {r['kind']} {r['outcome']}")
        initial = ev["initial"]
        if len(initial) != 2:
            problems.append(f"{len(initial)} initial observations")
            return problems
        for o in initial:
            problems += [f"actor {o['actor']}: {p}" for p in self.schema.check(o["values"])]
            if o["age_ms"] < self.context["perceptual_delay_ms"]:
                problems.append(f"actor {o['actor']}: first observation only {o['age_ms']} ms old")
            if o["generation"] != record["generation"]:
                problems.append(f"actor {o['actor']}: observation of generation {o['generation']}")
        # What the manifest alone says the first observations must be.
        debug = record.get("ready_debug") or []
        costs = None
        if debug:
            costs = [[k["cost"] for k in t["self"]["kit"]] for t in debug[-1]["truth"]]
        for o in initial:
            slot = o["actor"]
            expected = observation.expected_initial(
                self.schema, self.catalog, self.manifest, slot, self.context,
                enemy_visible=bool(ev["clean"]["sees"][slot]), start_offset_ms=o["start_offset_ms"],
                kit_costs=costs[slot] if costs else None)
            skip = () if costs else tuple(f"self.kit.{k}.cost" for k in self.schema.kit)
            if ev["clean"]["sees"][slot]:
                skip += tuple(f"self.kit.{k}.in_range" for k in self.schema.kit)
            diffs = observation.compare(expected, o["values"], self.schema, tolerance=1e-4, skip=skip)
            problems += [f"actor {slot} differs from the manifest: {d}" for d in diffs]
        if reference is not None:
            for slot in (0, 1):
                ref_clean = reference["evidence"]["clean"]["actors"][slot]
                got_clean = ev["clean"]["actors"][slot]
                for key in sorted(set(ref_clean) | set(got_clean)):
                    if key not in VOLATILE_CLEAN and ref_clean.get(key) != got_clean.get(key):
                        problems.append(f"actor {slot} clean state {key}: {got_clean.get(key)} (reference "
                                        f"{ref_clean.get(key)})")
                ref_obs = reference["evidence"]["initial"][slot]
                diffs = observation.compare(ref_obs["values"], initial[slot]["values"], self.schema, tolerance=1e-6,
                                            skip=("match.remaining",))
                problems += [f"actor {slot} initial observation differs from the reference: {d}" for d in diffs]
            if ev["clean"]["sees"] != reference["evidence"]["clean"]["sees"]:
                problems.append("start visibility differs from the reference")
        if baseline is not None:
            side = acceptance.compare(baseline, acceptance.snapshot(root, self.layout, guids))
            record["side_effects"] = side
            problems += [f"progression: {p}" for p in side["progression"]]
        return problems


def find_hidden_spots(run: Run) -> None:
    """Around the blocked pair: a free spot actor 0 at blocked.a sees, and a second free spot behind the pillar."""
    c = run.console
    blocked = run.positions["blocked"]
    a = blocked["a"]
    b = blocked["b"]

    def free(x, y):
        low = c.los((x, y, a[2] + 1.0), (x, y, a[2] + 1.0))["ground"][0]
        top = c.los((x, y, a[2] + 30.0), (x, y, a[2] + 30.0))["ground"][0]
        if low < -1000 or top < -1000 or abs(low - a[2]) > 1.5 or top - low > 1.0:
            return None
        return low

    for step in range(48):
        angle = step * math.pi / 12
        dist = 7.0 + (step % 4)
        x, y = a[0] + dist * math.cos(angle), a[1] + dist * math.sin(angle)
        g = free(x, y)
        if g is not None and c.los(a, (x, y, g + 0.1))["los"]:
            run.positions["seen_from_blocked_a"] = (x, y, g + 0.1)
            break
    for step in range(48):
        angle = step * math.pi / 12
        dist = 2.5 + (step % 3)
        x, y = b[0] + dist * math.cos(angle), b[1] + dist * math.sin(angle)
        g = free(x, y)
        if g is not None and not c.los(a, (x, y, g + 0.1))["los"]:
            run.positions["hidden_b2"] = (x, y, g + 0.1)
            break
    missing = [k for k in ("seen_from_blocked_a", "hidden_b2") if k not in run.positions]
    if missing:
        raise ResetError(f"no arena spots for the hidden-position case: {missing}")


def schedule(count: int) -> list[str]:
    cases, tenth = [], 0
    while len(cases) < count:
        for case in CYCLE:
            if case is None:
                case = TENTH[tenth % len(TENTH)]
                tenth += 1
            cases.append(case)
            if len(cases) == count:
                break
    return cases


def run(layout: Layout, manifest: dict, resets: int = 100, cases: list[str] | None = None, log=print) -> dict:
    """cases, when given, replaces the schedule (a trial of chosen conditions); resets is then its length."""
    unknown = [c for c in cases or [] if c not in CYCLE + TENTH]
    if unknown:
        raise ResetError(f"unknown cases: {unknown}")
    if cases:
        resets = len(cases)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = layout.data / "runs" / f"{stamp}-reset-acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"kind": "leeroy-dojo-reset-acceptance", "format": 1, "started": stamp,
                    "instance": layout.instance, "scenario": manifest["id"], "resets_planned": resets}
    root = mysql.Client(layout, layout.server.root_cnf)
    report["mysql"] = mysql.verify_identity(layout, root)
    report["live_before"] = acceptance.live_fingerprint(layout)
    names = [a["name"] for a in manifest["actors"]]
    guids = acceptance.fixture_guids(root, layout, names)
    if len(guids) != len(names):
        raise world.WorldError("the fixtures do not exist; run dojo fixture create and build first")
    r = Run(layout, manifest, out_dir, log)
    checks: list[dict] = []

    def keep_logs(label: str) -> None:
        """A worldserver start rewrites its logs: keep each process's before the next one starts."""
        logs = out_dir / "worker-logs"
        logs.mkdir(exist_ok=True)
        for name in ("Server.log", "Playerbots.log"):
            if (layout.logs / name).exists():
                shutil.copy2(layout.logs / name, logs / f"{label}-{name}")

    def restart(label: str) -> dict:
        """The reference: a fresh worldserver process, then a reset."""
        if world.running(layout):
            if r.console:
                r.console.close()
                r.console = None
            world.stop(layout, log=log)
            keep_logs(f"before-{label}")
        world.start(layout, log=log)
        r.connect()
        r.seq = r.status()["trace_seq"]
        record = r.reset(label)
        record["case"] = label
        return record

    try:
        if world.running(layout):
            world.stop(layout, log=log)
        world.start(layout, log=log)
        r.connect()
        r("dojo fixture restore")
        report["restore_before"] = r.console.wait_job()
        baseline = acceptance.snapshot(root, layout, guids)
        report["schema"] = _lines("dojo-schema", r("dojo episode schema"))[0]
        r.context = _lines("dojo-context", r("dojo episode schema"))[0]
        cat_doc = json.loads(r("dojo control actions").splitlines()[0][len("dojo-catalog "):])
        r.catalog = actions.build_catalog(cat_doc["kit"])
        r.schema = observation.build_schema(r.catalog)
        schema_ok = r.schema.document == report["schema"] and r.catalog.document == cat_doc
        checks.append({"check": "the worker's observation schema and catalog equal leeroy_dojo's", "ok": schema_ok,
                       "schema_sha256": r.schema.sha256, "size": r.schema.size})
        context_problems = observation.context_problems(r.context, manifest)
        checks.append({"check": "the worker's context matches the manifest", "ok": not context_problems,
                       "problems": context_problems, "context": r.context})

        reference = restart("reference-before")
        reference_problems = r.check(reference, None, baseline, root, guids)
        r.write("resets", {k: v for k, v in reference.items() if k != "trace"})
        checks.append({"check": "the restart-based reference reset is clean and matches the manifest",
                       "ok": not reference_problems, "problems": reference_problems,
                       "seconds": reference["wall_seconds"]})
        if reference_problems:
            raise ResetError("the reference reset is not clean: " + "; ".join(reference_problems[:5]))

        # Positions for the cases, found once in the reference episode.
        center = (manifest["arena"]["bounds"]["x"], manifest["arena"]["bounds"]["y"])
        r.positions = control.find_positions(r.console, center)
        find_hidden_spots(r)
        report["positions"] = r.positions

        plan = list(cases) + ["plain"] if cases else schedule(resets) + ["plain"]   # an abort's retry may need one more
        done = 0
        expect_failure = False
        while done < resets:
            case = plan[done]
            log(f"reset {done + 1}/{resets}: {case}")
            status = r.status()
            info: dict = {"case": case}
            if status["phase"] == READY:
                try:
                    info = r.condition(case)
                except (ResetError, control.ControlError) as e:
                    info = {"case": case, "condition_error": str(e)}
            else:
                info["from_phase"] = status["phase"]
            if case == "arena_fault":
                info["fault"] = r("dojo episode fault arena")
            elif case == "leave_fault":
                info["fault"] = r("dojo episode fault leave")
            record = r.reset(f"{case}-{done + 1}", drop_console=case == "console_disconnect",
                             abort_after=1.0 if case == "abort_mid_reset" else None)
            done += 1
            record.update(info)
            record["index"] = done
            expect_failure = case == "arena_fault"
            if case == "abort_mid_reset":
                # The aborted reset counts; the next one must wait for the director, then succeed, and the abandoned
                # completion must have been ignored.
                ok_abort = record["status"]["phase"] == "aborted"
                record["problems"] = [] if ok_abort else [f"abort left phase {record['status']['phase']}"]
                record["ok"] = ok_abort
                r.results.append(record)
                r.write("resets", {k: v for k, v in record.items() if k != "trace"})
                if done >= resets:
                    break
                r.wait_status(lambda s: not s["director"]["running"], 240, "the abandoned reset's director job")
                retry = r.reset(f"after-abort-{done + 1}")
                done += 1
                retry["case"] = "after_abort"
                retry["index"] = done
                retry["problems"] = r.check(retry, reference, baseline, root, guids)
                if retry["status"]["counts"]["stale_completions"] < 1:
                    retry["problems"].append("the abandoned reset's completion was not reported as ignored")
                retry["ok"] = not retry["problems"]
                r.results.append(retry)
                r.write("resets", {k: v for k, v in retry.items() if k != "trace"})
                log(f"  {'ok  ' if retry['ok'] else 'FAIL'} after abort: {retry['problems'][:3]}")
                continue
            if expect_failure:
                failed = (record["status"]["phase"] == "failed" and "injected fault: arena" in record["status"]["result"]
                          and injected(record))
                record["problems"] = [] if failed else [f"the injected teardown fault did not fail the reset: "
                                                        f"{record['status']['phase']} {record['status']['result']}"]
            else:
                record["problems"] = r.check(record, reference, baseline, root, guids)
                if "condition_error" in info:
                    record["problems"].append("the case was not set up: " + info["condition_error"])
                if case == "mid_cast" and not info.get("casting"):
                    record["problems"].append("actor 0 was not casting before the reset")
                if case == "death" and not any(info.get("dead", [])):
                    record["problems"].append("no actor was dead before the reset")
                if case == "cc" and not info.get("rooted"):
                    record["problems"].append("actor 0 was not rooted before the reset")
                if case == "disconnect" and info.get("aborted", {}).get("phase") != "aborted":
                    record["problems"].append("the disconnect did not abort the episode")
                if case == "hidden" and not info.get("ok"):
                    record["problems"].append("a hidden change reached an observation or a mask early")
                if case == "leave_fault" and not injected(record):
                    record["problems"].append("the leave fault was not reached by this reset")
                if case in ("delayed_action", "projectile", "mid_cast"):
                    record["problems"] += leaked(record, info)
            record["ok"] = not record["problems"]
            r.results.append(record)
            r.write("resets", {k: v for k, v in record.items() if k != "trace"})
            log(f"  {'ok  ' if record['ok'] else 'FAIL'} generation {record['generation']} in {record['wall_seconds']} s"
                + (f": {record['problems'][:3]}" if record["problems"] else ""))
            if record["status"]["phase"] != READY and not expect_failure:
                # Recover with a further reset; a failure here is the acceptance's own failure.
                log("  not ready; the next reset must recover")

        reference_after = restart("reference-after")
        after_problems = r.check(reference_after, reference, baseline, root, guids)
        r.write("resets", {k: v for k, v in reference_after.items() if k != "trace"})
        checks.append({"check": "a second restart-based reset equals the first", "ok": not after_problems,
                       "problems": after_problems})
        r("dojo arena close")
        report["close_result"] = r.console.wait_job()
        report["trace_lost"] = r.lost
        report["trace_records"] = len(r.trace)
    finally:
        if r.console:
            r.console.close()
        r.close_files()
    world.stop(layout, log=log)
    logs = out_dir / "worker-logs"
    logs.mkdir(exist_ok=True)
    for name in ("Server.log", "Playerbots.log"):
        if (layout.logs / name).exists():
            shutil.copy2(layout.logs / name, logs / name)
    after = acceptance.snapshot(root, layout, guids)
    report["side_effects"] = acceptance.compare(baseline, after)
    report["live_after"] = acceptance.live_fingerprint(layout)
    lb, la = report["live_before"], report["live_after"]
    report["live_untouched"] = (lb["configs"] == la["configs"] and lb["data"] == la["data"]
                                and {k: v for k, v in lb["processes"].items() if "dojo-runtime" not in v["path"]}
                                == {k: v for k, v in la["processes"].items() if "dojo-runtime" not in v["path"]}
                                and lb["mysql84_service"] == la["mysql84_service"])
    results = r.results
    counted = [x for x in results]
    by_case: dict[str, dict] = {}
    for x in counted:
        c = by_case.setdefault(x["case"], {"resets": 0, "ok": 0})
        c["resets"] += 1
        c["ok"] += 1 if x["ok"] else 0
    ready = [x for x in counted if x["status"]["phase"] == READY]
    seconds = sorted(x["wall_seconds"] for x in ready)
    report["summary"] = {
        "resets": len(counted), "ok": sum(1 for x in counted if x["ok"]), "ready": len(ready),
        "by_case": by_case, "reset_seconds": {"min": seconds[0] if seconds else None,
                                              "median": seconds[len(seconds) // 2] if seconds else None,
                                              "max": seconds[-1] if seconds else None},
        "consecutive_ok": _longest_run(counted)}
    checks.append({"check": f"{resets} consecutive resets behave as expected", "ok":
                   len(counted) == resets and all(x["ok"] for x in counted), "failed": [
                       {"index": x["index"], "case": x["case"], "problems": x["problems"][:5]} for x in counted
                       if not x["ok"]]})
    checks.append({"check": "no trace record was lost", "ok": report.get("trace_lost", 1) == 0})
    checks.append({"check": "no progression side effects", "ok": report["side_effects"]["ok"],
                   "progression": report["side_effects"]["progression"],
                   "bookkeeping": report["side_effects"]["bookkeeping"]})
    checks.append({"check": "live install untouched", "ok": report["live_untouched"]})
    report["checks"] = checks
    report["ok"] = all(c["ok"] for c in checks)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "ra-transcript.jsonl").write_text("".join(json.dumps(t) + "\n" for t in r.transcript), encoding="utf-8")
    report["out"] = norm(out_dir)
    return report


def leaked(record: dict, info: dict) -> list[str]:
    """For a command still pending, a cast in progress or a projectile in flight at the reset: the command was dropped
    (or had finished) and nothing it started reached the new episode."""
    problems = []
    ids = []
    for key in ("decision",):
        d = info.get(key)
        if d:
            ids += [x["action_id"] for x in d["requested"] if x["action"] != 0]
    after = [t for t in record["trace"] if t["seq"] > record["began_seq"]]
    for action_id in ids:
        for t in after:
            if t["action_id"] == action_id and not (t["kind"] == "command" and t["outcome"] == "dropped"):
                problems.append(f"action {action_id} still produced {t['producer']} {t['kind']} {t['outcome']} after the reset")
    if info.get("case") == "delayed_action":
        dropped = [t for t in after if t["kind"] == "command" and t["outcome"] == "dropped" and t["action_id"] in ids]
        if len(dropped) != len(ids):
            problems.append(f"{len(dropped)} of {len(ids)} pending commands were dropped by the reset")
    return problems


def injected(record: dict) -> bool:
    """The director traced the armed fault as used by this reset."""
    return any(t["kind"] == "fault" and t["outcome"] == "injected" for t in record["trace"] if t["seq"] > record["began_seq"])


def _longest_run(results: list[dict]) -> int:
    best = cur = 0
    for x in results:
        cur = cur + 1 if x["ok"] else 0
        best = max(best, cur)
    return best
