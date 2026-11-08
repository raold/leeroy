"""Adversarial console checks: free loopback ports, synthetic data and fake processes only."""
import asyncio
import os
from contextlib import suppress
from pathlib import Path

import pytest

from conftest import run
from console.rig import KEY, http, make_app
from console.test_console_server import served
from leeroy_mind.console import httpd

GET_ROUTES = ("state", "events", "health", "feed", "raid", "guild", "social", "realm", "calendar",
              "board", "bank", "training", "dojo", "library", "personas", "persona", "face", "tuning",
              "selfies", "selfies/images", "selfies/member", "selfies/gallery", "selfies/photo")
POST_ROUTES = ("tune", "selfie", "voice", "control", "raid", "say", "board")


def test_every_route_and_method_keeps_the_same_authority_boundary(tmp_path):
    async def scenario():
        async with served(make_app(tmp_path, allow_hosts=("pc.example.ts.net",))) as app:
            for path in ("/", "/static/js/core.mjs", *("/api/" + name for name in GET_ROUTES)):
                for method in ("GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE"):
                    assert (await http(app, method, path, host="wrong.invalid", body={})).status == 403
                    assert (await http(app, method, path, origin="https://wrong.invalid", body={})).status == 403
                if path.startswith("/api/"):
                    assert (await http(app, "GET", path, key=False)).status == 403
                for method in ("HEAD", "OPTIONS", "PUT", "DELETE"):
                    assert (await http(app, method, path)).status == 405
            for name in POST_ROUTES:
                path = "/api/" + name
                assert (await http(app, "POST", path, body={}, origin=True, key=False)).status == 403
                assert (await http(app, "POST", path, body={})).status == 403
                assert (await http(app, "POST", path, body={}, origin=True,
                                   headers={"Origin": app.origin})).status == 403
            for name in set(POST_ROUTES) - set(GET_ROUTES):
                assert (await http(app, "GET", "/api/" + name)).status == 405
            assert (await http(app, "GET", "/api/state", host="pc.example.ts.net", origin=app.origin)).status == 403
            assert (await http(app, "GET", "/api/state", host="wrong.invalid",
                               headers={"X-Forwarded-Host": f"127.0.0.1:{app.s.port}"})).status == 403
            assert app.mind.acts == []
    run(scenario())


def test_static_and_image_ids_never_become_paths(tmp_path):
    async def scenario():
        async with served(make_app(tmp_path)) as app:
            for value in ("..", "%2e%2e", "..%2f..", "..\\..", "C:secret", "C%3asecret",
                          "\\\\host\\share", "file.jpg:secret", "%00", "%2fetc%2fpasswd"):
                assert (await http(app, "GET", "/static/" + value, key=False)).status == 404
                assert (await http(app, "GET", "/api/face?guid=" + value + "&size=sm")).status == 400
                assert (await http(app, "GET", "/api/selfies/photo?guid=101&kind=snap&id=" + value)).status == 400
            page = await http(app, "GET", "/", key=False)
            csp = page.header("content-security-policy")
            assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
            assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
            assert page.header("x-content-type-options") == "nosniff"
            assert page.header("cache-control") == "no-store"
            assert page.header("set-cookie") is None and KEY.encode() not in page.body
    run(scenario())


def test_body_header_and_slow_request_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(httpd, "READ_S", .1)
    async def scenario():
        async with served(make_app(tmp_path)) as app:
            for header, expected in [(f"Content-Length: {httpd.MAX_BODY + 1}", 413),
                                     ("Content-Length: 1\r\nContent-Length: 1", 400),
                                     ("Transfer-Encoding: chunked", 411),
                                     ("X-Large: " + "x" * httpd.MAX_HEAD, 431)]:
                reply = await raw(app, (f"POST /api/control HTTP/1.1\r\nHost: 127.0.0.1:{app.s.port}\r\n"
                                       + header + "\r\n\r\n").encode())
                assert reply.startswith(f"HTTP/1.1 {expected} ".encode())
            assert await raw(app, b"GET / HTTP/1.1\r\n") == b""
            assert (await http(app, "POST", "/api/control", origin=True, body=b"\xff")).status == 400
            assert (await http(app, "POST", "/api/control", origin=True,
                               body=b'{"number":' + b'9' * 5000 + b'}')).status == 400
    run(scenario())


