"""Test doubles for the selfie worker (P2): the fake worker's command, and a render request.
Every person here is invented."""
import os
import sys
from dataclasses import replace
from pathlib import Path

from leeroy_mind.selfies.worker import RenderRequest

from .fake_worker import JPEG  # noqa: F401  (the bytes the fake writes, for the tests)

FAKE_WORKER = Path(__file__).with_name("fake_worker.py")


def fake_command(tmp_path: Path, **env: str) -> tuple[list[str], dict[str, str]]:
    """The fake worker under this interpreter, isolated as the real one runs (on Windows a venv
    launcher that starts the base interpreter as its child), and its environment: this
    process's, plus the FAKE_SELFIE_* behaviour given."""
    return ([sys.executable, "-I", str(FAKE_WORKER)],
            {**os.environ, "LEEROY_SELFIE_DATA": str(tmp_path / "selfie-data"), **env})


def request(tmp_path: Path, **over) -> RenderRequest:
    req = RenderRequest(
        phrases=(("photo of a woman in her thirties", 0), ("short auburn hair", 1),
                 ("standing in a sunlit kitchen", 3)),
        negative=(("child, teenager", 0), ("blurry", 1)),
        seed=1234, steps=30, cfg=5.0, size=1024, out=tmp_path / "c0.jpg",
        thumb=tmp_path / "c0.thumb.jpg", thumb_size=512, nsfw_max=0.2)
    return replace(req, **over)
