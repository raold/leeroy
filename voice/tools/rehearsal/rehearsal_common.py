"""Synthetic-only coordination for the two-interpreter regression rehearsal."""
import asyncio
import json
import os
from pathlib import Path

WORKTREE = Path(os.environ["REHEARSAL_WORKTREE"]).resolve()
RUN = Path(os.environ["REHEARSAL_RUN"]).resolve()
LIVE_PORTS = {7878, 7879, 7880, 11434, 3306}
FIRST_LINE = "The next pull is clear."
CUT_LINE = "This synthetic sentence stays long enough to test a logout during playback."


def under(path, root):
    path, root = Path(path).resolve(), Path(root).resolve()
    assert path.is_relative_to(root), (str(path), str(root))
    return path


def package_path(module, project):
    return str(under(module.__file__, WORKTREE / project))


def endpoint(port):
    assert type(port) is int and 1024 <= port <= 65535 and port not in LIVE_PORTS, port
    return port


def publish(name, **data):
    target = RUN / f"{name}.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(target)


async def until(predicate, label, timeout=25):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert not (RUN / "abort.json").exists(), f"sibling failed while waiting for {label}"
        assert asyncio.get_running_loop().time() < deadline, f"timed out: {label}"
        await asyncio.sleep(0.01)


async def stage(name):
    path = RUN / f"{name}.json"
    await until(path.exists, name)
    return json.loads(path.read_text(encoding="utf-8"))
