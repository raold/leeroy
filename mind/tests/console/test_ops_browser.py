"""Optional browser check, confined to an owned synthetic console and fake probes."""
import asyncio
import sys

import pytest

from conftest import run
from console.test_console_ops_actions import served, cli_process
from test_opscontrol import app, add_root, ROOT_ID
from leeroy_mind.console import opsview


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_native_switches_show_file_values_without_runtime_claims(app, tmp_path, theme):
    from console.test_native_switches import files, read
    pw = pytest.importorskip("playwright.async_api")
    async def go():
        root = tmp_path / "install"
        files(root, leeroy="Leeroy.MemberDungeon.Enable = 1", playerbots="Playerbots.Enabled = 0")
        payload = read(root)
        async with served(app) as (c, endpoint), pw.async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**({"channel":"msedge"} if sys.platform=="win32" else {}))
            except pw.Error as error:
                pytest.skip("Optional browser executable is unavailable: " + type(error).__name__)
            sources = {name:lambda:opsview.panel() for name in opsview.PANELS}
            sources["switches"] = lambda:payload
            c._ops = opsview.OpsView(root,app.data_dir,sources=sources)
            page = await browser.new_page(viewport={"width":390,"height":900})
            await page.add_init_script(f"localStorage.setItem('guild-console-theme', '{theme}');")
            errors = []
            page.on("pageerror",lambda error:errors.append(str(error)))
            async def guard(route):
                if not route.request.url.startswith(c.origin+"/"):
                    errors.append("outside fixture")
                    return await route.abort()
                await route.continue_()
            await page.route("**/*",guard)
            try:
                await page.goto(c.url)
                await page.locator("#t-ops").click()
                sections = page.locator(".native-switches")
                await pw.expect(sections).to_have_count(2)
                first = sections.nth(0)
                await first.locator("summary").focus()
                await page.keyboard.press("Enter")
                await pw.expect(first.get_by_text("Leeroy.MemberDungeon.Enable · configured on · running state unknown",exact=True)).to_be_visible()
                await pw.expect(first.get_by_text("Source: build/bin/RelWithDebInfo/configs/modules/mod_leeroy.conf",exact=True)).to_be_visible()
                second = sections.nth(1)
                await second.locator("summary").click()
                await pw.expect(second.get_by_text("Playerbots.Enabled · configured off · running state unknown",exact=True)).to_be_visible()
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert not await page.locator(".native-switches button,.native-switches input").count()
                await sections.nth(0).screenshot(path=str(tmp_path/f"native-switches-{theme}.png"))
                assert not errors
            finally:
                await browser.close()
    run(go())


def test_raid_debrief_keyboard_details_and_mobile_layout(app, tmp_path):
    pw = pytest.importorskip("playwright.async_api")
    async def go():
        async with served(app) as (c, endpoint), pw.async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**({"channel":"msedge"} if sys.platform=="win32" else {}))
            except pw.Error as error:
                pytest.skip("Optional browser executable is unavailable: " + type(error).__name__)
            report = dict(raid=dict(id=1,name="Icecrown Citadel",offline=True,started=1,ended=600,outcome="too_few",kills=1,wipes=1),
                logs=dict(files=2,records=4,note="Observed counts are lower bounds.",native_timezone="UTC"),
                bosses=[dict(map=631,boss_id=9,difficulty=0,boss="Valithria Dreamwalker",pulls=1,kills=0,wipes=1,
                    incomplete=0,aborted=0,mechanics=[dict(name="Dream portals",evidence="no_direct_action_telemetry")],
                    hits=[dict(guid=101,spell=123,hits=2,requested=300,mechanics=["Test hazard"])])],
                group_rez=[dict(reason="fenced",fence="raid,realm",records=2,max_dead=2,max_casters=1,last=500)])
            sources = {name:lambda:opsview.panel() for name in opsview.PANELS}
            sources["debrief"] = lambda:opsview.panel(**report)
            c._ops = opsview.OpsView(app.data_dir,app.data_dir,sources=sources)
            page = await browser.new_page(viewport={"width":390,"height":900})
            errors = []
            page.on("pageerror",lambda error:errors.append(str(error)))
            async def guard(route):
                if not route.request.url.startswith(c.origin+"/"):
                    errors.append("outside fixture")
                    return await route.abort()
                await route.continue_()
            await page.route("**/*",guard)
            try:
                await page.goto(c.url)
                await page.locator("#t-ops").click()
                panel = page.locator(".raid-debrief")
                await pw.expect(panel).to_be_visible(timeout=10000)
                summary = panel.locator("summary").filter(has_text="Valithria")
                await summary.focus()
                await page.keyboard.press("Enter")
                await pw.expect(panel.get_by_text("Dream portals: no direct action telemetry; success unverified",exact=True)).to_be_visible()
                await pw.expect(panel.get_by_text("Member #101 · spell 123 · Test hazard · 2 hits · 300 requested damage",exact=True)).to_be_visible()
                await panel.locator("summary").filter(has_text="Unassigned group").click()
                await pw.expect(panel.get_by_text("group_rez_blocked",exact=False)).to_be_visible()
                assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                await panel.screenshot(path=str(tmp_path/"raid-debrief-mobile.png"))
                assert not errors
            finally:
                await browser.close()
    run(go())


