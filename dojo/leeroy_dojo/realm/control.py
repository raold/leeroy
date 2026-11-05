"""Task 6 acceptance: exclusive control and ordinary execution on the training realm.

Drives the worker's console (`dojo control`, `dojo harness`) over RA and keeps everything it saw. In the training
arena: owned idle actors produce nothing; a released actor resumes stock behaviour and stops again when re-owned;
mask queries change nothing; and the listed execution cases (global cooldown, insufficient mana, rooted movement,
line-of-sight failure, interruption, a superseded command, no correction during a cast) are each refused or carried
out by the core's own rules with the reason recorded. Then the worldserver shuts down while both actors are owned,
and its logs show ownership held to the end. Fixture rows are compared before and after, and the live install is
fingerprinted, as in the realm acceptance.

The evidence goes to <data root>/runs/<stamp>-control-acceptance/: report.json, the RA transcript, every trace record
and the worker's logs from the shutdown. It holds ids, hashes, counts and positions, never a credential.
"""

from __future__ import annotations

import json
import math
import random
import shutil
import time
from pathlib import Path

from .. import actions
from . import acceptance, mysql, world
from .layout import Layout, norm

ARENA_IN_PROGRESS = 3          # BattlegroundStatus STATUS_IN_PROGRESS
SPELL_FROST_NOVA = 42917
PERCEIVED = 0.25               # seconds for a change to pass the 100 ms perceptual delay and reach a mask


class ControlError(Exception):
    pass


def _json_after(prefix: str, text: str) -> dict:
    for line in text.splitlines():
        if line.startswith(prefix + " "):
            return json.loads(line[len(prefix) + 1:])
    raise ControlError(f"no {prefix} line in: {text[:400]}")


