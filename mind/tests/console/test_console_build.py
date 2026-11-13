"""A tab left open across a console update: the page and the event stream's snapshot carry this
console's build id, a fingerprint of the page and its static files, so a stale page can tell."""
import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from leeroy_mind.console import app as console_app
from console.rig import http, make_app


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


def test_the_page_carries_this_consoles_build(data):
    async def go():
        async with served(make_app(data)) as app:
            assert re.fullmatch(r"[0-9a-f]{16}", app.build)
            page = (await http(app, "GET", "/", key=False)).body.decode()
            assert f'<meta name="console-build" content="{app.build}">' in page
            assert console_app.BUILD_MARK.decode() not in page
    asyncio.run(go())


def test_the_build_follows_the_page_and_every_static_file():
    files = {"js/a.mjs": (b"one", "text/javascript"), "console.css": (b"x", "text/css")}
    base = console_app.console_build(b"<page>", files)
    assert base == console_app.console_build(b"<page>", dict(reversed(list(files.items()))))
    assert base != console_app.console_build(b"<page2>", files)
    assert base != console_app.console_build(b"<page>", {**files, "js/a.mjs": (b"two", "text/javascript")})
    assert base != console_app.console_build(b"<page>", {**files, "js/b.mjs": (b"", "text/javascript")})


def test_the_installed_page_and_files_have_one_build():
    page = (Path(console_app.__file__).parent / "page.html").read_bytes()
    assert console_app.BUILD_MARK in page
    assert console_app.console_build(page, console_app.static_files()) == \
        console_app.console_build(page, console_app.static_files())
