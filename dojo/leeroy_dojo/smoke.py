"""python -m leeroy_dojo protocol-smoke --fake: the whole contract end to end against the in-process fake worker.

It plays three episodes over loopback and records them: A with a step delivered twice and a result replayed from the
cache, B cut by dropping the connection right after a step was sent (committed, its result never read), and C on a
new session with A's seed and policies. Then it checks that the world advanced exactly once per decision, that C
replays A exactly, that B is aborted and excluded, and that the recording checks catch a missing final chunk and a
crash before the footer. Everything it writes stays under one output directory.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import __version__, fake_game
from .client import DojoClient, DojoError
from .fake_worker import FakeWorker
from .protocol import encode
from .recording import EpisodeRecorder, decision_record, load_transitions, verify_episode

POLICY_NAMES = ("caster", "lancer")


@dataclass
class SmokeReport:
    out: Path
    checks: list[dict[str, str]] = field(default_factory=list)
    decisions: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(c["status"] == "ok" for c in self.checks)

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append({"status": "ok" if ok else "FAIL", "check": name, "detail": detail})
        return ok

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "out": str(self.out), "decisions": self.decisions, "checks": self.checks}


def _header(hello: dict[str, Any], reset: dict[str, Any]) -> dict[str, Any]:
    return {
        "session": hello["session"], "protocol": hello["protocol"], "worker": hello["worker"],
        "scenario": reset["scenario"], "scenario_seed": reset["scenario_seed"], "generation": reset["generation"],
        "origin_sim_ms": reset["sim_ms"], "schemas": hello["schemas"], "actors": hello["actors"],
        "actions": hello["actions"], "clock": hello["clock"], "rng": hello["rng"], "bounds": hello["bounds"],
        "client": {"package": "leeroy_dojo", "version": __version__},
        "policies": [{"actor": slot, "kind": "scripted", "name": name} for slot, name in enumerate(POLICY_NAMES)],
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _choose(observations: list[dict[str, Any]]) -> list[int]:
    policies: list[Callable[[list[float], list[int]], int]] = [fake_game.POLICIES[n] for n in POLICY_NAMES]
    return [policies[o["actor"]](o["obs"], o["mask"]) for o in observations]


def _answer(client: DojoClient, rid: str) -> dict[str, Any]:
    while True:
        answer = client.read_message(client.timeout)
        if answer is None:
            raise TimeoutError(f"no answer to {rid}")
        if answer.get("id") == rid:
            return answer


def play(client: DojoClient, recordings: Path, episode: str, seed: int, *, chunk_records: int,
         duplicate_at: int | None = None, replay_at: int | None = None, stop_after: int | None = None,
         report: SmokeReport | None = None) -> tuple[EpisodeRecorder, list[dict[str, Any]]]:
    """Resets and plays scripted policies until the episode ends, or until stop_after decisions.

    Returns the recorder and the observations the next decision would answer.
    """
    reset = client.reset(episode, seed)
    recorder = EpisodeRecorder(recordings, episode, _header(client.hello_result, reset), chunk_records=chunk_records)
    observations = reset["observations"]
    while stop_after is None or recorder.transitions < stop_after:
        message = client.step_message(_choose(observations))
        decision = message["decision"]
        if decision == duplicate_at:
            line = encode(message)
            client.send_line(line)
            client.send_line(line)
            first, second = _answer(client, message["id"]), _answer(client, message["id"])
            if report is not None:
                report.check("duplicate delivery", first == second and first["type"] == "step_result",
                             f"{episode} decision {decision} sent twice, answered twice identically")
            client.track(first)
            result = first
        else:
            result = client.request(message)
        if decision == replay_at:
            client.send_line(encode(message))
            replayed = _answer(client, message["id"])
            if report is not None:
                report.check("cached replay", replayed == result,
                             f"{episode} decision {decision} resent after its result, answered from the cache")
        recorder.add(decision_record(observations, result, reset["sim_ms"]))
        observations = result["observations"]
        if result["terminated"] or result["truncated"]:
            outcome = result["outcome"]
            recorder.finish("truncated" if result["truncated"] else "terminated", reason=outcome["reason"],
                            winner=outcome["winner"], decisions=recorder.transitions)
            break
    return recorder, observations


def _replay_view(path: Path) -> list[dict[str, Any]]:
    return [{k: v for k, v in record.items() if k != "request_id"} for record in load_transitions(path)]


def run_fake_smoke(out: Path | None = None, *, seed: int = 7, chunk_records: int = 8,
                   time_limit_ms: int = fake_game.TIME_LIMIT_MS) -> SmokeReport:
    root = Path(out) if out is not None else Path(tempfile.mkdtemp(prefix="dojo-smoke-"))
    root.mkdir(parents=True, exist_ok=True)
    recordings = root / "episodes"
    if recordings.exists() and any(recordings.iterdir()):
        raise FileExistsError(f"{recordings} is not empty")
    report = SmokeReport(root)
    worker = FakeWorker(time_limit_ms=time_limit_ms)
    port = worker.start()
    worker.write_receipt(root / "receipt.json")
    expect = {"observation_schema": "dojo.fake.obs/1", "action_schema": "dojo.fake.act/1", "scenario": "fake_duel_v1"}
    try:
        client = DojoClient(timeout=5.0)
        client.connect("127.0.0.1", port)
        hello = client.hello(expect)
        report.check("hello", hello["scenario"] == "fake_duel_v1" and hello["rng"]["combat_replay"] is True,
                     f"session {hello['session']}, protocol {hello['protocol']}, {len(hello['actions'])} actions")

        a, _ = play(client, recordings, "smoke-a", seed, chunk_records=chunk_records, duplicate_at=3, replay_at=5,
                 report=report)
        report.check("episode a", a.finished, f"{a.transitions} decisions in {len(a.chunks)} chunks")

        b, last = play(client, recordings, "smoke-b", seed + 1, chunk_records=chunk_records, stop_after=4)
        old_session = client.session
        unread = client.step_message(_choose(last))
        client.send_line(encode(unread))
        client.disconnect()
        b.finish("aborted", reason="connection lost after a committed step", decisions=b.transitions)
        aborted = _wait(lambda: ("smoke-b", "disconnect") in worker.aborted)
        report.check("forced reconnect", aborted, "the worker aborted smoke-b when the connection dropped")

        client.connect("127.0.0.1", port)
        hello2 = client.hello(expect)
        report.check("new session", hello2["session"] != old_session and hello2["state"] == "ready",
                     f"{hello2['session']} replaces {old_session}; state {hello2['state']}")
        stale = dict(unread, id=client.next_id("s"), session=old_session)
        try:
            client.request(stale)
            refused = ""
        except DojoError as e:
            refused = e.code or ""
        report.check("old session refused", refused == "bad_session", f"a step on the old session got {refused!r}")

        c, _ = play(client, recordings, "smoke-c", seed, chunk_records=chunk_records)
        report.check("episode c", c.finished, f"{c.transitions} decisions in {len(c.chunks)} chunks")
        client.close("smoke done")
        report.check("close", _wait(lambda: worker.lifecycle.state == "ready"), "the worker is ready again")
    finally:
        worker.stop()

    applied = dict(worker.applied)
    report.check("exactly once", all(n == 1 for n in applied.values()),
                 f"{len(applied)} decisions applied, none twice")
    for name, recorder, expected in (("smoke-a", a, None), ("smoke-c", c, None), ("smoke-b", b, 5)):
        done = sorted(d for (e, d) in applied if e == name)
        want = list(range(expected if expected is not None else recorder.transitions))
        report.decisions[name] = len(done)
        report.check(f"applied {name}", done == want, f"world advanced for decisions 0..{len(done) - 1}")

    checks = {name: verify_episode(recordings / name) for name in ("smoke-a", "smoke-b", "smoke-c")}
    report.check("recording a", checks["smoke-a"].usable, f"{checks['smoke-a'].transitions} records verified")
    report.check("recording c", checks["smoke-c"].usable, f"{checks['smoke-c'].transitions} records verified")
    report.check("recording b excluded", checks["smoke-b"].complete and not checks["smoke-b"].usable,
                 f"aborted after {checks['smoke-b'].transitions} records, kept out of the learning stream")
    report.check("deterministic replay", _replay_view(recordings / "smoke-a") == _replay_view(recordings / "smoke-c"),
                 "smoke-c repeats smoke-a decision for decision")

    probes = root / "probes"
    missing = probes / "missing-final-chunk"
    shutil.copytree(recordings / "smoke-a", missing)
    last = max(missing.glob("chunk-*.jsonl"))
    last.unlink()
    found = verify_episode(missing)
    report.check("missing final chunk", not found.usable and any("missing" in p for p in found.problems),
                 "; ".join(found.problems))
    crashed = probes / "crash-before-footer"
    shutil.copytree(recordings / "smoke-a", crashed)
    (crashed / "footer.json").unlink()
    max(crashed.glob("chunk-*.jsonl")).unlink()
    found = verify_episode(crashed)
    report.check("crash before footer", not found.complete and found.valid_chunks > 0,
                 f"{found.valid_chunks} intact chunks, excluded: " + "; ".join(found.problems))
    return report


def _wait(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()
