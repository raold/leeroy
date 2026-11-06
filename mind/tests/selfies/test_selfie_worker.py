"""The mind's handle on the selfie worker (P2 Task 7): start, render and stop, with the worker's
whole process tree gone afterwards. Against the fake worker only: the live guard refuses
launching leeroy_selfie, so no real worker or model runs here."""
import asyncio
import json
import logging
import os
import sys
import time

import pytest

from conftest import run
from leeroy_mind.selfies import job
from leeroy_mind.selfies import worker as worker_module
from leeroy_mind.selfies.job import ProcessTree, pid_alive
from leeroy_mind.selfies.worker import (GRACE_S, STOP_S, Ready, Rendered, SelfieWorker,
                                        WorkerError)

from .fakes import JPEG, fake_command, request

LAUNCHER = os.name == "nt" and (os.path.normcase(sys.executable)
                                != os.path.normcase(getattr(sys, "_base_executable", "")))


async def until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.02)


def script(tmp_path, text: str) -> list[str]:
    path = tmp_path / "odd_worker.py"
    path.write_text(text, encoding="utf-8")
    return [sys.executable, "-I", str(path)]


def test_a_worker_starts_renders_and_stops(tmp_path):
    cmd, env = fake_command(tmp_path)

    async def go():
        w = SelfieWorker(cmd, env=env)
        ready = await w.start()
        up = w.running
        got = await w.render(request(tmp_path))
        await w.stop()
        return ready, up, got, w.running
    ready, up, got, running = run(go())
    assert ready == Ready(device="cuda", free_gib=20.0, versions={"fake": "1"})
    assert up is True and running is False
    assert got == Rendered(flagged=False, nsfw=0.01, prompt="photo of a woman in her thirties, "
                           "short auburn hair, standing in a sunlit kitchen", tokens=15,
                           seconds=0.01, peak_gib=10.5)
    assert (tmp_path / "c0.jpg").read_bytes() == JPEG
    assert (tmp_path / "c0.thumb.jpg").read_bytes() == JPEG


def test_an_error_answer_is_its_reason_and_the_worker_goes_on(tmp_path):
    log = tmp_path / "requests.log"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_OOM="1", FAKE_SELFIE_FLAG_SEEDS="7",
                            FAKE_SELFIE_LOG=str(log))

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        try:
            with pytest.raises(WorkerError, match="^oom$"):
                await w.render(request(tmp_path))
            after = w.running
            flagged = await w.render(request(tmp_path, seed=7), rid="101:1:0:1")
            return after, flagged
        finally:
            await w.stop()
    after, flagged = run(go())
    assert after is True
    assert flagged.flagged is True and not (tmp_path / "c0.jpg").exists()
    assert log.read_text().splitlines() == ["r1 1234", "101:1:0:1 7"]


def test_a_ready_refusal_is_its_reason(tmp_path):
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_READY_ERROR="vram", FAKE_SELFIE_PIDS=str(pids))

    async def go():
        w = SelfieWorker(cmd, env=env)
        with pytest.raises(WorkerError, match="^vram$"):
            await w.start()
        return w.running
    assert run(go()) is False
    assert not pid_alive(json.loads(pids.read_text())["pid"])


def test_a_worker_that_dies_mid_request_is_gone(tmp_path):
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_DIE="1", FAKE_SELFIE_PIDS=str(pids))

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        with pytest.raises(WorkerError, match="^gone$"):
            await w.render(request(tmp_path))
        return w.running
    assert run(go()) is False
    assert not pid_alive(json.loads(pids.read_text())["pid"])


def test_stop_kills_a_child_the_worker_started(tmp_path):
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_CHILD="1", FAKE_SELFIE_PIDS=str(pids))

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        await w.stop()
        return json.loads(pids.read_text())
    got = run(go())
    assert not pid_alive(got["pid"]) and not pid_alive(got["child"])


