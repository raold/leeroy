"""The protocol from the mind's side: every request line the mind builds
(mind/tests/selfies/test_protocol_golden.py builds each one again) is one this worker parses, and
each answer this worker gives has the keys the golden file lists, which the mind's fake worker
is held to as well. Paths in the file are "@DIR@<name>": an absolute folder of this platform."""
import io
import json
import os
from pathlib import Path

import pytest

from leeroy_selfie import critic, embedding, protocol, render
from leeroy_selfie.render import Renderer
from leeroy_selfie.worker import serve

from fakes import JPEG, make_gate, make_renderer, make_torch, stub_pil

GOLDEN = Path(__file__).parent / "fixtures" / "protocol_golden.json"


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def line(name: str, folder: Path) -> bytes:
    case = next(c for c in golden()["requests"] if c["name"] == name)
    return case["line"].replace("@DIR@", json.dumps(str(folder) + os.sep)[1:-1]).encode("utf-8")


@pytest.mark.parametrize("case", golden()["requests"], ids=lambda c: c["name"])
def test_every_line_the_mind_builds_parses(case, tmp_path):
    raw = line(case["name"], tmp_path)
    req = protocol.parse(raw)
    assert req.id == json.loads(raw)["id"]


def answers(renderer, *names: str, folder: Path) -> list[dict]:
    out = io.BytesIO()
    data = b"".join(line(n, folder) + b"\n" for n in names)
    assert serve(lambda: renderer, stdin=io.BytesIO(data), out=out, on_eof=lambda: None) == 0
    return [json.loads(x) for x in out.getvalue().splitlines()]


def keys(answer: dict) -> list[str]:
    assert answer["ok"] is True, answer
    return sorted(answer)


def test_the_ready_answer_has_the_golden_keys(tmp_path, monkeypatch):
    from test_render import fake_stack
    fake_stack(monkeypatch, make_torch())
    renderer = render.load(tmp_path, min_free_gib=8)
    assert keys(answers(renderer, folder=tmp_path)[0]) == golden()["answers"]["ready"]


def test_render_and_grid_answers_have_the_golden_keys(tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    for i in range(4):
        (tmp_path / f"t{i}.jpg").write_bytes(JPEG)
    got = answers(make_renderer(), "render", "grid", folder=tmp_path)
    want = golden()["answers"]
    assert [keys(a) for a in got[1:]] == [want["render"], want["grid"]]


def test_a_detailed_render_answer_has_the_golden_keys(tmp_path, monkeypatch):
    pytest.importorskip("numpy")  # the detailer's pass
    stub_pil(monkeypatch)
    got = answers(make_renderer(), "render with the detailer on hands only", folder=tmp_path)
    assert keys(got[1]) == golden()["answers"]["render with the detailer"]


def test_caption_critic_and_embed_answers_have_the_golden_keys(tmp_path, monkeypatch):
    Image = pytest.importorskip("PIL.Image")
    pytest.importorskip("numpy")
    for name in ("base.jpg", "c0.jpg"):
        Image.new("RGB", (512, 512), (40, 90, 60)).save(tmp_path / name, format="JPEG")

    class Plain:
        info, identity, gate = {}, False, make_gate(nsfw=0.01)
        caption = Renderer.caption

        def critic(self, req):
            return critic.answer(critic.load_detectors(tmp_path), req, None)

    want = golden()["answers"]
    got = answers(Plain(), "caption", "critic", folder=tmp_path)
    assert [keys(a) for a in got[1:]] == [want["caption"], want["critic"]]
    from test_embedding import engine
    engine(monkeypatch)  # stubs the JPEG reader too: after the caption and the critic
    encoder = embedding.load(tmp_path, min_free_gib=11.5)
    assert keys(answers(encoder, "embed", folder=tmp_path)[1]) == want["embed"]


def test_a_refused_line_is_answered_with_the_golden_keys(tmp_path):
    out = io.BytesIO()
    serve(lambda: make_renderer(), stdin=io.BytesIO(b'{"id": "x1", "op": "render"}\n'), out=out,
          on_eof=lambda: None)
    refused = json.loads(out.getvalue().splitlines()[1])
    assert refused == {"id": "x1", "ok": False, "error": "bad_request"}
    assert sorted(refused) == golden()["answers"]["refused"]