class Console:
    """One RA session and a transcript of every command."""

    def __init__(self, layout: Layout, transcript: list, log=print):
        self.layout = layout
        self.transcript = transcript
        self.log = log
        self.client = world.ra(layout)

    def close(self) -> None:
        self.client.close()

    def __call__(self, text: str) -> str:
        out = self.client.command(text)
        self.transcript.append({"at": round(time.time(), 3), "command": text, "output": out})
        return out

    def status(self) -> dict:
        return _json_after("dojo-control", self("dojo control status"))

    def trace(self) -> list[dict]:
        out = self("dojo control trace 4096")
        return [json.loads(line[len("dojo-trace "):]) for line in out.splitlines() if line.startswith("dojo-trace ")]

    def seq(self) -> int:
        return int(self.status()["trace_seq"])

    def since(self, seq: int) -> list[dict]:
        return [r for r in self.trace() if r["seq"] > seq]

    def act(self, slot: int, *names: str, force: bool = False) -> list[int]:
        out = self(f"dojo control act {slot} {' '.join(names)}{' force' if force else ''}")
        ids = []
        for line in out.splitlines():
            if ": action " in line:
                ids.append(int(line.split(": action ", 1)[1].split()[0]))
        if not ids:
            raise ControlError(f"no action accepted: {out}")
        return ids

    def masks(self, slot: int, full: bool = False) -> dict:
        return _json_after("dojo-masks", self(f"dojo control masks {slot}{' all' if full else ''}"))

    def maskcheck(self, slot: int, repeats: int) -> dict:
        return _json_after("dojo-maskcheck", self(f"dojo control maskcheck {slot} {repeats}"))

    def los(self, a: tuple, b: tuple) -> dict:
        return _json_after("dojo-los", self("dojo harness los " + " ".join(f"{v:.3f}" for v in (*a, *b))))

    def wait(self, predicate, timeout: float, what: str, poll: float = 0.2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = predicate()
            if found:
                return found
            time.sleep(poll)
        raise ControlError(f"timed out after {timeout:.0f} s waiting for {what}")

    def wait_record(self, seq: int, match, timeout: float, what: str) -> dict:
        def find():
            for r in self.since(seq):
                if match(r):
                    return r
            return None
        return self.wait(find, timeout, what)

    def actor(self, slot: int) -> dict:
        return self.status()["actors"][slot]

    def wait_job(self, timeout: float = 240) -> str:
        """Polls dojo status on this session (RA serves one session at a time) until no job runs."""
        def done():
            lines = self("dojo status").splitlines()
            if next((l for l in lines if l.startswith("job: ")), "") == "job: none":
                return next((l[len("result: "):] for l in lines if l.startswith("result: ")), "") or "?"
            return None
        return self.wait(done, timeout, "the dojo job to finish", poll=2)


def stock_records(records: list[dict]) -> list[dict]:
    """What a gate failure would look like: an action by an owned actor that no executor command asked for."""
    return [r for r in records if r["producer"] == "stock"]


def counters_delta(before: dict, after: dict, slot: int) -> dict:
    b = before["actors"][slot]["counters"]
    a = after["actors"][slot]["counters"]
    return {k: a[k] - b[k] for k in a}


def find_positions(console: Console, center: tuple[float, float], seed: int = 6) -> dict:
    """Ground-checked spots in the arena: a pair in line of sight about 20 yd apart (with a spot 6 yd from the first
    and the second's mirror about the first), and a pair within range that a pillar blocks. A free spot has floor
    within 1.5 yd of the centre's and nothing above it; pillars are where something stands well above the floor."""
    rng = random.Random(seed)
    cx, cy = center

    def height(x: float, y: float, z: float) -> float | None:
        g = console.los((x, y, z), (x, y, z))["ground"][0]
        return g if g > -1000 else None

    floor = height(cx, cy, 14.0)
    if floor is None:
        raise ControlError("no ground at the arena's centre")

    def free(x: float, y: float) -> float | None:
        low, top = height(x, y, floor + 1.0), height(x, y, floor + 30.0)
        if low is None or top is None or abs(low - floor) > 1.5 or top - low > 1.0:
            return None
        return low

    found: dict = {"floor": floor}
    tried = 0
    while tried < 400 and "clear" not in found:
        tried += 1
        ax, ay = cx + rng.uniform(-20, 20), cy + rng.uniform(-20, 20)
        angle = rng.uniform(0, 2 * math.pi)
        dist = rng.uniform(18, 24)
        bx, by = ax + dist * math.cos(angle), ay + dist * math.sin(angle)
        mx, my = ax - dist * math.cos(angle), ay - dist * math.sin(angle)   # the mirror of b about a
        nx, ny = ax + 6 * math.cos(angle), ay + 6 * math.sin(angle)
        spots = [free(*xy) for xy in ((ax, ay), (bx, by), (mx, my), (nx, ny))]
        if any(g is None for g in spots):
            continue
        a, b, mirror, near = [(x, y, g + 0.1) for (x, y), g in zip(((ax, ay), (bx, by), (mx, my), (nx, ny)), spots)]
        if not all(console.los(a, other)["los"] for other in (b, mirror, near)):
            continue
        facing = math.atan2(by - ay, bx - ax) % (2 * math.pi)
        found["clear"] = {"a": a, "b": b, "mirror": mirror, "near": near, "distance": dist, "a_facing": facing,
                          "b_facing": (facing + math.pi) % (2 * math.pi)}

    pillars = []
    for i in range(-13, 14):
        for j in range(-13, 14):
            x, y = cx + 3 * i, cy + 3 * j
            if math.hypot(x - cx, y - cy) > 38:
                continue
            low, top = height(x, y, floor + 1.0), height(x, y, floor + 30.0)
            tried += 1
            if top is not None and (low is None or top - low > 5.0):
                pillars.append((x, y))
    found["pillar_points"] = len(pillars)
    for px, py in pillars:
        for radius in (9.0, 11.0, 12.5):
            for step in range(12):
                angle = math.pi * step / 12
                ax, ay = px + radius * math.cos(angle), py + radius * math.sin(angle)
                bx, by = px - radius * math.cos(angle), py - radius * math.sin(angle)
                ga, gb = free(ax, ay), free(bx, by)
                tried += 1
                if ga is None or gb is None:
                    continue
                a, b = (ax, ay, ga + 0.1), (bx, by, gb + 0.1)
                if console.los(a, b)["los"]:
                    continue
                facing = math.atan2(by - ay, bx - ax) % (2 * math.pi)
                found["blocked"] = {"a": a, "b": b, "pillar": (px, py), "distance": 2 * radius, "a_facing": facing,
                                    "b_facing": (facing + math.pi) % (2 * math.pi)}
                break
            if "blocked" in found:
                break
        if "blocked" in found:
            break
    found["probes"] = tried
    if "clear" not in found or "blocked" not in found:
        raise ControlError(f"could not find arena positions in {tried} probes: {sorted(found)}")
    return found


class Run:
    def __init__(self, layout: Layout, manifest: dict, log=print):
        self.layout = layout
        self.manifest = manifest
        self.log = log
        self.transcript: list = []
        self.cases: list[dict] = []
        self.console: Console | None = None
        self.catalog_obj: actions.Catalog | None = None

    # --- helpers ---

    def connect(self) -> Console:
        if self.console:
            self.console.close()
        self.console = Console(self.layout, self.transcript, self.log)
        return self.console

    def case(self, name: str, ok: bool, **evidence) -> dict:
        entry = {"case": name, "ok": bool(ok), **evidence}
        self.cases.append(entry)
        self.log(f"{'ok  ' if ok else 'FAIL'} {name}")
        return entry

    def restore(self, positions: tuple | None = None) -> None:
        c = self.console
        c("dojo harness restore all")
        if positions:
            for slot, (pos, facing) in enumerate(positions):
                c(f"dojo harness place {slot} {pos[0]:.3f} {pos[1]:.3f} {pos[2]:.3f} {facing:.4f}")
        time.sleep(1.5)   # the near teleports' acks and a world tick or two
        c("dojo harness restore all")

    def executed(self, seq: int, action_id: int, timeout: float = 5) -> dict:
        return self.console.wait_record(seq, lambda r: r["kind"] == "command" and r["action_id"] == action_id
                                        and r["producer"] == "executor", timeout, f"action {action_id} to execute")

    def wait_job(self, timeout: float = 240) -> str:
        return self.console.wait_job(timeout)

    # --- phases ---

    def catalog(self) -> dict:
        out = self.console("dojo control actions")
        line = next(l for l in out.splitlines() if l.startswith("dojo-catalog "))
        doc = json.loads(line[len("dojo-catalog "):])
        cases = json.loads((Path(__file__).resolve().parents[3] / "modules" / "mod-dojo" / "tests" / "fixtures"
                            / "action_cases.json").read_text(encoding="utf-8"))
        ours = actions.build_catalog(doc["kit"])
        self.catalog_obj = ours
        shared = actions.build_catalog(cases["kit"])
        kit_ids = [k["spell_id"] for k in self.manifest["loadout"]["kit"]]
        ok = (ours.document == doc and ours.sha256 == shared.sha256 and doc["size"] == len(ours.entries)
              and [k["spell_id"] for k in doc["kit"]] == kit_ids)
        manifest_dir = self.layout.data / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        published = {"kind": "leeroy-dojo-action-catalog", "format": 1, "scenario": self.manifest["id"],
                     "schema": actions.SCHEMA, "size": len(ours.entries), "sha256": ours.sha256, "document": doc}
        path = manifest_dir / f"actions-{self.manifest['id']}.json"
        path.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")
        return self.case("catalog matches the shared fixture and the manifest kit", ok, size=len(ours.entries),
                         sha256=ours.sha256, worker_document=doc, published=norm(path))

    def idle(self, seconds: float) -> dict:
        c = self.console
        before = c.status()
        seq = before["trace_seq"]
        time.sleep(seconds)
        after = c.status()
        records = c.since(seq)
        deltas = [counters_delta(before, after, s) for s in (0, 1)]
        skipped = [after["actors"][s]["skipped_updates"] - before["actors"][s]["skipped_updates"] for s in (0, 1)]
        owned = all(after["actors"][s]["owned"] for s in (0, 1))
        quiet = not records and all(not any(d.values()) for d in deltas)
        return self.case(f"owned idle actors produce nothing for {seconds:.0f} s", owned and quiet and min(skipped) > 0,
                         skipped_updates=skipped, counter_deltas=deltas, records=records,
                         state=[after["actors"][s]["state"] for s in (0, 1)])

    def maskcheck(self, label: str) -> dict:
        c = self.console
        checks = [c.maskcheck(slot, 1000) for slot in (0, 1)]
        ok = all(m["unchanged"] and m["stable"] and m["enemy_unchanged"] for m in checks)
        return self.case(f"1000 mask queries per actor change nothing ({label})", ok, checks=checks)

    def release_and_resume(self, slot: int, window: float = 30) -> dict:
        c = self.console
        seq = c.seq()
        released = c(f"dojo control release {slot}")
        stock = None
        try:
            stock = c.wait_record(seq, lambda r: r["actor"] == slot and r["producer"] == "stock (released)", window,
                                  "a stock action by the released actor")
        except ControlError:
            stock = None
        time.sleep(2)   # let stock behaviour carry on for a moment
        reowned = c(f"dojo control own {slot}")
        seq_owned = c.seq()
        time.sleep(10)
        after = c.since(seq_owned)
        records = [r for r in c.trace() if seq < r["seq"] <= seq_owned]
        neutralize = [r for r in records if r["kind"] == "neutralize"]
        quiet = not [r for r in after if r["actor"] == slot and r["producer"].startswith("stock")]
        ok = stock is not None and quiet and c.actor(slot)["owned"]
        return self.case("a released actor resumes stock behaviour; re-owned, it stops", ok, released=released,
                         first_stock_action=stock, reowned=reowned, neutralize=neutralize, released_records=records,
                         after_reown=after)

    def wait_in_progress(self, timeout: float = 120) -> dict:
        c = self.console
        state = c.wait(lambda: (lambda s: s if all(a["state"].get("bg_status") == ARENA_IN_PROGRESS for a in s["actors"])
                                else None)(c.status()), timeout, "the arena to start")
        return state

    def gcd(self, pos: dict) -> dict:
        c = self.console
        self.restore(((pos["a"], pos["a_facing"]), (pos["b"], pos["b_facing"])))
        seq = c.seq()
        first = c.act(0, "ice_lance/hold/toward")[0]
        r1 = self.executed(seq, first)
        time.sleep(PERCEIVED)   # masks answer for the actor's delayed observation (Task 7)
        mask = c.masks(0)
        second = c.act(0, "ice_lance/hold/toward", force=True)[0]
        r2 = self.executed(seq, second)
        time.sleep(1.6)
        seq3 = c.seq()
        third = c.act(0, "ice_lance/hold/toward")[0]
        r3 = self.executed(seq3, third)
        # Inside the spell queue window the core accepts the request early and casts it when the cooldown ends.
        time.sleep(1.15)
        seq4 = c.seq()
        fourth = c.act(0, "ice_lance/hold/toward", force=True)[0]
        r4 = self.executed(seq4, fourth)
        time.sleep(1.0)
        queued = [x for x in c.since(seq4) if x["action_id"] == fourth]
        ok = (r1["outcome"] == "executed" and mask["abilities"]["ice_lance"] == "global cooldown"
              and r2["outcome"] == "rejected" and "not_ready" in r2["detail"] and r3["outcome"] == "executed")
        return self.case("global cooldown: masked, and a forced request is refused by the core", ok, first=r1,
                         mask_after_first=mask["abilities"], forced_second=r2, after_gcd=r3,
                         near_the_end=r4, near_the_end_records=queued,
                         queue_window_note="a request this close to ready is the core's spell queue, not a rejection")

    def mana(self, pos: dict) -> dict:
        c = self.console
        self.restore(((pos["a"], pos["a_facing"]), (pos["b"], pos["b_facing"])))
        c("dojo harness mana 0 0")
        time.sleep(PERCEIVED)
        mask = c.masks(0)
        seq = c.seq()
        forced = c.act(0, "frostbolt/hold/toward", force=True)[0]
        r = self.executed(seq, forced)
        ok = mask["abilities"]["frostbolt"] == "mana" and r["outcome"] == "rejected" and "no_power" in r["detail"]
        return self.case("insufficient mana: masked, and a forced cast is refused by the core", ok,
                         mask=mask["abilities"], knowledge=mask["knowledge"], forced=r)

    def rooted(self, pos: dict) -> dict:
        c = self.console
        attempts = []
        for attempt in range(3):
            self.restore(((pos["a"], pos["a_facing"]), (pos["near"], pos["b_facing"])))
            seq = c.seq()
            nova = c.act(1, "frost_nova/hold/keep")[0]
            rn = self.executed(seq, nova)
            time.sleep(0.3)
            rooted = c.actor(0)["state"]["rooted"]
            attempts.append({"frost_nova": rn, "rooted": rooted})
            if rooted:
                break
        mask = c.masks(0, full=True)
        seq = c.seq()
        move = c.act(0, "none/forward/keep")[0]
        rm = self.executed(seq, move)
        turn = c.act(0, "none/hold/away")[0]
        rt = self.executed(seq, turn)
        state = c.actor(0)["state"]
        open_move = mask["mask"][self.catalog_obj.index("none/forward/keep")] == "1"
        ok = (attempts[-1]["rooted"] and rm["outcome"] == "rejected" and "rooted" in rm["detail"]
              and rt["outcome"] == "executed" and open_move)
        return self.case("rooted movement: left selectable, refused at execution; turning still works", ok,
                         attempts=attempts, move=rm, turn=rt, movement_mask_open=open_move, state=state)

    def line_of_sight(self, pos: dict) -> dict:
        c = self.console
        self.restore(((pos["blocked"]["a"], pos["blocked"]["a_facing"]), (pos["blocked"]["b"], pos["blocked"]["b_facing"])))
        mask = c.masks(0)
        seq = c.seq()
        cast = c.act(0, "frostbolt/hold/toward", force=mask["abilities"]["frostbolt"] != "")[0]
        r = self.executed(seq, cast)
        ok = mask["abilities"]["frostbolt"] == "" and r["outcome"] == "rejected" and "line_of_sight" in r["detail"]
        return self.case("line of sight: not masked (geometry is left to execution), refused by the core", ok,
                         mask=mask["abilities"], distance=mask["knowledge"]["enemy_distance"], cast=r)

    def interruption(self, pos: dict) -> dict:
        c = self.console
        self.restore(((pos["a"], pos["a_facing"]), (pos["b"], pos["b_facing"])))
        seq = c.seq()
        bolt = c.act(0, "frostbolt/hold/toward")[0]
        rb = self.executed(seq, bolt)
        time.sleep(0.5)
        stop = c.act(0, "stop_cast/hold/keep")[0]
        rs = self.executed(seq, stop)
        stopped = c.wait_record(seq, lambda r: r["kind"] == "cast" and r["action_id"] == bolt
                                and r["outcome"] in ("stopped", "cancelled"), 3, "the stopped cast")
        time.sleep(1.8)
        seq2 = c.seq()
        bolt2 = c.act(0, "frostbolt/hold/toward")[0]
        rb2 = self.executed(seq2, bolt2)
        time.sleep(0.5)
        move = c.act(0, "none/left/keep")[0]
        rm = self.executed(seq2, move)
        cancelled = c.wait_record(seq2, lambda r: r["kind"] == "cast" and r["action_id"] == bolt2
                                  and r["outcome"] == "cancelled", 3, "the cast movement cancelled")
        time.sleep(3)
        later = [r for r in c.since(seq2) if r["actor"] == 0 and r["kind"] == "cast" and r["action_id"] != bolt2]
        ok = (rb["outcome"] == "executed" and "casting" in rb["detail"] and rs["outcome"] == "executed"
              and stopped["producer"] == "executor" and rb2["outcome"] == "executed" and rm["outcome"] == "executed"
              and cancelled["producer"] == "core" and "interrupted" in cancelled["detail"] and not later)
        return self.case("interruption: stop-cast stops it, movement cancels it by the core's rule, nothing re-casts",
                         ok, cast=rb, stop=rs, stopped=stopped, cast_again=rb2, move=rm, cancelled=cancelled,
                         casts_afterwards=later)

    def superseded(self, pos: dict) -> dict:
        c = self.console
        self.restore(((pos["a"], pos["a_facing"]), (pos["b"], pos["b_facing"])))
        seq = c.seq()
        ids = c.act(0, "frostbolt/hold/toward", "noop", "ice_lance/hold/toward")
        bolt, lance = ids[0], ids[-1]
        r = self.executed(seq, lance)
        records = [x for x in c.since(seq) if x["kind"] == "command"]
        sup = [x for x in records if x["action_id"] == bolt and x["outcome"] == "superseded"]
        kept = [x for x in records if x["outcome"] == "kept" and x["action_id"] == bolt]
        ran_bolt = [x for x in records if x["action_id"] == bolt and x["producer"] == "executor"]
        ok = bool(sup) and bool(kept) and not ran_bolt and r["outcome"] == "executed" and str(lance) in sup[0]["detail"]
        return self.case("superseded command: a noop keeps it, the next command replaces it before it runs", ok,
                         action_ids=ids, queue_records=records, executed=r)

    def no_correction(self, pos: dict) -> dict:
        c = self.console
        self.restore(((pos["a"], pos["a_facing"]), (pos["b"], pos["b_facing"])))
        seq = c.seq()
        bolt = c.act(0, "frostbolt/hold/toward")[0]
        rb = self.executed(seq, bolt)
        before = c.actor(0)["state"]["position"]
        m = pos["mirror"]
        c(f"dojo harness place 1 {m[0]:.3f} {m[1]:.3f} {m[2]:.3f} {pos['b_facing']:.4f}")
        time.sleep(3.0)
        after = c.actor(0)["state"]["position"]
        records = [r for r in c.since(seq) if r["actor"] == 0]
        stock = stock_records(records)
        turned = abs(after[3] - before[3]) > 0.01
        outcome = [r for r in records if r["kind"] == "cast" and r["action_id"] == bolt]
        ok = rb["outcome"] == "executed" and not turned and not stock
        return self.case("no automatic facing or cancellation while the target moves behind a cast", ok, cast=rb,
                         orientation_before=before[3], orientation_after=after[3], cast_outcome=outcome,
                         records=records)

    def combat_maskcheck(self, pos: dict) -> dict:
        """Mask queries with combat state up: a root and its duration on one actor, the other's global cooldown, any
        proc charges a hit left, and both in combat."""
        c = self.console
        self.restore(((pos["a"], pos["a_facing"]), (pos["near"], pos["b_facing"])))
        seq = c.seq()
        nova = c.act(1, "frost_nova/hold/keep")[0]
        self.executed(seq, nova)
        lance = c.act(0, "ice_lance/hold/toward")[0]
        self.executed(seq, lance)
        states = [c.actor(s)["state"] for s in (0, 1)]
        checks = [c.maskcheck(slot, 1000) for slot in (0, 1)]
        ok = all(m["unchanged"] and m["stable"] and m["enemy_unchanged"] for m in checks) and states[0]["in_combat"]
        return self.case("1000 mask queries per actor change nothing (in combat, rooted, on the global cooldown)", ok,
                         checks=checks, states=states)

    def reactions(self) -> dict:
        """Actor 1 was attacked throughout the cases while owned and idle: nothing it did was stock."""
        records = self.console.trace()
        stock = stock_records(records)
        counters = self.console.status()["actors"]
        ok = not stock and all(a["counters"]["stock_casts"] == 0 and a["counters"]["stock_moves"] == 0
                               and a["counters"]["stock_targets"] == 0 and a["counters"]["stock_attacks"] == 0
                               for a in counters)
        return self.case("owned actors never acted on their own, attacked or not", ok, stock=stock,
                         counters=[a["counters"] for a in counters])


def run(layout: Layout, manifest: dict, log=print) -> dict:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = layout.data / "runs" / f"{stamp}-control-acceptance"
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"kind": "leeroy-dojo-control-acceptance", "format": 1, "started": stamp,
                    "instance": layout.instance, "scenario": manifest["id"]}
    root = mysql.Client(layout, layout.server.root_cnf)
    report["mysql"] = mysql.verify_identity(layout, root)
    report["live_before"] = acceptance.live_fingerprint(layout)
    if not world.running(layout):
        world.start(layout, log=log)
    report["worker_pid"] = world.pid(layout)
    names = [a["name"] for a in manifest["actors"]]
    guids = acceptance.fixture_guids(root, layout, names)
    if len(guids) != len(names):
        raise world.WorldError("the fixtures do not exist; run dojo fixture create and build first")
    r = Run(layout, manifest, log)
    c = r.connect()
    # A run starts from the staging baseline, whatever an earlier run left; the comparison covers this run only.
    c("dojo fixture restore")
    report["restore_before"] = r.wait_job()
    before = acceptance.snapshot(root, layout, guids)
    try:
        r.case("fixtures start from the staging baseline", report["restore_before"].startswith("restore ok"),
               result=report["restore_before"])
        r.catalog()
        report["open_command"] = c("dojo arena open")
        report["open_result"] = r.wait_job()
        report["arena_open"] = c("dojo status").splitlines()
        r.case("arena open with owned actors and no stock cast", report["open_result"].startswith("open ok"),
               result=report["open_result"], status=c.status())
        r.idle(20)
        r.maskcheck("preparation")
        report["in_progress"] = r.wait_in_progress()
        center = (manifest["arena"]["bounds"]["x"], manifest["arena"]["bounds"]["y"])
        positions = find_positions(c, center)
        report["positions"] = positions
        clear = positions["clear"]
        # Stock arena behaviour waits for the gates, so the hand-back is tested once they are open, face to face.
        r.restore(((clear["a"], clear["a_facing"]), (clear["b"], clear["b_facing"])))
        r.release_and_resume(0, window=30)
        r.gcd(clear)
        r.mana(clear)
        r.rooted(clear)
        r.line_of_sight(positions)
        r.interruption(clear)
        r.superseded(clear)
        r.no_correction(clear)
        r.combat_maskcheck(clear)
        r.restore(((clear["a"], clear["a_facing"]), (clear["b"], clear["b_facing"])))
        r.idle(10)
        r.reactions()
        report["close_command"] = c("dojo arena close")
        report["close_result"] = r.wait_job()
        r.case("arena close in progress: no outcome, baseline restored", report["close_result"].startswith("close ok"),
               result=report["close_result"])
        # Shutdown while owned: a second arena, a movement in flight and a command still pending.
        c("dojo arena open")
        reopen = r.wait_job()
        seq = c.seq()
        c.act(0, "none/forward/keep")
        time.sleep(0.2)
        c.act(1, "frost_nova/hold/keep")
        time.sleep(0.2)
        state = c.status()
        pending = c.act(0, "none/back/keep")[0]
        # On the same session, a tick or two later: the command is still inside its 150 ms delay.
        report["shutdown_command"] = c("server shutdown 1")
        report["before_shutdown"] = {"reopen": reopen, "status": state, "pending_action": pending, "trace_seq": seq}
    finally:
        c.close()
    wait_stopped(layout, log=log)
    logs = out_dir / "worker-logs"
    logs.mkdir(exist_ok=True)
    for name in ("Server.log", "Playerbots.log"):
        if (layout.logs / name).exists():
            shutil.copy2(layout.logs / name, logs / name)
    outs = sorted(layout.logs.glob("worldserver-2*.out"), key=lambda p: p.stat().st_mtime)
    if outs:
        shutil.copy2(outs[-1], logs / outs[-1].name)
    epochs = [a["epoch"] for a in report["before_shutdown"]["status"]["actors"]]
    report["shutdown"] = shutdown_evidence(logs / "Server.log", logs / "Playerbots.log",
                                           report["before_shutdown"]["trace_seq"], pending, epochs)
    r.case("shutdown while owned: ownership held to the end, the pending command dropped, nothing stock",
           report["shutdown"]["ok"], evidence={k: v for k, v in report["shutdown"].items() if k != "ok"})

    world.start(layout, log=log)
    c = r.connect()
    try:
        after_start = c.status()
        r.case("after a restart nothing is owned or online", all(not a["owned"] and not a["online"]
                                                                 for a in after_start["actors"]), status=after_start)
        c("dojo fixture restore")
        report["restore_result"] = r.wait_job()
        r.case("fixtures restored to the staging baseline", report["restore_result"].startswith("restore ok"),
               result=report["restore_result"])
        report["trace_after_restart"] = c.trace()
    finally:
        c.close()
    after = acceptance.snapshot(root, layout, guids)
    report["side_effects"] = acceptance.compare(before, after)
    r.case("no progression side effects", report["side_effects"]["ok"],
           progression=report["side_effects"]["progression"], bookkeeping=report["side_effects"]["bookkeeping"])
    report["worker_peers"] = acceptance.worker_peers(world.pid(layout))
    r.case("the worker talks only to the dojo MySQL instance",
           all(p.startswith("127.0.0.1:") and p.endswith(f":{layout.db_port}") for p in report["worker_peers"]),
           peers=report["worker_peers"])
    world.stop(layout, log=log)
    report["live_after"] = acceptance.live_fingerprint(layout)
    lb, la = report["live_before"], report["live_after"]
    report["live_untouched"] = (lb["configs"] == la["configs"] and lb["data"] == la["data"]
                                and {k: v for k, v in lb["processes"].items() if "dojo-runtime" not in v["path"]}
                                == {k: v for k, v in la["processes"].items() if "dojo-runtime" not in v["path"]}
                                and lb["mysql84_service"] == la["mysql84_service"])
    r.case("live install untouched", report["live_untouched"])
    report["cases"] = r.cases
    report["ok"] = all(x["ok"] for x in r.cases)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out_dir / "ra-transcript.jsonl").write_text("".join(json.dumps(t) + "\n" for t in r.transcript), encoding="utf-8")
    (out_dir / "snapshot-before.json").write_text(json.dumps(before, indent=1) + "\n", encoding="utf-8")
    (out_dir / "snapshot-after.json").write_text(json.dumps(after, indent=1) + "\n", encoding="utf-8")
    report["out"] = norm(out_dir)
    return report


