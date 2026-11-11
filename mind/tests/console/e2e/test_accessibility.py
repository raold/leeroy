"""axe runs on the real rehearsal page; the dependency is local and never fetched by a page."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from .test_tabs import POPULATED, open_console, tab_names, visit

AXE = Path(__file__).parent / "node_modules/axe-core/axe.min.js"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_every_tab_accessibility(rehearsal, tmp_path, theme):
    if not AXE.is_file():
        message = "Install console accessibility checks: npm ci --prefix tests/console/e2e"
        if os.environ.get("LEEROY_CONSOLE_E2E_REQUIRED"):
            pytest.fail(message)
        pytest.skip(message)

    async def run():
        from playwright.async_api import expect
        async with rehearsal(theme=theme) as r:
            await open_console(r)
            await r.page.evaluate(AXE.read_text(encoding="utf-8"))
            results = {}
            for tab in await tab_names(r.page):
                panel = await visit(r.page, tab)
                await expect(panel).to_contain_text(POPULATED[tab])
                report = await r.page.evaluate("""async () => {
                    const r = await axe.run(document, {runOnly: {type: 'tag',
                      values: ['wcag2a', 'wcag2aa', 'wcag21aa']}});
                    return r.violations.map(v => ({id: v.id, impact: v.impact,
                      nodes: v.nodes.map(n => ({target: n.target, summary: n.failureSummary}))}));
                }""")
                results[tab] = report
            (tmp_path / ("axe-" + theme + ".json")).write_text(json.dumps(results, indent=2))
            serious = {tab: rows for tab, items in results.items()
                       if (rows := [v for v in items if v["impact"] in ("serious", "critical")])}
            print("axe", theme, json.dumps({tab: [(v["id"], len(v["nodes"])) for v in rows]
                                           for tab, rows in results.items()}))
            if not os.environ.get("LEEROY_AXE_BASELINE"):
                assert not serious, serious
            assert not r.errors, r.errors
    asyncio.run(run())
