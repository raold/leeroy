import asyncio
import json

import pytest

from .test_tabs import open_console, tab_names, visit


@pytest.mark.parametrize("broken_body", ["{", json.dumps({"roster": [None]})])
def test_malformed_poll_keeps_last_good_data_and_recovers(rehearsal, broken_body):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            panel = await visit(r.page, "guild")
            await expect(panel).to_contain_text("Juniper")
            async def broken(route):
                await route.fulfill(status=200, content_type="application/json", body=broken_body)
            await r.page.route("**/api/guild", broken)
            await visit(r.page, "raid")
            await visit(r.page, "guild")
            await expect(r.page.locator("#guild-stale")).to_contain_text("Stale: keeping the last good data")
            await expect(panel).to_contain_text("Juniper")
            await r.page.unroute("**/api/guild", broken)
            await visit(r.page, "raid")
            await visit(r.page, "guild")
            await expect(r.page.locator("#guild-stale")).to_be_hidden()
            await expect(panel).to_contain_text("Juniper")
            assert not r.errors, r.errors
    asyncio.run(run())


def test_discord_malformed_refresh_keeps_messages_draft_and_recovers(rehearsal):
    async def run():
        from playwright.async_api import expect
        from leeroy_mind.discord import store
        async with rehearsal() as r:
            store.post(r.app.store, channel="general", author=101, origin="mind",
                       text="Fixture Discord message", now=r.app.clock.now())
            await open_console(r)
            await r.page.locator('#dc-channels [data-view="d:general"]').click()
            await expect(r.page.locator("#feed")).to_contain_text("Fixture Discord message")
            draft = r.page.locator("#dc-text")
            await draft.fill("Unsent fixture draft")
            async def broken(route):
                response = await route.fetch(); data = await response.json()
                data["members"] = [None]
                await route.fulfill(response=response, json=data)
            await r.page.route("**/api/discord?*", broken)
            await r.page.evaluate("async () => (await import('/static/js/comms/discordstate.mjs')).refreshDiscord()")
            await expect(r.page.locator("#comms-stale")).to_contain_text("Stale:")
            await expect(r.page.locator("#feed")).to_contain_text("Fixture Discord message")
            await expect(draft).to_have_value("Unsent fixture draft")
            await r.page.unroute("**/api/discord?*", broken)
            await r.page.evaluate("async () => (await import('/static/js/comms/discordstate.mjs')).refreshDiscord()")
            await expect(r.page.locator("#comms-stale")).to_be_hidden()
            await expect(draft).to_have_value("Unsent fixture draft")
            # Whole-item budgets can omit every item without implying an empty store.
            async def omitted(route):
                response = await route.fetch(); data = await response.json()
                for key in ("messages", "quotes", "pins", "threads"):
                    data[key] = []
                data["limited"] = {"messages": True, "quotes": False, "pins": True, "threads": True}
                await route.fulfill(response=response, json=data)
            await r.page.route("**/api/discord?*", omitted)
            await r.page.evaluate("async () => (await import('/static/js/comms/discordstate.mjs')).refreshDiscord()")
            await expect(r.page.locator("#feed")).to_contain_text("Earlier messages remain saved.")
            await expect(r.page.locator("#feed")).not_to_contain_text("Nothing said here yet")
            for shelf, empty in (("pins", "No pinned messages"), ("threads", "No threads here yet")):
                await r.page.locator("#dc-" + shelf).click()
                await expect(r.page.locator("#dc-panel")).to_contain_text("More remain saved.")
                await expect(r.page.locator("#dc-panel")).not_to_contain_text(empty)
            await expect(draft).to_have_value("Unsent fixture draft")
            await r.page.unroute("**/api/discord?*", omitted)
            assert not r.errors, r.errors
    asyncio.run(run())


def test_insight_bad_claim_list_keeps_card_budget_and_draft(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r); await visit(r.page, "personas")
            await r.page.get_by_role("button", name="Profile of Arkon", exact=True).click()
            budget = r.page.get_by_label("Awareness token budget")
            await budget.select_option("small")
            await r.page.locator("#insight-line").fill("Unsent claim fixture")
            before = await r.page.locator("#insight-card").inner_text()
            async def broken(route):
                response = await route.fetch(); data = await response.json()
                data["claims"] = {"state": "ok", "live": [None], "history": []}
                await route.fulfill(response=response, json=data)
            await r.page.route("**/api/persona/insight?*", broken)
            await budget.focus()
            await r.page.evaluate("async () => (await import('/static/js/insight.mjs')).pollInsight(101)")
            await expect(r.page.locator("#personas-stale")).to_contain_text("Stale:")
            await expect(budget).to_be_focused(); await expect(budget).to_have_value("small")
            await expect(r.page.locator("#insight-card")).to_have_text(before)
            await expect(r.page.locator("#insight-line")).to_have_value("Unsent claim fixture")
            await r.page.unroute("**/api/persona/insight?*", broken)
            await r.page.evaluate("async () => (await import('/static/js/insight.mjs')).pollInsight(101)")
            await expect(r.page.locator("#personas-stale")).to_be_hidden()
            assert not r.errors, r.errors
    asyncio.run(run())