def test_event_streams_cannot_consume_all_request_slots(tmp_path, monkeypatch):
    from leeroy_mind.console import app as module
    monkeypatch.setattr(module, "MAX_STREAMS", 2)
    async def scenario():
        async with served(make_app(tmp_path)) as app:
            streams = []
            try:
                for _ in range(2):
                    reader, writer = await asyncio.open_connection("127.0.0.1", app.s.port)
                    streams.append(writer)
                    writer.write((f"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1:{app.s.port}\r\n"
                                  f"X-Console-Key: {KEY}\r\n\r\n").encode())
                    await writer.drain()
                    assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)).startswith(b"HTTP/1.1 200 ")
                assert (await http(app, "GET", "/api/events")).status == 503
                assert (await http(app, "GET", "/api/state")).status == 200
            finally:
                for writer in streams:
                    writer.close()
                await asyncio.gather(*(writer.wait_closed() for writer in streams))
    run(scenario())


async def raw(app, data):
    reader, writer = await asyncio.open_connection("127.0.0.1", app.s.port)
    try:
        writer.write(data)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 2)
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


def test_malformed_http_is_refused_without_uncaught_errors(tmp_path):
    async def scenario():
        async with served(make_app(tmp_path)) as app:
            for first, extra in [("GET //[ HTTP/1.1", ""), ("GET / HTTP/1.99", ""),
                                 ("GET /#private HTTP/1.1", ""),
                                 ("GET / HTTP/1.1", "Bad Header: value\r\n"),
                                 ("GET / HTTP/1.1", "X-Test: before\x00after\r\n")]:
                reply = await raw(app, (first + f"\r\nHost: 127.0.0.1:{app.s.port}\r\n"
                                       + extra + "\r\n").encode())
                assert reply.startswith(b"HTTP/1.1 400 "), (first, extra, reply)
    run(scenario())


def test_control_json_rejects_ambiguous_and_excessively_nested_values(tmp_path):
    async def scenario():
        async with served(make_app(tmp_path)) as app:
            for body in [b'{"service":"voice","service":"mind","action":"start"}',
                         b'{"service":"mind","action":"start","unused":NaN}',
                         b'{"service":"mind","action":"start","unused":1e999}',
                         b'{"service":"mind","action":"start","unused":' + b'[' * 40 + b'0' + b']' * 40 + b'}']:
                reply = await http(app, "POST", "/api/control", body=body, origin=True)
                assert reply.status == 400
            assert app.mind.acts == []
    run(scenario())


def test_idle_connections_are_bounded_and_slots_return(tmp_path, monkeypatch):
    monkeypatch.setattr(httpd, "MAX_CONNECTIONS", 3, raising=False)
    async def scenario():
        async with served(make_app(tmp_path)) as app:
            idle = [await asyncio.open_connection("127.0.0.1", app.s.port) for _ in range(3)]
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", app.s.port)
                try:
                    assert await asyncio.wait_for(reader.read(), .3) == b""
                finally:
                    writer.close()
            finally:
                for _, writer in idle:
                    writer.close()
                await asyncio.gather(*(writer.wait_closed() for _, writer in idle))
            for _ in range(5):
                await asyncio.sleep(0)
            assert (await http(app, "GET", "/", key=False)).status == 200
    run(scenario())


def test_face_never_follows_a_member_directory_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "chosen.jpg").write_bytes(b"\xff\xd8\xffprivate synthetic photo")
    data = tmp_path / "data"
    (data / "selfies").mkdir(parents=True)
    try:
        os.symlink(outside, data / "selfies/101", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    async def scenario():
        async with served(make_app(data)) as app:
            assert (await http(app, "GET", "/api/face?guid=101&size=sm")).status == 404
    run(scenario())


def test_all_photo_readers_reject_linked_and_non_image_files(store, tmp_path):
    from leeroy_mind.console import faces, galleryview, selfieview
    from leeroy_mind.selfies import files
    from selfies.fakes import ready
    ready(store, tmp_path, 101, 1)
    thumb = files.candidate(tmp_path, 101, 1, 0, thumb=True)
    thumb.write_bytes(b"synthetic private text")
    assert 0 not in {image["index"] for image in selfieview.set_images(store.conn, tmp_path, 101, 1)["images"]}
    thumb.unlink()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"\xff\xd8\xffoutside synthetic image")
    try:
        thumb.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks unavailable")
    assert 0 not in {image["index"] for image in selfieview.set_images(store.conn, tmp_path, 101, 1)["images"]}
    assert galleryview.photo(store.conn, tmp_path, 101, "candidate", 1, 0, thumb=True) is None
    assert faces.read_image(thumb, tmp_path / "selfies") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_image_ancestors_reject_windows_junctions(tmp_path):
    import subprocess
    from leeroy_mind.console.faces import read_face
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "chosen.jpg").write_bytes(b"\xff\xd8\xffsynthetic")
    root = tmp_path / "data/selfies"
    root.mkdir(parents=True)
    link = root / "101"
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(outside)} | Out-Null"],
                   check=True, capture_output=True)
    assert link.is_junction()
    assert read_face(tmp_path / "data", 101) is None