def test_killing_the_tree_ends_the_launcher_the_worker_and_its_child(tmp_path):
    """Review Focus 1: on Windows the venv's python.exe is a launcher whose child is the real
    interpreter; the kill must take the launcher, that child and whatever it started, with
    the worker's stdin still open (nothing but the kill ends them)."""
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_CHILD="1", FAKE_SELFIE_PIDS=str(pids))

    async def go():
        tree = await ProcessTree.spawn(cmd, env=env)
        try:
            assert json.loads(await tree.process.stdout.readline())["ok"] is True
            got = json.loads(pids.read_text())
            everyone = [tree.process.pid, got["pid"], got["child"]]
            before = [pid_alive(p) for p in everyone], tree.left()
            tree.kill()
            await until(lambda: tree.left() == 0)
            await asyncio.wait_for(tree.process.wait(), 5)
            return everyone, before, [pid_alive(p) for p in everyone]
        finally:
            tree.close()
    everyone, (alive, n), after = run(go())
    assert all(alive) and n >= (len(set(everyone)) if os.name == "nt" else 1)
    assert not any(after)
    if LAUNCHER:  # the launcher-and-child case is the one under test here
        assert everyone[0] != everyone[1]


def test_a_render_that_hangs_times_out_and_the_tree_is_stopped(tmp_path):
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_HANG="1")

    async def go():
        w = SelfieWorker(cmd, env=env, render_s=0.5)
        await w.start()
        with pytest.raises(WorkerError, match="^timeout$"):
            await w.render(request(tmp_path))
        return w.running
    assert run(go()) is False


def test_a_start_that_never_answers_times_out_and_the_tree_is_gone(tmp_path):
    pid = tmp_path / "pid"
    cmd = script(tmp_path, "import os, sys, time\n"
                           f"open({str(pid)!r}, 'w').write(str(os.getpid()))\n"
                           "time.sleep(60)\n")

    async def go():
        w = SelfieWorker(cmd, start_s=1.0)
        with pytest.raises(WorkerError, match="^timeout$"):
            await w.start()
        return w.running
    assert run(go()) is False
    assert not pid_alive(int(pid.read_text()))


ODD = """import json, sys
out = sys.stdout.buffer
out.write(json.dumps({"id": "ready", "ok": True, "device": "cuda", "free_gib": 20.0,
                      "versions": {}, "error": ""}).encode() + b"\\n")
out.flush()
sys.stdin.buffer.readline()
out.write(open(sys.argv[1], "rb").read() + b"\\n")
out.flush()
sys.stdin.buffer.readline()
"""


@pytest.mark.parametrize("answer", [
    {"id": "r1", "ok": False, "error": "x" * 70000},  # its own id, but longer than a line may be
    {"id": "someone-else", "ok": False, "error": "oom"},
    "not json",
])
def test_a_line_too_long_an_answer_for_another_request_or_noise_is_gone(tmp_path, answer):
    reply = tmp_path / "reply"
    reply.write_bytes(answer.encode() if isinstance(answer, str) else json.dumps(answer).encode())
    cmd = script(tmp_path, ODD) + [str(reply)]

    async def go():
        w = SelfieWorker(cmd)
        await w.start()
        with pytest.raises(WorkerError, match="^gone$"):
            await w.render(request(tmp_path))
        return w.running
    assert run(go()) is False


def test_stop_twice_returns_within_the_bound(tmp_path):
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_HANG="1")

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        busy = asyncio.create_task(w.render(request(tmp_path)))
        await asyncio.sleep(0.3)  # the render hangs
        t0 = time.monotonic()
        await w.stop()
        first = time.monotonic() - t0
        await w.stop()
        second = time.monotonic() - t0 - first
        with pytest.raises(WorkerError, match="^gone$"):
            await busy
        return first, second, w.running
    first, second, running = run(go())
    assert first < GRACE_S + STOP_S + 0.5 and second < 0.1 and running is False


def test_a_cancelled_render_kills_the_tree(tmp_path):
    """His login cancels the night's stage mid-render; the stage's finally then stops it."""
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_HANG="1", FAKE_SELFIE_CHILD="1",
                            FAKE_SELFIE_PIDS=str(pids))

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        busy = asyncio.create_task(w.render(request(tmp_path)))
        await asyncio.sleep(0.3)
        busy.cancel()
        with pytest.raises(asyncio.CancelledError):
            await busy
        running = w.running
        await w.stop()
        return running
    assert run(go()) is False
    got = json.loads(pids.read_text())
    assert not pid_alive(got["pid"]) and not pid_alive(got["child"])


