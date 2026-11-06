import io
import json
import subprocess
import sys
import threading

from leeroy_selfie.fit import PromptTooLong
from leeroy_selfie.protocol import RenderFailed
from leeroy_selfie.render import ReadyError
from leeroy_selfie.worker import serve

INFO = {"device": "cuda", "free_gib": 13.1, "versions": {"torch": "t", "diffusers": "d"}}


class Fake:
    info = INFO

    def __init__(self, exc=None):
        self.exc, self.seen = exc, []

    def render(self, req):
        self.seen.append(req.id)
        if self.exc and len(self.seen) == 1:
            raise self.exc
        return {"flagged": False, "nsfw": 0.004, "prompt": "p", "dropped": [],
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


def test_serve_answers_ready_then_each_request(tmp_path):
    fake = Fake()
    code, lines = run(lambda: fake, req(tmp_path, "a") + req(tmp_path, "b"))
    assert code == 0
    assert lines[0] == {"id": "ready", "ok": True, "error": "", **INFO}
    assert [(x["id"], x["ok"], x["flagged"], x["error"]) for x in lines[1:]] == [
        ("a", True, False, ""), ("b", True, False, "")]
    assert fake.seen == ["a", "b"]


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


def test_the_end_of_stdin_calls_on_eof():
    eofs = []
    code, lines = run(lambda: Fake(), b"", eofs)
    assert code == 0 and eofs == [1] and len(lines) == 1


def test_eof_is_handled_during_the_load():
    eof = threading.Event()
    seen = []

    def load():
        seen.append(eof.wait(5))  # only the reader can set it, so it runs before load ends
        return Fake()

    serve(load, stdin=io.BytesIO(b""), out=io.BytesIO(), on_eof=eof.set)
    assert seen == [True]


def test_eof_is_handled_in_the_middle_of_a_render(tmp_path):
    eof = threading.Event()
    seen = []

    class Slow(Fake):
        def render(self, req):
            seen.append(eof.wait(5))
            return super().render(req)

    serve(lambda: Slow(), stdin=io.BytesIO(req(tmp_path)), out=io.BytesIO(), on_eof=eof.set)
    assert seen == [True]


def test_a_reader_that_fails_still_ends_the_worker():
    class Broken(io.BytesIO):
        def readline(self, *a):
            raise OSError("pipe broke")

    eofs = []
    done = []
    t = threading.Thread(target=lambda: done.append(serve(
        lambda: Fake(), stdin=Broken(), out=io.BytesIO(), on_eof=lambda: eofs.append(1))))
    t.start()
    t.join(10)
    assert not t.is_alive() and done == [0] and eofs == [1]


def test_a_lone_surrogate_id_and_deep_nesting_are_bad_requests_and_the_worker_goes_on(
        tmp_path):
    data = b'{"id": "\\ud800", "op": "nope"}\n' + b"[" * 5000 + b"\n" + req(tmp_path, "ok")
    code, lines = run(lambda: Fake(), data)
    assert code == 0
    assert [(x["id"], x["ok"]) for x in lines[1:]] == [("", False), ("", False), ("ok", True)]
    assert lines[1]["error"] == lines[2]["error"] == "bad_request"


def test_an_answer_with_a_lone_surrogate_is_still_sent(tmp_path):
    class Odd(Fake):
        def render(self, req):
            return {**super().render(req), "prompt": "bad \ud800 text"}

    _, lines = run(lambda: Odd(), req(tmp_path))
    assert lines[1]["ok"] is True and lines[1]["prompt"] == "bad \ud800 text"


def test_a_renderer_that_prints_cannot_corrupt_the_protocol():
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


CHILD = (
    "import sys\n"
    "from leeroy_selfie import render\n"
    "class R:\n"
    "    info = {'device': 'cuda'}\n"
    "    def render(self, req): return {}\n"
    "def load(data, *, min_free_gib, device='cuda'):\n"
    "    if sys.argv[1] == 'fail': raise render.ReadyError('cuda')\n"
    "    return R()\n"
    "render.load = load\n"
    "from leeroy_selfie.__main__ import main\n"
    "sys.exit(main(['worker', '--min-free-gib', '1', '--data', '.']))\n")


def test_a_normal_exit_with_stdin_still_open_is_clean():
    for mode, answer, code in (("fail", b'"ok": false', 1), ("quit", b'"ok": true', 0)):
        p = subprocess.Popen([sys.executable, "-c", CHILD, mode], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if mode == "quit":
                p.stdin.write(b'{"id": "q", "op": "quit"}\n')
                p.stdin.flush()
            assert answer in p.stdout.readline()
            assert p.wait(timeout=20) == code  # stdin is still held open here
            assert b"Fatal Python error" not in p.stderr.read()
        finally:
            p.kill()
            p.stdin.close()
            p.stdout.close()
            p.stderr.close()