def test_ops_preview_freezes_draft_discards_closed_dialog_and_saves_snapshot(app):
    pw = pytest.importorskip("playwright.async_api")
    async def go():
        async with served(app) as (c, endpoint), pw.async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**({"channel":"msedge"} if sys.platform=="win32" else {}))
            except pw.Error as e:
                pytest.skip("Optional browser executable is unavailable: "+type(e).__name__)
            c._ops = opsview.OpsView(app.data_dir,app.data_dir,sources={name:lambda:opsview.panel() for name in opsview.PANELS})
            page = await browser.new_page(viewport={"width":390,"height":900})
            errors = []
            page.on("pageerror",lambda error:errors.append(str(error)))
            seen, release = asyncio.Event(), asyncio.Event()
            delay = [False]
            async def guard(route):
                if not route.request.url.startswith(c.origin+"/"):
                    errors.append("outside fixture")
                    return await route.abort()
                if (delay[0] and route.request.method=="POST" and route.request.url.endswith("/api/ops/action")
                        and route.request.post_data_json.get("phase")=="preview"):
                    seen.set()
                    await release.wait()
                await route.continue_()
            await page.route("**/*",guard)
            try:
                await page.goto(c.url)
                await pw.expect(page.locator("#t-comms")).to_have_attribute("aria-selected","true")
                await page.locator("#t-ops").click()
                button = page.get_by_role("button",name="Take a snapshot",exact=True)
                await pw.expect(button).to_be_enabled(timeout=10000)
                await button.click()
                await page.locator("#ops-action-label").fill("first")
                delay[0] = True
                await page.locator("#ops-action-preview").click()
                await asyncio.wait_for(seen.wait(),5)
                await pw.expect(page.locator("#ops-action-label")).to_be_disabled()
                await pw.expect(page.locator("#ops-action-label")).to_have_value("first")
                await page.keyboard.press("Escape")
                await pw.expect(page.locator("#ops-action-dialog")).not_to_be_visible()
                release.set()
                await pw.expect(button).to_be_enabled(timeout=10000)
                await button.click()
                await pw.expect(page.locator("#ops-action-label")).to_have_value("")
                await pw.expect(page.locator("#ops-action-preview")).to_be_visible()
                await pw.expect(page.locator("#ops-action-confirm")).not_to_be_visible()
                delay[0] = False
                await page.locator("#ops-action-label").fill("bad/path")
                await page.locator("#ops-action-preview").click()
                await pw.expect(page.locator("#ops-action-words")).to_contain_text("Snapshot label")
                await pw.expect(page.locator("#ops-action-label")).to_be_enabled()
                await pw.expect(page.locator("#ops-action-label")).to_have_value("bad/path")
                await page.locator("#ops-action-label").fill("browser")
                await page.locator("#ops-action-preview").click()
                await pw.expect(page.locator("#ops-action-confirm")).to_be_visible()
                await page.locator("#ops-action-confirm").click()
                await pw.expect(page.locator("#ops-action-message")).to_contain_text("snapshot was saved",timeout=10000)
                assert len(list((app.data_dir/"snapshots").glob("*.db"))) == 1
                for width in (360,390,1440):
                    await page.set_viewport_size({"width":width,"height":900})
                    for theme in ("dark","light"):
                        await page.evaluate("theme=>document.documentElement.dataset.theme=theme",theme)
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not errors
            finally:
                release.set()
                await browser.close()
    run(go())