def test_closing_stdin_ends_the_worker_at_once(tmp_path):
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_HANG="1")

    async def go():
        tree = await ProcessTree.spawn(cmd, env=env)
        try:
            p = tree.process
            assert json.loads(await p.stdout.readline())["id"] == "ready"
            p.stdin.write(json.dumps({"id": "r1", "op": "render", "seed": 1}).encode() + b"\n")
            await p.stdin.drain()
            await asyncio.sleep(0.3)
            assert p.returncode is None  # hanging in the render
            t0 = time.monotonic()
            p.stdin.close()
            await asyncio.wait_for(p.wait(), 5)
            return time.monotonic() - t0
        finally:
            tree.kill()
            tree.close()
    assert run(go()) < 2.0


def test_a_stop_during_a_start_ends_the_worker_it_was_starting(tmp_path, monkeypatch):
    """Task 10's login path may stop the worker from another task while a start is spawning
    it: when stop() returns, that worker and its child are dead, and the start says so."""
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_CHILD="1", FAKE_SELFIE_PIDS=str(pids))
    real = ProcessTree.spawn

    async def go():
        spawning = asyncio.Event()

        async def slow_spawn(command, *, env, limit):
            tree = await real(command, env=env, limit=limit)
            spawning.set()  # the stop arrives here, before start() has the tree
            await until(pids.exists)
            return tree
        monkeypatch.setattr(ProcessTree, "spawn", slow_spawn)
        w = SelfieWorker(cmd, env=env)
        starting = asyncio.create_task(w.start())
        try:
            await spawning.wait()
            await w.stop()
            await until(pids.exists)  # at once when fixed; a surviving worker writes it later
            got = json.loads(pids.read_text())
            alive = pid_alive(got["pid"]), pid_alive(got["child"])
            with pytest.raises(WorkerError, match="^stopped$"):
                await starting
            return alive, w.running
        finally:
            await w.stop()
    assert run(go()) == ((False, False), False)


def test_a_stop_while_the_worker_loads_is_a_stopped_start(tmp_path):
    pid = tmp_path / "pid"
    cmd = script(tmp_path, "import os, time\n"
                           f"open({str(pid)!r}, 'w').write(str(os.getpid()))\n"
                           "time.sleep(60)\n")  # loading, for longer than the test

    async def go():
        w = SelfieWorker(cmd)
        starting = asyncio.create_task(w.start())
        await until(pid.exists)
        await w.stop()
        alive = pid_alive(int(pid.read_text()))
        with pytest.raises(WorkerError, match="^stopped$"):
            await starting
        return alive, w.running
    assert run(go()) == (False, False)


def test_a_late_failure_after_a_restart_leaves_the_new_worker_alone(tmp_path):
    """A render still waiting on the old worker when start() replaces it fails as gone; its
    cleanup must stop only the tree it failed on, never the fresh one."""
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_HANG="1", FAKE_SELFIE_PIDS=str(pids))

    async def go():
        w = SelfieWorker(cmd, env=env)
        try:
            await w.start()
            old = json.loads(pids.read_text())["pid"]
            busy = asyncio.create_task(w.render(request(tmp_path)))
            await asyncio.sleep(0.3)  # the render hangs on the old worker
            await w.start()
            with pytest.raises(WorkerError, match="^gone$"):
                await busy
            await asyncio.sleep(0.3)
            new = json.loads(pids.read_text())["pid"]
            return old != new, pid_alive(old), pid_alive(new), w.running
        finally:
            await w.stop()
    assert run(go()) == (True, False, True, True)


windows_only = pytest.mark.skipif(os.name != "nt", reason="the job object is Windows only")


@windows_only
def test_a_kill_whose_capture_fails_still_terminates_the_job(tmp_path, monkeypatch):
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_CHILD="1", FAKE_SELFIE_PIDS=str(pids))

    async def go():
        tree = await ProcessTree.spawn(cmd, env=env)
        try:
            assert json.loads(await tree.process.stdout.readline())["ok"] is True
            got = json.loads(pids.read_text())
            everyone = [tree.process.pid, got["pid"], got["child"]]

            def broken():
                raise OSError("no list")
            monkeypatch.setattr(tree._contained, "_capture", broken)
            with pytest.raises(OSError):
                tree.kill()
            await until(lambda: not any(pid_alive(p) for p in everyone))
            return [pid_alive(p) for p in everyone]
        finally:
            monkeypatch.undo()
            tree.kill()
            tree.close()
            await asyncio.wait_for(tree.process.wait(), 5)
    assert run(go()) == [False, False, False]