def test_ops_unknown_status_is_stale_and_dialog_keyboard_returns_focus(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            r.console.s.ops_actions = True
            await open_console(r); await visit(r.page, "ops")
            opener = r.page.get_by_role("button", name="Take a snapshot", exact=True)
            await expect(opener).to_be_enabled(); await opener.focus()
            await r.page.keyboard.press("Enter")
            dialog = r.page.locator("#ops-action-dialog")
            await expect(dialog).to_be_visible()
            await r.page.get_by_label("Snapshot label", exact=True).fill("fixture")
            await r.page.locator("#ops-action-cancel").focus(); await r.page.keyboard.press("Tab")
            assert await r.page.evaluate("document.activeElement.closest('dialog') !== null")
            await r.page.keyboard.press("Escape")
            await expect(dialog).not_to_be_visible(); await expect(opener).to_be_focused()
            async def broken(route):
                await route.fulfill(status=200, content_type="application/json", json={
                    "enabled": True, "reason": "", "operation": {"id": "fixture", "action": "snapshot",
                        "state": "unknown-state", "message": "Malformed fixture state"}})
            await r.page.route("**/api/ops/action", broken)
            await r.page.evaluate("async () => (await import('/static/js/opsactions.mjs')).refreshActions()")
            await expect(r.page.locator("#ops-stale")).to_contain_text("Stale:")
            await expect(r.page.locator("#ops-action-message")).to_contain_text("unavailable")
            await r.page.unroute("**/api/ops/action", broken)
            await r.page.evaluate("async () => (await import('/static/js/opsactions.mjs')).refreshActions()")
            await expect(r.page.locator("#ops-stale")).to_be_hidden()
            await expect(opener).to_be_enabled()
            assert not r.console._ops_actions.operation
            assert not r.errors, r.errors
    asyncio.run(run())


def test_keyboard_tabs_focus_ring_and_gallery_trap_return(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            tabs = await tab_names(r.page)
            await r.page.locator("#t-comms").focus()
            for tab in tabs[1:] + tabs[:1]:
                await r.page.keyboard.press("ArrowRight")
                button = r.page.locator("#t-" + tab)
                await expect(button).to_be_focused()
                await expect(button).to_have_attribute("aria-selected", "true")
                assert await button.evaluate("e => getComputedStyle(e).outlineStyle !== 'none'")
                # Tab leaves the roving tablist into the main scroll region, then controls.
                await r.page.keyboard.press("Tab")
                await expect(r.page.locator("main")).to_be_focused()
                await r.page.keyboard.press("Shift+Tab")
                await expect(button).to_be_focused()
            await r.page.keyboard.press("End")
            await expect(r.page.locator("#t-mind")).to_be_focused()
            await r.page.keyboard.press("Home")
            await expect(r.page.locator("#t-comms")).to_be_focused()
            for _ in range(tabs.index("personas")):
                await r.page.keyboard.press("ArrowRight")
            gallery_requested, release_gallery = asyncio.Event(), asyncio.Event()
            async def loading_gallery(route):
                gallery_requested.set()
                await release_gallery.wait()
                await route.continue_()
            await r.page.route("**/api/selfies/gallery?*", loading_gallery)
            opener = r.page.get_by_role("button", name="Browse photos of Arkon", exact=True).first
            await opener.focus()
            await r.page.keyboard.press("Enter")
            dialog = r.page.get_by_role("dialog")
            await expect(dialog).to_be_visible()
            close = dialog.get_by_role("button", name="Close", exact=True)
            await expect(close).to_be_focused()
            try:
                await asyncio.wait_for(gallery_requested.wait(), 5)
                # Only Close is enabled while the response is held. Loading must
                # remain trapped, but its tab order is not the loaded tab order.
                await r.page.keyboard.press("Shift+Tab")
                await expect(close).to_be_focused()
            finally:
                release_gallery.set()
            await expect(dialog.locator(".gallery-strip button").first).to_be_visible()
            await expect(dialog.get_by_role("button", name="Next", exact=True)).to_be_enabled()
            # Reproduce the old flaky interleaving: Shift+Tab happened while
            # loading, then Next became enabled before Tab. Next is correct.
            await r.page.keyboard.press("Tab")
            await expect(dialog.get_by_role("button", name="Next", exact=True)).to_be_focused()
            await close.focus()
            await r.page.keyboard.press("Shift+Tab")
            await expect(dialog.locator(".gallery-strip button").last).to_be_focused()
            await r.page.keyboard.press("Tab")
            await expect(close).to_be_focused()
            await r.page.unroute("**/api/selfies/gallery?*", loading_gallery)
            await r.page.keyboard.press("Escape")
            await expect(dialog).to_have_count(0)
            await expect(opener).to_be_focused()
            await r.page.locator("#t-personas").focus()
            await expect(r.page.locator("#t-personas")).to_be_focused()
            await r.page.keyboard.press("End")
            await expect(r.page.locator("#t-mind")).to_have_attribute("aria-selected", "true")
            await expect(r.page.locator("#t-mind")).to_be_focused()
            stop = r.page.locator('button[data-svc="mind"][data-act="stop"]')
            r.expected_http_errors.add(("/api/control", 409))  # explicit confirmation required
            await stop.focus()
            await r.page.keyboard.press("Enter")
            confirmation = r.page.get_by_role("alertdialog")
            await expect(confirmation).to_be_visible()
            await expect(r.page.locator("#confirm-no")).to_be_focused()
            await r.page.keyboard.press("Shift+Tab")
            await expect(r.page.locator("#confirm-yes")).to_be_focused()
            await r.page.keyboard.press("Tab")
            await expect(r.page.locator("#confirm-no")).to_be_focused()
            await r.page.keyboard.press("Escape")
            await expect(confirmation).to_be_hidden()
            await expect(stop).to_be_focused()
            assert not r.handle.acts  # keyboard cancellation never stopped the rehearsal
            assert not r.errors, r.errors
    asyncio.run(run())


def test_mind_bridge_and_voice_down_up_are_words_on_every_tab(rehearsal):
    async def run():
        from playwright.async_api import expect
        from console.test_console_voice import FakeVoice
        from leeroy_mind.console.ws import WsClient
        from selfies.fakes import until
        async with rehearsal() as r:
            fake = FakeVoice()
            await fake.start()
            r.ports.add(fake.port)
            r.console.s.voice_enabled = True
            r.console.s.voice_ui_port = fake.port
            r.console.relay._connect = WsClient.connect
            r.console.relay._retry_s = 0.05
            try:
                await open_console(r)
                await expect(r.page.locator("#s-vlink")).to_contain_text("voice linked")
                await r.bridge.go_down()
                await expect(r.page.locator("#s-bridge")).to_contain_text("down")
                for tab in await tab_names(r.page):
                    await visit(r.page, tab)
                    await expect(r.page.locator("#s-bridge")).to_contain_text("down")
                await r.bridge.come_up()
                await until(lambda: r.app.world.online, 10)
                await expect(r.page.locator("#s-bridge")).not_to_contain_text("down")
                await fake.close()
                await expect(r.page.locator("#s-vlink")).to_contain_text("voice service down")
                for tab in await tab_names(r.page):
                    await visit(r.page, tab)
                    await expect(r.page.locator("#s-vlink")).to_contain_text("voice service down")
                fake.peers.clear()
                await fake.start()
                r.ports.add(fake.port)
                r.console.s.voice_ui_port = fake.port
                await expect(r.page.locator("#s-vlink")).to_contain_text("voice linked")
                await r.stop_mind()
                await expect(r.page.locator("#s-mind")).to_contain_text("mind stopped")
                for tab in await tab_names(r.page):
                    await visit(r.page, tab)
                    await expect(r.page.locator("#s-mind")).to_contain_text("mind stopped")
                await r.start_mind()
                # This fixture has no mind.log growth hint: the settled process probe
                # discovers an external start on its next 15 s pass, plus the 2 s strip.
                from leeroy_mind.console.app import SNAPSHOT_S, STATUS_S
                await expect(r.page.locator("#s-mind")).to_contain_text(
                    "mind running", timeout=(SNAPSHOT_S + STATUS_S) * 1000)
                assert not r.errors, r.errors
            finally:
                await fake.close()
    asyncio.run(run())


def test_changed_roster_keeps_the_profile_button_focused(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await r.page.clock.install()
            await open_console(r)
            await visit(r.page, "personas")
            profile = r.page.get_by_role("button", name="Profile of Arkon", exact=True)
            await expect(profile).to_be_visible()
            async def changed(route):
                response = await route.fetch()
                body = await response.json()
                body["recent_days"] = 8
                await route.fulfill(response=response, json=body)
            await r.page.route("**/api/personas?*", changed)
            await profile.focus()
            await r.page.clock.fast_forward(30001)
            await expect(r.page.locator("#p-memhead")).to_contain_text("8 d")
            await expect(profile).to_be_focused()
            await r.page.keyboard.press("Enter")
            await expect(r.page.locator("#p-detail")).to_be_visible()
            assert not r.errors, r.errors
    asyncio.run(run())
