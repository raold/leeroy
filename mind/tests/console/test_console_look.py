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


TABS = ["comms", "raid", "guild", "social", "personas", "board", "calendar", "bank", "realm",
        "training", "dojo", "library", "mind"]
LABELS = ["Comms", "Raid", "Guild", "Social", "Personas", "Board", "Calendar", "Bank", "Realm",
          "Training", "Dojo", "Library", "Mind"]
COMING = {
    "board": "The guild's message board: realm forum posts, the Realm's write-ups and the letters members "
             "send you, with your replies. Coming soon.",
    "realm": "The three rival guilds: their members and progress, the race for first kills, poaching, their "
             "Trade-chat lines and canon achievements. Coming soon.",
    "dojo": "The dojo's training realm: whether it is running, its build, the acceptance gates and their "
            "evidence, recorded episodes and replays. Coming soon.",
    "library": "Search the scholar's library: GM commands, teleports, configuration templates, items, quests "
               "and spells. Coming soon.",
}


def _page() -> str:
    return resources.files("leeroy_mind.console").joinpath("page.html").read_text("utf-8")


def test_the_thirteen_tabs_in_order():
    page = _page()
    nav = page[page.index('<nav class="tabs"'):page.index("</nav>")]
    assert re.findall(r'data-tab="([\w-]+)"', nav) == TABS
    assert re.findall(r'<button[^>]*>([^<]+)(?:<|$)', nav) == LABELS
    for t in TABS:
        assert f'id="t-{t}"' in nav and f'id="tab-{t}"' in page


def test_the_new_tabs_say_what_is_coming():
    page = _page()
    for t, copy in COMING.items():
        m = re.search(rf'<section id="tab-{t}"([^>]*)>(.*?)</section>', page, re.S)
        assert m, t
        assert re.search(r'class="(?:[^"]* )?placeholder(?: [^"]*)?"', m.group(1)), t
        assert f"<h2>{LABELS[TABS.index(t)]}</h2>" in m.group(2), t
        assert f"<p>{copy}</p>" in m.group(2), t
    for t in ("comms", "raid", "guild", "social", "calendar", "bank", "personas", "training", "mind"):
        assert not re.search(rf'<section id="tab-{t}"[^>]*placeholder', page), t


def test_the_strip_names_the_mode_and_the_brand():
    page = _page()
    assert re.search(r'<div class="brand">.*?<h1>Guild Console</h1>', page, re.S)
    assert "The guild, the realm and the mind, beside the game" in page
    assert re.search(r'<span id="s-game" class="mode"', page)
    assert 'id="theme"' in page
    for i in ("s-mind", "s-bridge", "s-vlink", "s-game", "s-room", "s-ptt", "s-lost"):
        assert f'id="{i}"' in page
    strip = (STATIC / "js" / "strip.mjs").read_text("utf-8")
    for w in ('"In game"', '"Not in game"', '"Game: unknown"'):
        assert w in strip


def test_the_header_is_sticky_and_toasts_sit_below_it():
    css = _css()
    assert re.search(r"position:\s*sticky", _block(css, "\n.top"))
    page = _page()
    assert page.index('id="toasts"') < page.index("</header>")
    assert re.search(r"top:\s*100%", _block(css, "#toasts"))


def test_each_banner_aligns_with_its_own_panels():
    css = _css()
    assert ".tab > .banner" not in css and "#tab-comms > .banner" in css


def test_the_address_bar_hash_opens_a_tab():
    main = (STATIC / "js" / "main.mjs").read_text("utf-8")
    assert 'addEventListener("hashchange"' in main


def _js(name: str) -> str:
    return (STATIC / "js" / name).read_text("utf-8")


def test_a_key_pasted_into_the_open_tab_is_taken():
    """After a console restart he pastes the new URL into the tab already open: only the fragment
    changes, so the page does not reload; the hashchange handler takes the key, clears it from the
    address bar and connects again (key.test.mjs checks hashKey itself)."""
    main = _js("main.mjs")
    handler = main[main.index('addEventListener("hashchange"'):]
    assert "hashKey(location.hash)" in handler and "takeKey(" in handler
    take = re.search(r"function takeKey\(key\) \{(.*?)\n\}", main, re.S)
    assert take, "takeKey"
    for step in ("setKey(key)", "forgetFaces()", "connect()", 'lost("")'):
        assert step in take.group(1), step
    assert "history.replaceState" in handler


def test_the_comms_columns_hold_at_narrow_widths():
    """Three columns need room: below 1101 px the right column goes under the other two, so the
    feed keeps its width (the page check measures 721, 900 and 1100 px)."""
    css = _css()
    m = re.search(r"@media \(max-width: 1100px\) \{(.*?)\n\}", css, re.S)
    assert m, "no rule for the middle widths"
    assert ".comms" in m.group(1) and ".panel.right" in m.group(1)


def test_the_voice_status_and_a_crowded_room_are_named():
    page = _page()
    assert re.search(r'<div class="vstatus" role="group" aria-label="Voice status">', page)
    left = (STATIC / "js" / "comms" / "leftview.mjs").read_text("utf-8")
    assert 'strip.setAttribute("aria-label"' in left


def test_speaking_lights_faces_without_a_redraw():
    """A speaking event toggles the class on the faces already drawn: a redraw of the room tree on
    every line would eat clicks and move the focus."""
    left = (STATIC / "js" / "comms" / "leftview.mjs").read_text("utf-8")
    on_voice = left[left.index("var ON_VOICE"):]
    assert re.search(r"speaking:\s*function", on_voice)
    assert not re.search(r"speaking:\s*renderRooms", on_voice)
    assert not re.search(r"ptt:\s*function \(\) \{ renderRooms", on_voice)
    right = (STATIC / "js" / "comms" / "rightview.mjs").read_text("utf-8")
    assert "lightSpeakers(" in left and "lightSpeakers(" in right