def wait_stopped(layout: Layout, timeout: float = 180, log=print) -> None:
    """Waits for a shutdown asked for on the console to finish, as world.stop does after asking."""
    p = world.pid(layout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not mysql.pid_alive(p) and not mysql.port_listening(layout.ports["ra"]):
            world.clean_tmp(layout)
            log("worldserver stopped")
            return
        time.sleep(1)
    raise world.WorldError(f"worldserver pid {p} still running {timeout:.0f} s after the shutdown")


def shutdown_evidence(server_log: Path, playerbots_log: Path, since_seq: int, pending: int, epochs: list[int]) -> dict:
    """From the worker's own logs: the dojo-trace lines after the shutdown began, and the playerbots ownership lines."""
    records = []
    text = server_log.read_text(encoding="utf-8", errors="replace") if server_log.exists() else ""
    for line in text.splitlines():
        i = line.find("dojo-trace ")
        if i >= 0:
            try:
                records.append(json.loads(line[i + len("dojo-trace "):]))
            except json.JSONDecodeError:
                pass
    tail = [r for r in records if r["seq"] > since_seq]
    held = [r for r in tail if r["kind"] == "ownership" and r["outcome"] == "held"]
    dropped = [r for r in tail if r["kind"] == "command" and r["outcome"] == "dropped" and r["action_id"] == pending]
    ran = [r for r in tail if r["kind"] == "command" and r["action_id"] == pending and r["producer"] == "executor"]
    stock = stock_records(tail)
    bots = playerbots_log.read_text(encoding="utf-8", errors="replace") if playerbots_log.exists() else ""
    acquired = [l.strip() for l in bots.splitlines() if "is externally controlled by mod-dojo" in l]
    released = [l.strip() for l in bots.splitlines() if "is released by mod-dojo" in l
                and any(f"(epoch {e};" in l for e in epochs)]
    initiated = any("shutdown initiated" in r["detail"] for r in held)
    ok = initiated and not stock and not released and all(epochs) and bool(dropped) and not ran
    return {"ok": ok, "held": held, "pending_dropped": dropped, "pending_ran_before_shutdown": ran,
            "stock": stock, "records_after_open": len(tail), "playerbots_acquired": acquired,
            "playerbots_released": released, "trace_tail": tail[-40:]}
