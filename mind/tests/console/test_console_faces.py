"""The face endpoint: a member's chosen photo, served as it is, from mind-data/selfies/<guid>/chosen.jpg.
Synthetic bytes only; the console only ever reads under selfies/."""
import asyncio
import os
from pathlib import Path

import pytest

from leeroy_mind.console import faces, httpd
from console.rig import http, make_app

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60  # synthetic


@pytest.fixture
def data(tmp_path):
    return tmp_path


def put(data: Path, guid: int, body: bytes = JPEG) -> Path:
    folder = data / "selfies" / str(guid)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "chosen.jpg"
    path.write_bytes(body)
    return path


def ask(data, path, **kw):
    async def go():
        app = make_app(data)
        await app.start()
        try:
            return await http(app, "GET", path, **kw)
        finally:
            await app.close()
    return asyncio.run(go())


def test_a_chosen_photo_is_served_as_is_for_every_size(data):
    put(data, 1101)
    for size in ("sm", "md", "lg"):
        r = ask(data, f"/api/face?guid=1101&size={size}")
        assert r.status == 200 and r.body == JPEG and r.header("content-type") == "image/jpeg"
        assert r.headers["cache-control"] == ["private, max-age=60"]
        assert r.header("x-content-type-options") == "nosniff"
        assert r.header("cross-origin-resource-policy") == "same-origin"


def test_other_answers_keep_no_store(data):
    assert ask(data, "/api/face?guid=1&size=sm").headers["cache-control"] == ["no-store"]
    assert httpd.Response(200, b"x").encode().count(b"Cache-Control") == 1


def test_no_photo_is_404(data):
    assert ask(data, "/api/face?guid=1101&size=sm").status == 404  # no selfies dir
    (data / "selfies" / "1101").mkdir(parents=True)
    assert ask(data, "/api/face?guid=1101&size=sm").status == 404  # no file


def test_a_bad_file_is_404(data):
    put(data, 1, b"GIF89a" + b"\x00" * 20)
    put(data, 2, b"\xff\xd8\xff" + b"\x00" * faces.MAX_FACE_BYTES)  # one byte over
    (data / "selfies" / "3" / "chosen.jpg").mkdir(parents=True)
    for guid in (1, 2, 3):
        assert ask(data, f"/api/face?guid={guid}&size=sm").status == 404, guid
    real = put(data, 5)
    (data / "selfies" / "4").mkdir()
    try:
        os.symlink(real, data / "selfies" / "4" / "chosen.jpg")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are refused here")
    assert ask(data, "/api/face?guid=4&size=sm").status == 404


def test_the_limit_itself_is_served(data):
    body = b"\xff\xd8\xff" + b"\x00" * (faces.MAX_FACE_BYTES - 3)
    put(data, 1, body)
    assert faces.read_face(data, 1) == body


def test_faces_need_the_key_and_the_host(data):
    put(data, 1101)
    assert ask(data, "/api/face?guid=1101&size=sm", key=False).status == 403
    assert ask(data, "/api/face?guid=1101&size=sm", host="localhost:1").status == 403


def test_a_bad_question_is_400(data):
    put(data, 1101)
    for q in ("", "guid=1101", "size=sm", "guid=0&size=sm", "guid=-1&size=sm", "guid=1e3&size=sm",
              "guid=4294967296&size=sm", "guid=1&guid=2&size=sm", "guid=1101&size=xl",
              "guid=1101&size=sm&size=md"):
        assert ask(data, "/api/face?" + q).status == 400, q


def test_the_console_never_writes_under_selfies(data):
    put(data, 1101)

    def tree():
        return sorted((str(p.relative_to(data)), p.stat().st_size, p.stat().st_mtime_ns)
                      for p in (data / "selfies").rglob("*"))
    before = tree()
    ask(data, "/api/face?guid=1101&size=md")
    ask(data, "/api/face?guid=9&size=md")
    assert tree() == before


def test_the_file_is_not_held_open_after_serving(data):
    path = put(data, 1101)
    ask(data, "/api/face?guid=1101&size=sm")
    path.replace(path.with_name("moved.jpg"))  # Windows refuses this while a reader holds it


def test_the_synthetic_browser_jpeg_is_served_whole(data):
    import base64
    from console.rig import SYNTHETIC_JPEG_B64
    jpeg = base64.b64decode(SYNTHETIC_JPEG_B64)
    assert jpeg.startswith(b"\xff\xd8\xff") and jpeg.endswith(b"\xff\xd9")
    put(data, 7, jpeg)
    assert ask(data, "/api/face?guid=7&size=lg").body == jpeg