@pytest.mark.parametrize("case", ["wrong_id", "expired"])
def test_ops_confirmation_refusal_restores_review_without_losing_draft(app, cli_process, case):
    from pathlib import Path
    from leeroy_mind.lifecycle import Outcome
    pw = pytest.importorskip("playwright.async_api")
    journal = add_root(app.store.conn)

    async def go():
        async with served(app) as (c, endpoint), pw.async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(**({"channel":"msedge"} if sys.platform=="win32" else {}))
            except pw.Error as e:
                pytest.skip("Optional browser executable is unavailable: "+type(e).__name__)
            c.s.root, c.mind.python = Path(__file__).resolve().parents[3], Path(sys.executable)
            c.mind.clean_stop = lambda: Outcome(0, "stopped", "stopped")
            sources = {name:lambda:opsview.panel() for name in opsview.PANELS}
            sources["realm"] = lambda:opsview.panel(roots=[{"id":ROOT_ID,"state":"unknown","release_command":"synthetic"}])
            c._ops = opsview.OpsView(app.data_dir, app.data_dir, sources=sources)
            page = await browser.new_page()
            errors = []
            page.on("pageerror",lambda error:errors.append(str(error)))
            async def guard(route):
                if not route.request.url.startswith(c.origin+"/"):
                    errors.append("outside fixture")
                    return await route.abort()
                await route.continue_()
            await page.route("**/*",guard)
            try:
                await page.goto(c.url)
                await pw.expect(page.locator("#t-comms")).to_have_attribute("aria-selected","true")
                await page.locator("#t-ops").click()
                review = page.get_by_role("button",name="Review release",exact=True)
                await pw.expect(review).to_be_enabled(timeout=10000)
                await review.click()
                await page.locator("#ops-action-reason").fill("retained operator reason")
                await page.locator("#ops-action-preview").click()
                await pw.expect(page.locator("#ops-action-confirm")).to_be_visible()
                typed = "wrong" if case == "wrong_id" else ROOT_ID
                await page.locator("#ops-action-typed").fill(typed)
                if case == "expired":
                    token, _, args = c._ops_actions.preview
                    c._ops_actions.preview = (token, 0, args)
                await page.locator("#ops-action-confirm").click()
                await pw.expect(page.locator("#ops-action-words")).to_contain_text("did not match" if case=="wrong_id" else "expired")
                await pw.expect(page.locator("#ops-action-preview")).to_be_visible()
                await pw.expect(page.locator("#ops-action-preview")).to_be_focused()
                await pw.expect(page.locator("#ops-action-confirm")).not_to_be_visible()
                await pw.expect(page.locator("#ops-action-reason")).to_be_enabled()
                await pw.expect(page.locator("#ops-action-reason")).to_have_value("retained operator reason")
                await pw.expect(page.locator("#ops-action-typed")).to_have_value(typed)
                assert c._ops_actions.preview is None and journal.get(ROOT_ID).state == "unknown"
                await page.locator("#ops-action-preview").click()
                await pw.expect(page.locator("#ops-action-confirm")).to_be_visible()
                await page.locator("#ops-action-typed").fill(ROOT_ID)
                await page.locator("#ops-action-confirm").click()
                await pw.expect(page.locator("#ops-action-message")).to_contain_text("was released",timeout=10000)
                app.store.conn.commit()  # Refresh this fixture connection after the separate CLI commit.
                assert journal.get(ROOT_ID).result_reason == "abandoned"
                assert c.mind.acts == ["start"] and not errors
            finally:
                await browser.close()
    run(go())