@windows_only
def test_an_unknown_wait_result_counts_as_alive(tmp_path, monkeypatch, caplog):
    """WAIT_FAILED says nothing about the process, so stop() keeps waiting and warns rather
    than call the tree gone."""
    from leeroy_mind.selfies import worker as worker_module
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_PIDS=str(pids))
    kernel = job._win().kernel

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        monkeypatch.setattr(worker_module, "STOP_S", 0.3)
        monkeypatch.setattr(kernel, "WaitForSingleObject", lambda handle, ms: 0xFFFFFFFF)
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="leeroy_mind"):
            await w.stop()
        return time.monotonic() - t0
    took = run(go())
    monkeypatch.undo()
    assert "selfies_worker_left" in caplog.text and took >= 0.3
    assert not pid_alive(json.loads(pids.read_text())["pid"])


def test_a_spawn_that_cannot_contain_the_process_kills_it(tmp_path, monkeypatch):
    cmd, env = fake_command(tmp_path)
    seen = []

    def refuse(pid):
        seen.append(pid)
        raise OSError("no job")
    monkeypatch.setattr(job, "_contain", refuse)

    async def go():
        with pytest.raises(WorkerError, match="^spawn$"):
            await ProcessTree.spawn(cmd, env=env)
    run(go())
    assert len(seen) == 1 and not pid_alive(seen[0])


# -- a cancel never cuts a tree's ending short (Task 10 fix round 1) -----------------------------
# A killed tree has not exited, nor released its VRAM, until _gone says so (job.py). So each test
# cancels the caller while a tree is being ended, then asks whether _gone finished for that tree
# and whether its processes are dead once stop() returns.

def traced_gone(monkeypatch) -> list[tuple[str, int]]:
    seen = []
    real = worker_module._gone

    async def gone(tree):
        seen.append(("enter", id(tree)))
        await real(tree)
        seen.append(("done", id(tree)))
    monkeypatch.setattr(worker_module, "_gone", gone)
    return seen


def test_a_cancelled_stop_still_waits_for_the_tree_to_be_gone(tmp_path, monkeypatch):
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_CHILD="1", FAKE_SELFIE_PIDS=str(pids))
    gone = traced_gone(monkeypatch)

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        tree = id(w._tree)
        stopping = asyncio.create_task(w.stop())
        await asyncio.sleep(0)               # stop() is ending the tree
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        got = json.loads(pids.read_text())
        return tree, (pid_alive(got["pid"]), pid_alive(got["child"]))
    tree, alive = run(go())
    assert ("done", tree) in gone and alive == (False, False)


def test_a_cancel_while_a_failed_render_ends_its_tree_leaves_nothing_alive(tmp_path, monkeypatch):
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_HANG="1", FAKE_SELFIE_CHILD="1",
                            FAKE_SELFIE_PIDS=str(pids))
    gone = traced_gone(monkeypatch)

    class Cancelled(SelfieWorker):
        async def _end_failed(self, tree):    # the render timed out; his login arrives now
            asyncio.current_task().cancel()
            await super()._end_failed(tree)

    async def go():
        w = Cancelled(cmd, env=env, render_s=0.3)
        await w.start()
        tree = id(w._tree)
        with pytest.raises(asyncio.CancelledError):
            await w.render(request(tmp_path))
        await w.stop()
        got = json.loads(pids.read_text())
        return tree, (pid_alive(got["pid"]), pid_alive(got["child"]))
    tree, alive = run(go())
    assert ("done", tree) in gone and alive == (False, False)


def test_a_cancel_while_a_start_ends_the_old_tree_leaves_nothing_alive(tmp_path, monkeypatch):
    pids = tmp_path / "pids.json"
    cmd, env = fake_command(tmp_path, FAKE_SELFIE_CHILD="1", FAKE_SELFIE_PIDS=str(pids))
    gone = traced_gone(monkeypatch)

    async def go():
        w = SelfieWorker(cmd, env=env)
        await w.start()
        old = id(w._tree)
        got = json.loads(pids.read_text())
        restart = asyncio.create_task(w.start())
        await asyncio.sleep(0)               # start() is ending the old tree
        restart.cancel()
        with pytest.raises(asyncio.CancelledError):
            await restart
        await w.stop()
        return old, (pid_alive(got["pid"]), pid_alive(got["child"])), w.running
    old, alive, running = run(go())
    assert ("done", old) in gone and alive == (False, False) and running is False
