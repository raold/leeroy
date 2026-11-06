import io
import json
import subprocess
import sys

from leeroy_selfie.fit import PromptTooLong
from leeroy_selfie.protocol import RenderFailed
from leeroy_selfie.render import ReadyError
from leeroy_selfie.worker import serve

INFO = {"device": "cuda", "free_gib": 13.1, "versions": {"torch": "t", "diffusers": "d"}}


class Fake:
    info = INFO

    def __init__(self, flagged=False, exc=None):
        self.flagged, self.exc, self.seen = flagged, exc, []

    def render(self, req):
        self.seen.append(req.id)
        if self.exc and len(self.seen) == 1:
            raise self.exc
        if not self.flagged:
            open(req.out, "wb").write(b"jpg")
        return {"flagged": self.flagged, "nsfw": 0.004, "prompt": "p", "dropped": [],
                "tokens": 3, "seconds": 0.1, "peak_gib": 1.0}


def req(tmp_path, rid="r1"):
    return json.dumps({"id": rid, "op": "render", "phrases": [["a", 0]], "negative": [],
                       "seed": 1, "steps": 10, "cfg": 5, "size": 768,
                       "out": str(tmp_path / f"{rid}.jpg"), "thumb": str(tmp_path / "t.jpg"),
                       "thumb_size": 128, "nsfw_max": 0.2}).encode() + b"\n"


def run(load, data, eofs=None):
    out = io.BytesIO()
    code = serve(load, stdin=io.BytesIO(data), out=out,
                 on_eof=(lambda: eofs.append(1)) if eofs is not None else (lambda: None))
    return code, [json.loads(x) for x in out.getvalue().splitlines()]


def test_serve_answers_ready_then_each_request_and_writes_nothing_when_flagged(tmp_path):
    fake = Fake()
    code, lines = run(lambda: fake, req(tmp_path, "a") + req(tmp_path, "b"))
    assert code == 0
    assert lines[0] == {"id": "ready", "ok": True, "error": "", **INFO}
    assert [(x["id"], x["ok"], x["flagged"]) for x in lines[1:]] == [("a", True, False),
                                                                      ("b", True, False)]
    assert (tmp_path / "a.jpg").exists()
    flagged = Fake(flagged=True)
    _, lines = run(lambda: flagged, req(tmp_path, "c"))
    assert lines[1]["flagged"] is True and lines[1]["ok"] is True
    assert not (tmp_path / "c.jpg").exists()


def test_a_failed_load_answers_ready_false_with_its_reason_only():
    def bad():
        raise ReadyError("vram")

    def boom():
        raise KeyError("x")

    assert run(bad, b"") == (1, [{"id": "ready", "ok": False, "error": "vram"}])
    assert run(boom, b"") == (1, [{"id": "ready", "ok": False, "error": "KeyError"}])


def test_a_render_exception_answers_its_type_and_the_worker_goes_on(tmp_path):
    for exc, name in ((ValueError("secret prompt"), "ValueError"), (RenderFailed("oom"), "oom"),
                      (PromptTooLong("x"), "prompt_too_long")):
        fake = Fake(exc=exc)
        _, lines = run(lambda: fake, req(tmp_path, "a") + req(tmp_path, "b"))
        assert lines[1] == {"id": "a", "ok": False, "error": name}
        assert lines[2]["ok"] is True and lines[2]["id"] == "b"


def test_bad_lines_identity_and_quit_are_answered_without_the_renderer(tmp_path):
    ident = json.loads(req(tmp_path))
    ident["identity"] = "x"
    data = (b"nonsense\n" + json.dumps(ident).encode() + b"\n" + b"x" * 70000 + b"\n" +
            b'{"id": "q", "op": "quit"}\n' + req(tmp_path, "late"))
    fake = Fake()
    code, lines = run(lambda: fake, data)
    assert code == 0 and fake.seen == []
    assert [x.get("error") for x in lines[1:]] == ["bad_request", "identity_unavailable",
                                                   "bad_request"]
    assert lines[2]["id"] == "r1"


def test_the_end_of_stdin_calls_on_eof_at_once():
    eofs = []
    code, lines = run(lambda: Fake(), b"", eofs)
    assert code == 0 and eofs == [1] and len(lines) == 1


def test_a_renderer_that_prints_cannot_corrupt_the_protocol(tmp_path):
    script = (
        "import sys, os, io\n"
        "from leeroy_selfie import worker\n"
        "out = worker.claim_stdout()\n"
        "print('noise from a library')\n"
        "os.write(1, b'raw noise\\n')\n"
        "out.write(b'{\"id\": \"ready\"}\\n'); out.flush()\n")
    p = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60)
    assert p.stdout == b'{"id": "ready"}\n'
    assert b"noise from a library" in p.stderr and b"raw noise" in p.stderr
