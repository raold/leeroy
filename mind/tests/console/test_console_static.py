"""The console's own static files: its stylesheet and modules, served without the key under the
Host, Origin and Sec-Fetch-Site rules, and a page that runs nothing but them. A real server on a
free port; the scripts are read as text, and node checks them where it is installed."""
import asyncio
import json
import re
import shutil
import subprocess
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

import pytest

from leeroy_mind.console import app as console_app
from console.rig import http, make_app

STATIC = Path(str(resources.files("leeroy_mind.console").joinpath("static")))
MODULES = {"core", "state", "tabs", "words", "strip", "main", "comms", "raid", "guild",
           "personas", "mind", "names", "facecache", "faces"}
IMPORT = re.compile(r'^import\s*(?:\{([^}]*)\}\s*from\s*)?"([^"]+)";', re.M)
EXPORTED = re.compile(r"^export\s+(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)
MARKUP = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
          "new Function", 'setAttribute("style"')


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


def run(coro):
    return asyncio.run(coro)


def _scripts() -> list[Path]:
    found = sorted(p for p in STATIC.rglob("*") if p.suffix in (".mjs", ".js"))
    assert found, "no scripts under static/"
    return found


def _page() -> str:
    return resources.files("leeroy_mind.console").joinpath("page.html").read_text("utf-8")


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


# -- serving -------------------------------------------------------------------------------------

def test_static_files_need_no_key_but_keep_the_host_and_site_rules(data):
    async def go():
        async with served(make_app(data)) as app:
            r = await http(app, "GET", "/static/console.css", key=False)
            assert r.status == 200 and r.header("content-type") == "text/css; charset=utf-8"
            assert r.header("cross-origin-resource-policy") == "same-origin"
            assert r.body == (STATIC / "console.css").read_bytes()
            r = await http(app, "GET", "/static/js/main.mjs", key=False)
            assert r.status == 200
            assert r.header("content-type") == "text/javascript; charset=utf-8"
            assert (await http(app, "GET", "/static/console.css", key=False,
                               host="localhost:1")).status == 403
            assert (await http(app, "GET", "/static/js/main.mjs", key=False,
                               headers={"Sec-Fetch-Site": "cross-site"})).status == 403
            assert (await http(app, "GET", "/static/js/main.mjs", key=False,
                               origin="http://evil")).status == 403
            assert (await http(app, "POST", "/static/console.css", body={},
                               origin=True)).status == 405
    run(go())


def test_only_packaged_files_are_served(data):
    async def go():
        async with served(make_app(data)) as app:
            for path in ("/static/", "/static/nope.css", "/static/../app.py",
                         "/static/js/../../app.py", "/static/%2e%2e/app.py", "/static/page.html",
                         "/static/js/", "/static/console.css/"):
                assert (await http(app, "GET", path, key=False)).status == 404, path
    run(go())


def test_the_static_table_holds_only_the_kinds_it_may_serve():
    files = console_app.static_files()
    assert files["console.css"][1] == "text/css; charset=utf-8"
    assert {f"js/{m}.mjs" for m in MODULES} <= set(files)
    assert all(kind == "text/javascript; charset=utf-8"
               for name, (_, kind) in files.items() if name.endswith(".mjs"))
    assert not [name for name in files if not name.endswith((".css", ".js", ".mjs", ".woff2",
                                                              ".txt"))]


def test_the_page_runs_only_its_own_files(data):
    async def go():
        async with served(make_app(data)) as app:
            r = await http(app, "GET", "/", key=False)
            assert r.status == 200
            assert r.header("content-security-policy") == console_app.CSP
            assert "unsafe-inline" not in console_app.CSP
            assert "frame-ancestors 'none'" in console_app.CSP
            assert r.header("x-frame-options") == "DENY"
            assert r.header("referrer-policy") == "same-origin"  # a POST keeps its Origin
            page = r.body.decode()
            assert "<style" not in page and not re.search(r"<script(?![^>]*\bsrc=)", page)
            assert " style=" not in page
            assert '<link rel="stylesheet" href="/static/console.css">' in page
            assert '<script type="module" src="/static/js/main.mjs"></script>' in page
            for ref in re.findall(r'\b(?:src|href)="([^"]*)"', page):
                assert ref.startswith("/static/") or ref.startswith("data:"), ref
    run(go())
    for f in _scripts():  # nor does any script load anything from elsewhere
        text = f.read_text("utf-8")
        assert "http://" not in text and "https://" not in text, f.name


# -- the scripts ---------------------------------------------------------------------------------

def test_module_imports_resolve():
    exports, graph = {}, {}
    for f in _scripts():
        if f.suffix == ".mjs":
            exports[f] = set(EXPORTED.findall(f.read_text("utf-8")))
    assert MODULES <= {f.stem for f in exports}
    for f in exports:
        text = f.read_text("utf-8")
        statements = IMPORT.findall(text)
        assert len(statements) == len(re.findall(r"^import\b", text, re.M)), f.name
        graph[f] = []
        for names, spec in statements:
            assert spec.startswith(("./", "../")) and spec.endswith(".mjs"), (f.name, spec)
            target = (f.parent / spec).resolve()
            assert target in exports, (f.name, spec)
            graph[f].append(target)
            for name in filter(None, (n.split(" as ")[0].strip() for n in names.split(","))):
                assert name in exports[target], f"{f.name} imports {name} from {spec}"
    seen, todo = set(), [(STATIC / "js" / "main.mjs").resolve()]
    while todo:
        f = todo.pop()
        if f not in seen:
            seen.add(f)
            todo += graph[f]
    assert seen == set(exports), sorted(f.name for f in set(exports) - seen)


def test_every_id_the_scripts_ask_for_is_in_the_page():
    ids = set(re.findall(r'\bid="([^"]+)"', _page()))
    asked = {m for f in _scripts() for m in re.findall(r'\$\("([^"]+)"\)', f.read_text("utf-8"))}
    assert "tab-mind" in asked  # the Mind tab's own buttons are bound to it
    assert asked and asked <= ids, sorted(asked - ids)


def test_scripts_build_no_markup_from_text():
    for f in _scripts():
        text = f.read_text("utf-8")
        for bad in MARKUP:
            assert bad not in text, f"{f.name}: {bad}"


def _run_node(*args) -> subprocess.CompletedProcess:
    """node from static/ with relative names: the live guard refuses a launch whose arguments
    name the mind's package, as a path under it would."""
    return subprocess.run([_node(), *args], cwd=STATIC, capture_output=True, text=True,
                          timeout=60)


def test_scripts_parse():
    for f in _scripts():
        r = _run_node("--check", f.relative_to(STATIC).as_posix())
        assert r.returncode == 0, f"{f.name}: {r.stderr}"


def test_the_shared_modules_load_without_a_page():
    r = _run_node("--input-type=module", "-e",
                  "const c = await import('./js/core.mjs');"
                  "const s = await import('./js/state.mjs');"
                  "const w = await import('./js/words.mjs');"
                  "const got = [c.plural(2, 'raid'), c.pad(7), w.signed(1.5, 1), w.when(0),"
                  " w.moodText(null), w.CLASSES[6], s.S.tab, s.S.voice];"
                  "console.log(JSON.stringify(got));")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == ["2 raids", "07", "+1.5", "", "not recorded yet",
                                    "Death Knight", "comms", None]