def test_page_uses_text_sinks_and_memory_only_keys(tmp_path):
    from console.rig import page_source
    source = page_source()
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert unsafe not in source
    assert "textContent = text" in source and "history.replaceState" in source
    assert "document.cookie" not in source and "sessionStorage" not in source
    assert 'export function setKey(key) { KEY = key; GEN++; }' in source


def test_stable_keys_rotate_privately_and_compare_without_plain_equality(tmp_path, monkeypatch):
    from leeroy_mind.console import app as module, keyfile
    from leeroy_mind.endpoint import private_problem
    path = tmp_path / "synthetic-console.key"
    original = keyfile.load(path)
    changed = keyfile.rotate(path)
    assert original != changed and keyfile.load(path) == changed
    with path.open("rb") as stream:
        assert private_problem(stream.fileno()) is None
    calls = []
    compare = module.hmac.compare_digest
    def checked(left, right):
        calls.append((left, right))
        return compare(left, right)
    monkeypatch.setattr(module.hmac, "compare_digest", checked)
    async def scenario():
        async with served(make_app(tmp_path)) as app:
            assert (await http(app, "GET", "/api/state", key=False,
                               headers={"X-Console-Key": "wrong"})).status == 403
            assert (await http(app, "GET", "/api/state")).status == 200
    run(scenario())
    assert (b"wrong", KEY.encode()) in calls and (KEY.encode(), KEY.encode()) in calls


def test_lifecycle_authority_and_fixed_argv_cannot_be_supplied_by_http(tmp_path):
    from leeroy_mind import lifecycle
    from leeroy_mind.console.control import control_problems
    root, data = tmp_path / "checkout & inert", tmp_path / "data & inert"
    data.mkdir()
    python = lifecycle.venv_python(root)
    assert control_problems(root, platform="win32", executable=str(tmp_path / "other.exe"))["mind"]
    calls = []
    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return object()
    lifecycle.spawn_mind(root, data, python, popen=popen)
    args, kwargs = calls[0]
    assert args == [str(python), "-m", "leeroy_mind", "run", "--data-dir", str(data)]
    assert not kwargs.get("shell", False)
    assert kwargs["creationflags"] & lifecycle.CREATE_NO_WINDOW
    assert kwargs["creationflags"] & lifecycle.NORMAL_PRIORITY_CLASS
    assert kwargs["creationflags"] & lifecycle.CREATE_BREAKAWAY_FROM_JOB


def test_packaged_static_links_are_not_public_files(tmp_path, monkeypatch):
    from leeroy_mind.console import app as module
    package = tmp_path / "package"
    (package / "static").mkdir(parents=True)
    (package / "page.html").write_text("<!doctype html><title>synthetic</title>", encoding="utf-8")
    private = tmp_path / "private.css"
    private.write_text("synthetic private text", encoding="utf-8")
    try:
        (package / "static/linked.css").symlink_to(private)
    except OSError:
        pytest.skip("file symlinks unavailable")
    original = module.resources.files
    monkeypatch.setattr(module.resources, "files", lambda name: package if name == module.__package__ else original(name))
    async def scenario():
        async with served(make_app(tmp_path)) as app:
            reply = await http(app, "GET", "/static/linked.css", key=False)
            assert reply.status == 404 and b"synthetic private text" not in reply.body
    run(scenario())


def test_regular_response_drain_also_has_a_deadline(monkeypatch):
    monkeypatch.setattr(httpd, "DRAIN_S", .01)
    async def scenario():
        class StalledWriter:
            def __init__(self):
                self.transport = self
                self.aborted = self.closed = False
            def write(self, data): pass
            async def drain(self): await asyncio.Event().wait()
            def abort(self): self.aborted = True
            def close(self): self.closed = True
            async def wait_closed(self): pass
        reader, writer = asyncio.StreamReader(), StalledWriter()
        reader.feed_data(b"GET / HTTP/1.1\r\nHost: synthetic.invalid\r\n\r\n")
        async def handler(request): return httpd.Response(200, b"synthetic")
        await asyncio.wait_for(httpd._connection(handler, reader, writer), 1)
        assert writer.aborted and writer.closed
    run(scenario())
