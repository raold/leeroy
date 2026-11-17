"""The page and its static files are revalidated, not refetched (no-cache + an ETag of this
console's build), and text answers over 1 KB go out gzipped to a client that takes gzip. The
CSP, the keyless static rule and the reload-after-release mechanism stay as they were."""
import asyncio
import gzip
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from leeroy_mind.console import app as console_app
from leeroy_mind.console.httpd import Request, Response, Stream
from console.rig import http, make_app

GZ = {"Accept-Encoding": "gzip, deflate, br"}


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def test_static_and_page_are_revalidated_with_an_etag(data):
    async def go():
        async with served(make_app(data)) as app:
            out = {}
            for path in ("/", "/static/console.css", "/static/js/main.mjs", "/static/fonts/" + next(
                    n for n in app._static if n.startswith("fonts/") and n.endswith(".woff2")).split("/", 1)[1]):
                first = await http(app, "GET", path, key=False)
                assert first.status == 200 and first.header("cache-control") == "no-cache", path
                etag = first.header("etag")
                assert etag and app.build in etag, path
                again = await http(app, "GET", path, key=False, headers={"If-None-Match": etag})
                assert again.status == 304 and again.body == b"" and again.header("etag") == etag, path
                assert again.header("cache-control") == "no-cache"
                stale = await http(app, "GET", path, key=False, headers={"If-None-Match": '"0000000000000000"'})
                assert stale.status == 200 and stale.body == first.body, path
                out[path] = first
            assert "script-src 'self'" in out["/"].header("content-security-policy")
            # an API answer is still never stored
            assert (await http(app, "GET", "/api/health")).header("cache-control") == "no-store"
    asyncio.run(go())


def test_text_over_1k_is_gzipped_for_a_client_that_takes_it(data):
    async def go():
        async with served(make_app(data)) as app:
            plain = await http(app, "GET", "/static/console.css", key=False)
            packed = await http(app, "GET", "/static/console.css", key=False, headers=GZ)
            assert plain.header("content-encoding") is None
            assert packed.header("content-encoding") == "gzip" and "Accept-Encoding" in packed.header("vary")
            assert gzip.decompress(packed.body) == plain.body and len(packed.body) < len(plain.body) / 2
            assert packed.header("etag") == plain.header("etag")
            page = await http(app, "GET", "/", key=False, headers=GZ)
            assert page.header("content-encoding") == "gzip"
            assert "script-src 'self'" in page.header("content-security-policy")
            assert app.build.encode() in gzip.decompress(page.body)
            # a font is already compressed, a refusal of gzip is honoured
            font = next(n for n in app._static if n.endswith(".woff2"))
            assert (await http(app, "GET", "/static/" + font, key=False, headers=GZ)).header("content-encoding") is None
            none = await http(app, "GET", "/static/console.css", key=False, headers={"Accept-Encoding": "gzip;q=0, br"})
            assert none.header("content-encoding") is None and none.body == plain.body
            # JSON: long answers packed, short ones as they are; the key is still needed
            ops = await http(app, "GET", "/api/ops", headers=GZ)
            assert ops.status == 200 and ops.header("content-encoding") == "gzip"
            assert "panels" in json.loads(gzip.decompress(ops.body))
            assert ops.header("cache-control") == "no-store"
            short = await http(app, "GET", "/api/ops/action", headers=GZ)
            assert short.status == 200 and short.header("content-encoding") is None
            assert (await http(app, "GET", "/api/ops", key=False, headers=GZ)).status == 403
    asyncio.run(go())


def test_compress_leaves_streams_photos_and_encoded_answers_alone():
    req = Request("GET", "/x", "/x", {}, [("accept-encoding", "gzip")])
    big = b"a" * 5000
    stream = Stream(None, content_type="text/event-stream")
    assert console_app.compress(req, stream) is stream
    photo = Response(200, big, "image/jpeg")
    assert console_app.compress(req, photo) is photo
    done = Response(200, big, "text/plain", [("Content-Encoding", "gzip")])
    assert console_app.compress(req, done) is done
    small = Response(200, b"a" * 1024, "application/json")
    assert console_app.compress(req, small) is small
    packed = console_app.compress(req, Response(200, big, "application/json"))
    assert gzip.decompress(packed.body) == big
    assert not console_app.accepts_gzip(Request("GET", "/x", "/x", {}, []))
    assert console_app.accepts_gzip(Request("GET", "/x", "/x", {}, [("accept-encoding", "br, GZIP;q=0.5")]))


def test_a_release_changes_every_etag_so_the_reload_fetches_the_new_files(data, monkeypatch):
    """update.mjs reloads a page whose build differs from the snapshot's; that reload must get
    the new files, never a 304 for the old ones."""
    async def go():
        async with served(make_app(data)) as old:
            before = (await http(old, "GET", "/static/js/main.mjs", key=False)).header("etag")
            page_tag = (await http(old, "GET", "/", key=False)).header("etag")
            old_build = old.build
        real = console_app.static_files

        def released():
            files = real()
            body, kind = files["js/update.mjs"]
            files["js/update.mjs"] = (body + b"\n// a release\n", kind)
            return files
        monkeypatch.setattr(console_app, "static_files", released)
        async with served(make_app(data)) as new:
            assert new.build != old_build
            # the event stream's snapshot carries new.build (test_console_build), which a stale
            # page compares with its own meta before it reloads
            for path, tag in (("/static/js/main.mjs", before), ("/", page_tag)):
                got = await http(new, "GET", path, key=False, headers={"If-None-Match": tag})
                assert got.status == 200 and got.header("etag") != tag, path
            page = (await http(new, "GET", "/", key=False)).body.decode()
            assert f'<meta name="console-build" content="{new.build}">' in page
    asyncio.run(go())
