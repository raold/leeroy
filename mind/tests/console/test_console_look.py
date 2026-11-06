"""The Triage look: its tokens in both themes, dark by default, its self-hosted fonts with their
licences, and the one remembered theme key. Static checks plus a real server for the fonts."""
import asyncio
import re
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

import pytest

from console.rig import http, make_app

STATIC = Path(str(resources.files("leeroy_mind.console").joinpath("static")))

LIGHT = {"--bg": "#edf0f3", "--surface": "#ffffff", "--surface-2": "#f5f7f9", "--ink": "#14202b",
         "--ink-2": "#455463", "--ink-3": "#758291", "--line": "#d8dee5", "--accent": "#0d6470",
         "--accent-ink": "#ffffff", "--ok": "#2c7a4d", "--warn": "#b06a0c", "--bad": "#c2352a",
         "--muted": "#7a8197", "--violet": "#6956b0", "--blue": "#2a68a8", "--live": "#1f7a52"}
DARK = {"--bg": "#0e141a", "--surface": "#151d25", "--surface-2": "#1b252f", "--ink": "#e2e8ee",
        "--ink-2": "#a6b2be", "--ink-3": "#74818e", "--line": "#26323e", "--accent": "#4fb2bd",
        "--accent-ink": "#062a2f", "--ok": "#5dbe84", "--warn": "#e2a744", "--bad": "#ef6a5a",
        "--muted": "#9aa1b6", "--violet": "#a592e2", "--blue": "#6ba6e6", "--live": "#57c08c"}
FONTS = {"archivo": ("OFL-Archivo.txt", "Archivo"),
         "hanken-grotesk": ("OFL-HankenGrotesk.txt", "Hanken Grotesk"),
         "jetbrains-mono": ("OFL-JetBrainsMono.txt", "JetBrains Mono")}


def _css() -> str:
    return (STATIC / "console.css").read_text("utf-8")


def _block(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, selector
    return m.group(1)


def _tokens(block: str) -> dict[str, str]:
    return {k: v.strip().lower() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


@asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def test_the_tokens_are_the_triage_consoles():
    css = _css()
    light = _tokens(_block(css, ":root"))
    dark_block = _block(css, ':root[data-theme="dark"]')
    dark = _tokens(dark_block)
    assert LIGHT.items() <= light.items()
    assert DARK.items() <= dark.items()
    assert re.search(r"color-scheme:\s*dark", dark_block)
    assert '"archivo"' in light["--f-display"] and '"hanken grotesk"' in light["--f-body"]
    assert '"jetbrains mono"' in light["--f-mono"] and light["--radius"] == "10px"
    assert "background: var(--bg)" in _block(css, "\nbody")


def test_dark_is_the_default():
    page = resources.files("leeroy_mind.console").joinpath("page.html").read_text("utf-8")
    assert re.search(r'<html\b[^>]*\bdata-theme="dark"', page)
    head = page[:page.index("</head>")]
    assert head.index('<script src="/static/theme.js"></script>') < head.index("/static/console.css")


def test_the_fonts_are_self_hosted_with_their_licences(data):
    async def go():
        async with served(make_app(data)) as app:
            for stem, (licence, family) in FONTS.items():
                r = await http(app, "GET", f"/static/fonts/{stem}.woff2", key=False)
                assert r.status == 200 and r.header("content-type") == "font/woff2", stem
                assert r.body[:4] == b"wOF2", stem
                r = await http(app, "GET", f"/static/fonts/{licence}", key=False)
                assert r.status == 200, licence
                text = r.body.decode("utf-8")
                assert "SIL OPEN FONT LICENSE Version 1.1" in text
                top = [ln for ln in text.splitlines()[:12] if ln.startswith("Copyright")]
                assert top and any(family in ln for ln in top), licence
                assert not any("Reserved Font Name" in ln for ln in text.splitlines()[:12])
    asyncio.run(go())
    css = _css()
    for stem in FONTS:
        assert f"/static/fonts/{stem}.woff2" in css
    assert "http://" not in css and "https://" not in css


def test_the_theme_is_remembered_in_one_key():
    theme = (STATIC / "theme.js").read_text("utf-8")
    assert theme.count('"guild-console-theme"') == 1
    assert "try" in theme and "JSON.parse" in theme
    assert '"light"' in theme and '"dark"' in theme
    owners = [f for f in STATIC.rglob("*") if f.suffix in (".js", ".mjs")
              and "guild-console-theme" in f.read_text("utf-8")]
    assert {f.name for f in owners} == {"theme.js", "main.mjs"}
    main = (STATIC / "js" / "main.mjs").read_text("utf-8")
    assert main.count('"guild-console-theme"') == 1
    assert 'remember(THEME_KEY' in main or 'remember("guild-console-theme"' in main
    assert "localStorage" not in main


def test_the_stored_theme_is_read_in_both_forms():
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        "const src = require('fs').readFileSync('theme.js', 'utf8');"
        "const run = (v) => { const d = {documentElement: {dataset: {theme: 'dark'}}};"
        " const s = {getItem: () => v};"
        " new Function('document', 'localStorage', src)(d, s); return d.documentElement.dataset.theme; };"
        "const thrower = {getItem: () => { throw new Error('off'); }};"
        "const d2 = {documentElement: {dataset: {theme: 'dark'}}};"
        "new Function('document', 'localStorage', src)(d2, thrower);"
        "console.log(JSON.stringify([run('\"light\"'), run('light'), run('\"dark\"'), run('dark'),"
        " run('\"blue\"'), run('{'), run(null), d2.documentElement.dataset.theme]));")
    r = subprocess.run([node, "-e", script], cwd=STATIC, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    import json
    assert json.loads(r.stdout) == ["light", "light", "dark", "dark", "dark", "dark", "dark", "dark"]
