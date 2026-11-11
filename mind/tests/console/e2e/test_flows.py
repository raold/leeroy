"""User actions cross browser -> console -> real control endpoint -> production stores."""
import asyncio
import re

from conftest import HUMAN
from console.rig import KEY
from leeroy_mind.selfies import store as photos
from selfies.fakes import until

from .test_tabs import open_console, visit


async def submit(r, button, path, *, ok=True):
    async with r.page.expect_response(lambda x: x.url == r.url + path
                                      and x.request.method == "POST") as answer:
        await button.click()
    response = await answer.value
    # Read the body only once the request has finished (earlier, Chrome can answer
    # "No data found for resource"). finished() has no deadline of its own and its
    # requestfinished event sometimes never arrives for these routed requests (CI
    # hung in the mind stop/start flow until the job timeout), so bound the wait;
    # after 10 s the body is there whether or not the event came.
    try:
        await asyncio.wait_for(response.finished(), 10)
    except TimeoutError:
        pass
    result = await response.json()
    assert result.get("ok") is ok, result
    return result


def test_comms_send_and_refusals(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            await r.page.locator("#say-text").fill("Tea is packed for the rehearsal.")
            await submit(r, r.page.locator("#say-send"), "/api/say")
            await expect(r.page.locator("#tab-comms")).to_contain_text("Tea is packed for the rehearsal.")
            await expect(r.page.locator("#say-text")).to_have_value("")
            commands = len(r.bridge.commands)
            await r.page.locator("#say-text").fill("/dance")
            await expect(r.page.locator("#say-send")).to_be_disabled()
            await expect(r.page.locator("#say-why")).to_have_text("Slash commands aren't sent from here.")
            response = await r.context.request.post(r.url + "/api/say", data={
                "channel": "guild", "text": "/dance"}, headers={
                "X-Console-Key": KEY, "Origin": r.url})
            assert response.status == 400
            assert not (await response.json())["ok"]
            assert not any(c.get("text") == "/dance" for c in r.bridge.commands[commands:])
            # A past whisper makes a selectable thread; the cast says Juniper is offline.
            await r.bridge.push({"type": "chat", "channel": "whisper", "from_guid": 110,
                "from_name": "Juniper", "to_guid": HUMAN, "text": "See you next rehearsal.",
                "map": 0, "zone": 1519})
            await r.page.locator('[data-view="w:110"]').click()
            await r.page.locator("#say-text").fill("Are you there?")
            r.expected_http_errors.add(("/api/say", 409))
            refused = await submit(r, r.page.locator("#say-send"), "/api/say", ok=False)
            assert "offline" in refused["error"].lower(), refused
            await expect(r.page.locator("#say-why")).to_contain_text("offline")
            assert not any(c.get("type") == "owner_say" and c.get("to_guid") == 110
                           for c in r.bridge.commands)
            assert not r.errors, r.errors
    asyncio.run(run())


def test_board_reply_and_withdraw(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            panel = await visit(r.page, "board")
            await panel.get_by_role("button", name="Reply", exact=True).click()
            await r.page.locator("#board-reply-text").fill("I will bring the cups.")
            reply = await submit(r, panel.locator(".replybox").get_by_role("button", name="Send"), "/api/board")
            await expect(panel.locator(".reply")).to_contain_text("I will bring the cups.")
            await submit(r, panel.get_by_role("button", name="Withdraw"), "/api/board")
            await expect(panel.locator(".reply")).to_contain_text("withdrawn")
            row = r.app.store.conn.execute("SELECT state FROM board_reply WHERE id=?", (reply["id"],)).fetchone()
            assert row[0] == "withdrawn"
            assert not r.errors, r.errors
    asyncio.run(run())


def test_photo_pick_undo_more_and_gallery_cover(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            await visit(r.page, "personas")
            row = r.page.locator('.sfrow[data-guid="101"]')
            await submit(r, row.locator('[data-index="0"]'), "/api/selfie")
            assert photos.current(r.app.store, 101).idx == 0
            await submit(r, r.page.locator("#sf-status").get_by_role("button", name="Undo"), "/api/selfie")
            assert photos.current(r.app.store, 101) is None
            await submit(r, row.get_by_role("button", name="4 more"), "/api/selfie")
            assert any(s.guid == 101 and s.set_no == 2 for s in photos.queued_sets(r.app.store))
            await r.page.get_by_role("button", name="Browse photos of Arkon").first.click()
            gallery = r.page.get_by_role("dialog", name="Arkon", exact=True)
            await expect(gallery.locator(".gallery-image")).to_be_visible()
            await gallery.get_by_role("button", name="Photo 2", exact=True).click()
            await submit(r, gallery.get_by_role("button", name="Make cover"), "/api/selfie")
            assert photos.current(r.app.store, 101).idx == 1
            await expect(gallery.locator(".gallery-actions")).to_contain_text("Cover")
            await gallery.get_by_role("button", name="Close", exact=True).click()
            await expect(gallery).to_have_count(0)
            assert not r.errors, r.errors
    asyncio.run(run())


def test_tuning_set_preview_refused_and_undo(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            await visit(r.page, "personas")
            await r.page.get_by_role("button", name="Profile of Arkon", exact=True).click()
            dial = r.page.locator("#p-dials .dial").filter(has=r.page.get_by_role("slider", name="talkative", exact=True))
            await dial.get_by_role("slider").fill("8")
            await submit(r, dial.get_by_role("button", name="Set", exact=True), "/api/tune")
            await expect(dial).to_contain_text("your value 8")
            wording = r.page.locator("#p-dials .dial").filter(has=r.page.get_by_role("slider", name="argumentative", exact=True))
            slider = wording.get_by_role("slider")
            await slider.fill("10" if int(await slider.input_value()) <= 5 else "1")
            # The fake model manager may have simulated a login preload. This
            # preview specifically covers the absent-model state, without loading one.
            r.app.arbiter.admin.loaded.clear()
            model_calls = list(r.app.arbiter.admin.calls)
            r.expected_http_errors.add(("/api/tune", 409))
            result = await submit(r, wording.get_by_role("button", name="Preview", exact=True), "/api/tune", ok=False)
            assert "isn't loaded" in result["error"], result
            assert r.app.arbiter.admin.calls == model_calls
            await expect(r.page.locator("#t-preview")).to_contain_text("isn't loaded")
            change = r.page.locator("#t-history .chg").filter(has_text="6 → 8")
            await submit(r, change.get_by_role("button", name="Undo", exact=True), "/api/tune")
            await expect(dial).to_contain_text("your value 6")
            assert not r.errors, r.errors
    asyncio.run(run())


def test_raid_offline_reasons_and_mind_stop_start(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            await r.bridge.push({"type": "logout", "guid": HUMAN, "human": True})
            await until(lambda: not r.app.world.human_online)
            await expect(r.page.locator("#s-game")).to_contain_text("Not in game")
            panel = await visit(r.page, "raid")
            await expect(panel).to_contain_text("you're not in game")
            for button in await panel.locator("button[data-raid], #r-go").all():
                await expect(button).to_be_disabled()
                await expect(button).to_have_attribute("title", re.compile(r".+"))
            await visit(r.page, "mind")
            await submit(r, r.page.locator('[data-svc="mind"][data-act="stop"]'), "/api/control")
            await expect(r.page.locator("#s-mind")).to_contain_text("mind stopped", timeout=15000)
            assert r.task.done()
            await submit(r, r.page.locator('[data-svc="mind"][data-act="start"]'), "/api/control")
            await expect(r.page.locator("#s-mind")).to_contain_text("mind running", timeout=15000)
            assert not r.task.done() and r.app.world.human_online
            assert r.handle.acts == ["stop", "start"]
            assert not r.errors, r.errors
    asyncio.run(run())


def test_hide_photos_draws_initials_and_is_remembered(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            await visit(r.page, "personas")
            row = r.page.locator('.sfrow[data-guid="101"]')
            await submit(r, row.locator('[data-index="0"]'), "/api/selfie")
            tab = r.page.locator("#tab-personas")
            photo = tab.locator('img.av[data-face-guid="101"]').first
            await expect(photo).to_be_visible()
            await r.page.locator("#photos").click()
            await expect(r.page.locator("html")).to_have_attribute("data-photos", "hidden")
            await expect(r.page.locator("#photos")).to_have_text("Show photos")
            await expect(r.page.locator("#photos")).to_have_attribute("aria-pressed", "true")
            await expect(r.page.locator("img.av")).to_have_count(0)
            await expect(tab.locator('span.av[data-face-guid="101"]').first).to_be_visible()
            await r.page.locator("#photos").click()
            await expect(r.page.locator("#photos")).to_have_text("Hide photos")
            await expect(photo).to_be_visible()
            await r.page.locator("#photos").click()
            await visit(r.page, "comms")  # the keyless reload polls only what the theme test expects
            r.expected_http_errors.add(("/api/events", 403))
            r.expected_http_errors.add(("/api/discord", 403))  # Comms polls Discord too
            await r.page.reload()
            await expect(r.page.locator("html")).to_have_attribute("data-photos", "hidden")
            await expect(r.page.locator("#photos")).to_have_text("Show photos")
            assert not r.errors, r.errors
    asyncio.run(run())


def test_theme_persists_key_does_not_and_origin_gate(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            await r.page.locator("#theme").click()
            await expect(r.page.locator("html")).to_have_attribute("data-theme", "light")
            assert "key=" not in r.page.url
            response = await r.context.request.post(r.url + "/api/say", data={
                "channel": "guild", "text": "Never sent."}, headers={
                "X-Console-Key": KEY, "Origin": "https://wrong-origin.invalid"})
            assert response.status == 403
            assert not any(c.get("text") == "Never sent." for c in r.bridge.commands)
            r.expected_http_errors.add(("/api/events", 403))
            r.expected_http_errors.add(("/api/discord", 403))  # Comms polls Discord too
            await r.page.reload()
            await expect(r.page.locator("html")).to_have_attribute("data-theme", "light")
            await expect(r.page.locator("#s-lost")).to_contain_text("opened without its key")
            await expect(r.page.locator("#say-send")).to_be_disabled()
            stored = await r.page.evaluate("({local: {...localStorage}, session: {...sessionStorage}})")
            assert KEY not in str(stored)
            assert not r.errors, r.errors
    asyncio.run(run())
