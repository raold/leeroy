import io
import json
import os
import subprocess
import sys
import threading

import pytest

from leeroy_selfie.fit import PromptTooLong
from leeroy_selfie.protocol import RenderFailed
from leeroy_selfie.render import ReadyError
from leeroy_selfie.worker import serve

INFO = {"device": "cuda", "free_gib": 13.1, "versions": {"torch": "t", "diffusers": "d"}}


class Fake:
    info = INFO

    def __init__(self, exc=None, identity=False):
        self.exc, self.seen, self.identity = exc, [], identity

    def render(self, req):
        self.seen.append(req.id)
        if self.exc and len(self.seen) == 1:
            raise self.exc
        return {"flagged": False, "nsfw": 0.004, "prompt": "p", "dropped": [],
                "tokens": 3, "seconds": 0.1, "peak_gib": 1.0, "width": 768, "height": 768}

    def grid(self, req):
        self.seen.append(req.id)
        if self.exc and len(self.seen) == 1:
            raise self.exc
        if not all(os.path.isfile(t) for t in req.tiles):
            return {"ok": False, "error": "missing_tile"}
        return {"ok": True, "width": 256, "height": 256, "seconds": 0.1, "error": ""}


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
    assert lines[0] == {"id": "ready", "ok": True, "error": "", "identity": False, **INFO}
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


def test_bad_lines_and_quit_are_answered_without_the_renderer(tmp_path):
    data = (b"nonsense\n" + b"x" * 70000 + b"\n" +
            b'{"id": "q", "op": "quit"}\n' + req(tmp_path, "late"))
    fake = Fake()
    code, lines = run(lambda: fake, data)
    assert code == 0 and fake.seen == []
    assert [x.get("error") for x in lines[1:]] == ["bad_request", "bad_request"]


def with_identity(tmp_path, rid="r1"):
    d = json.loads(req(tmp_path, rid))
    d["identity"] = {"ref": str(tmp_path / "ref.jpg"), "crop": [0, 0, 1, 1], "scale": 0.6}
    return json.dumps(d).encode() + b"\n"


def run_identity(load, data):
    out = io.BytesIO()
    code = serve(load, stdin=io.BytesIO(data), out=out, on_eof=lambda: None, identity=True)
    return code, [json.loads(x) for x in out.getvalue().splitlines()]


def test_a_plain_worker_refuses_an_identity_reference(tmp_path):
    fake = Fake()
    code, lines = run(lambda: fake, with_identity(tmp_path, "a") + req(tmp_path, "b"))
    assert code == 0 and fake.seen == ["b"]
    assert lines[1] == {"id": "a", "ok": False, "error": "identity_unavailable"}
    assert lines[2]["ok"] is True


def test_an_identity_worker_refuses_a_render_without_one(tmp_path):
    fake = Fake(identity=True)
    _, lines = run_identity(lambda: fake, req(tmp_path, "a") + with_identity(tmp_path, "b"))
    assert lines[1] == {"id": "a", "ok": False, "error": "bad_request"}
    assert lines[2]["ok"] is True and fake.seen == ["b"]


def test_ready_says_identity_only_for_a_renderer_that_loaded_the_adapter(tmp_path):
    assert run(lambda: Fake(), b"")[1][0]["identity"] is False
    assert run_identity(lambda: Fake(identity=True), b"")[1][0]["identity"] is True
    # an --identity start whose renderer has no adapter is never ready, so nothing is ignored
    fake = Fake()
    code, lines = run_identity(lambda: fake, with_identity(tmp_path, "a"))
    assert code == 1 and fake.seen == []
    assert lines == [{"id": "ready", "ok": False, "error": "model"}]


def grid_line(tmp_path, name, rid):
    return json.dumps({"id": rid, "op": "grid", "tiles": [str(tmp_path / name)],
                       "tile": 256, "gutter": 0, "out": str(tmp_path / "g.jpg"),
                       "thumb": str(tmp_path / "gt.jpg"), "thumb_size": 128}).encode() + b"\n"


def test_a_grid_answers_missing_tile_or_its_compositor_result(tmp_path):
    (tmp_path / "t.jpg").write_bytes(b"jpg")
    data = grid_line(tmp_path, "nope.jpg", "a") + grid_line(tmp_path, "t.jpg", "b")
    for identity in (False, True):  # either worker composites
        fake = Fake(identity=identity)
        lines = (run_identity if identity else run)(lambda: fake, data)[1]
        assert fake.seen == ["a", "b"]
        assert lines[1] == {"id": "a", "ok": False, "error": "missing_tile"}
        assert lines[2] == {"id": "b", "ok": True, "width": 256, "height": 256, "seconds": 0.1,
                            "error": ""}


def test_a_grid_exception_answers_its_type_and_the_worker_goes_on(tmp_path):
    (tmp_path / "t.jpg").write_bytes(b"jpg")
    fake = Fake(exc=MemoryError("big"))
    data = grid_line(tmp_path, "t.jpg", "a") + grid_line(tmp_path, "t.jpg", "b")
    _, lines = run(lambda: fake, data)
    assert lines[1] == {"id": "a", "ok": False, "error": "MemoryError"}
    assert lines[2]["ok"] is True and lines[2]["id"] == "b"


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


