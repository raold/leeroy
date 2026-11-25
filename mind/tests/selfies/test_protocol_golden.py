"""The request lines the mind sends the selfie worker, against the golden file the selfie suite
parses with the worker's own protocol (selfie/tests/test_protocol_golden.py). Paths in the file
are "@DIR@<name>", @DIR@ standing for an absolute folder of the platform the suite runs on."""
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import run
from leeroy_mind.selfies.worker import (QUIT, RETRY, Detail, Identity, RenderRequest,
                                        SelfieWorker, WorkerError, caption_line, critic_line,
                                        embed_line, grid_line, render_line)

from .fakes import JPEG, fake_command, request

GOLDEN = Path(__file__).resolve().parents[3] / "selfie" / "tests" / "fixtures" / "protocol_golden.json"
DIR = Path(os.path.abspath(os.sep)) / "selfies"
PLAIN = RenderRequest(
    phrases=(("photo of a woman in her thirties", 0), ("short auburn hair", 1)),
    negative=(("child, teenager", 0), ("blurry", 1)), seed=1234, steps=30, cfg=5.0, size=1024,
    out=DIR / "c0.jpg", thumb=DIR / "c0.thumb.jpg", thumb_size=512)
BUILT = {
    "render": lambda: render_line(PLAIN, "r1"),
    "render with an identity, a portrait and the detailer": lambda: render_line(replace(
        PLAIN, identity=Identity(DIR / "ref.jpg", (0.1, 0.2, 0.6, 0.7), 0.6), shape="portrait",
        detail=Detail(faces=True, hands=True)), "r2"),
    "render with the detailer on hands only": lambda: render_line(replace(
        PLAIN, size=768, cfg=7, detail=Detail(faces=False, hands=True)), "r3"),
    "grid": lambda: grid_line("g1", [DIR / f"t{i}.jpg" for i in range(4)], tile=256, gutter=8,
                              out=DIR / "guild.jpg", thumb=DIR / "guild.thumb.jpg",
                              thumb_size=256),
    "caption": lambda: caption_line("c1", DIR / "base.jpg", top="WHEN THE TANK PULLS",
                                    bottom="", out=DIR / "meme.jpg"),
    "critic": lambda: critic_line("k1", DIR / "c0.jpg"),
    "critic with a reference": lambda: critic_line("k2", DIR / "c0.jpg", ref=DIR / "ref.jpg",
                                                   crop=(0.1, 0.2, 0.6, 0.7)),
    "embed": lambda: embed_line("e1", DIR / "c0.jpg"),
    "quit": lambda: QUIT,
    "retry": lambda: RETRY,
}


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def here(line: str) -> bytes:
    return line.replace("@DIR@", json.dumps(str(DIR) + os.sep)[1:-1]).encode("utf-8") + b"\n"


def test_the_golden_holds_every_line_the_mind_builds():
    assert [case["name"] for case in golden()["requests"]] == list(BUILT)


@pytest.mark.parametrize("case", golden()["requests"], ids=lambda c: c["name"])
def test_the_mind_builds_each_golden_line(case):
    assert BUILT[case["name"]]() == here(case["line"])


def test_the_fake_worker_answers_with_the_keys_the_real_one_does(tmp_path):
    """The selfie suite holds the real worker's answers to the same keys."""
    for name in ("base.jpg", *(f"t{i}.jpg" for i in range(4))):
        (tmp_path / name).write_bytes(JPEG)
    seen: list[dict] = []

    async def go():
        command, env = fake_command(tmp_path)
        w = SelfieWorker(command, env=env)
        answer = w._answer

        async def spy(*args):
            seen.append(await answer(*args))
            return seen[-1]
        w._answer = spy
        await w.start()
        await w.render(request(tmp_path))
        await w.render(replace(request(tmp_path), detail=Detail(faces=False, hands=True)))
        await w.grid([tmp_path / f"t{i}.jpg" for i in range(4)], tile=256, gutter=8,
                     out=tmp_path / "g.jpg", thumb=tmp_path / "g.thumb.jpg", thumb_size=256)
        await w.caption(tmp_path / "base.jpg", top="TOP", bottom="", out=tmp_path / "m.jpg")
        await w.critic(tmp_path / "c0.jpg")
        await w.embed(tmp_path / "c0.jpg")
        with pytest.raises(WorkerError, match="bad_request"):
            await w.render(replace(request(tmp_path), steps=5))  # protocol.parse refuses it
        await w.stop()
    run(go())
    want = golden()["answers"]
    assert [sorted(a) for a in seen] == [
        want[k] for k in ("ready", "render", "render with the detailer", "grid", "caption",
                          "critic", "embed", "refused")]
