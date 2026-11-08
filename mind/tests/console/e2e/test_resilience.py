import asyncio

from .test_tabs import open_console, tab_names, visit


def test_malformed_poll_keeps_last_good_data_and_recovers(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            panel = await visit(r.page, "guild")
            await expect(panel).to_contain_text("Juniper")
            async def broken(route):
                await route.fulfill(status=200, content_type="application/json", body="{")
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
            opener = r.page.get_by_role("button", name="Browse photos of Arkon", exact=True).first
            await opener.focus()
            await r.page.keyboard.press("Enter")
            dialog = r.page.get_by_role("dialog")
            await expect(dialog).to_be_visible()
            close = dialog.get_by_role("button", name="Close", exact=True)
            await expect(close).to_be_focused()
            await r.page.keyboard.press("Shift+Tab")
            assert await r.page.evaluate("document.activeElement.closest('dialog') !== null")
            await r.page.keyboard.press("Tab")
            await expect(close).to_be_focused()
            await r.page.keyboard.press("Escape")
            await expect(dialog).to_have_count(0)
            await expect(opener).to_be_focused()
            await r.page.locator("#t-personas").focus()
            await r.page.keyboard.press("End")
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