def test_the_claimed_stdin_reads_the_pipe_and_fd_0_is_the_null_device():
    script = (
        "import os, stat\n"
        "from leeroy_selfie import worker\n"
        "inp = worker.claim_stdin()\n"
        "print(stat.S_ISCHR(os.fstat(0).st_mode), inp.readline().decode().strip(), flush=True)\n")
    p = subprocess.run([sys.executable, "-c", script], input=b"hello\n", capture_output=True,
                       timeout=60)
    assert p.stdout.split() == [b"True", b"hello"], p.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="the pipe waits only on Windows")
def test_numpy_imports_while_the_reader_waits_on_the_pipe():
    pytest.importorskip("numpy")
    script = (
        "import os, threading, time\n"
        "from leeroy_selfie import worker\n"
        "inp = worker.claim_stdin()\n"
        "threading.Thread(target=inp.readline, daemon=True).start()\n"
        "time.sleep(0.5)\n"
        "import numpy\n"
        "print('imported', flush=True)\n"
        "os._exit(0)\n")
    p = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert p.wait(timeout=60) == 0  # the pipe stays open: the reader is still waiting
        assert p.stdout.read().strip() == b"imported"
    finally:
        p.kill()
        p.stdin.close()
        p.stdout.close()
        p.stderr.close()


CHILD = (  # argv: mode (fail, quit or plain: a load that ignores identity), then worker flags
    "import sys\n"
    "from leeroy_selfie import render\n"
    "class R:\n"
    "    info = {'device': 'cuda'}\n"
    "    def __init__(self, identity): self.identity = identity\n"
    "    def render(self, req): return {}\n"
    "def load(data, *, min_free_gib, device='cuda', identity=False):\n"
    "    if sys.argv[1] == 'fail': raise render.ReadyError('cuda')\n"
    "    return R(identity and sys.argv[1] != 'plain')\n"
    "render.load = load\n"
    "from leeroy_selfie.__main__ import main\n"
    "sys.exit(main(['worker', '--min-free-gib', '1', '--data', '.', *sys.argv[2:]]))\n")


def first_line_and_exit(mode, *flags):
    """The child's ready line and exit code, stdin held open (its end would exit 0 at once)."""
    p = subprocess.Popen([sys.executable, "-c", CHILD, mode, *flags], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        line = p.stdout.readline()
        if b'"ok": true' in line:
            p.stdin.write(b'{"id": "q", "op": "quit"}\n')
            p.stdin.flush()
        return json.loads(line), p.wait(timeout=20)
    finally:
        p.kill()
        p.stdin.close()
        p.stdout.close()
        p.stderr.close()


def test_worker_identity_loads_the_adapter_and_ready_says_so_only_when_it_did():
    ready, code = first_line_and_exit("quit", "--identity")
    assert ready["ok"] is True and ready["identity"] is True and code == 0
    ready, code = first_line_and_exit("plain", "--identity")  # a load that left it out
    assert ready == {"id": "ready", "ok": False, "error": "model"} and code == 1
    ready, code = first_line_and_exit("quit")
    assert ready["ok"] is True and ready["identity"] is False and code == 0


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


# -- a vram refusal at the start waits for the mind's retry (final review I2) -------------------

RETRY = b'{"id": "ready", "op": "retry"}\n'


def retrying(times: int, fields=None):
    """A load refused for VRAM `times` times; each retry is counted in `tries`."""
    tries = []

    def attempt():
        tries.append(1)
        if len(tries) <= times:
            raise ReadyError("vram", retry=attempt, fields=fields or {})
        return Fake()

    return attempt, tries


def test_a_vram_refusal_waits_for_a_retry_and_then_serves(tmp_path):
    load, tries = retrying(2, {"check_ms": 5, "load_ms": 7, "free_gib": 9.5})
    code, lines = run(load, RETRY + RETRY + req(tmp_path, "a"))
    assert code == 0 and len(tries) == 3
    refused = {"id": "ready", "ok": False, "error": "vram", "retry": True, "check_ms": 5,
               "load_ms": 7, "free_gib": 9.5}
    assert lines[:2] == [refused, refused]
    assert lines[2] == {"id": "ready", "ok": True, "error": "", "identity": False, **INFO}
    assert lines[3]["id"] == "a" and lines[3]["ok"] is True


def test_while_it_waits_a_quit_or_the_end_of_stdin_ends_it_and_other_lines_are_bad(tmp_path):
    load, tries = retrying(1)
    code, lines = run(load, req(tmp_path, "early") + b"nonsense\n" + b'{"id": "q", "op": "quit"}\n'
                      + RETRY)
    assert code == 1 and len(tries) == 1
    assert [(x["id"], x.get("error")) for x in lines[1:]] == [("early", "bad_request"),
                                                               ("", "bad_request")]
    load, tries = retrying(1)
    eofs = []
    assert run(load, b"", eofs)[0] == 1 and eofs == [1] and len(tries) == 1


def test_a_final_refusal_says_no_retry_and_a_retry_once_serving_is_bad(tmp_path):
    def final():
        raise ReadyError("model", fields={"check_ms": 3, "load_ms": 0})
    assert run(final, RETRY) == (1, [{"id": "ready", "ok": False, "error": "model",
                                      "check_ms": 3, "load_ms": 0}])
    code, lines = run(lambda: Fake(), RETRY)
    assert code == 0 and lines[1] == {"id": "ready", "ok": False, "error": "bad_request"}
