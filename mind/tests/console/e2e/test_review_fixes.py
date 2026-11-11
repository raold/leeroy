"""Review regressions against the real console with owned synthetic boundaries."""
import asyncio
from urllib.parse import urlsplit,parse_qs

from .test_tabs import open_console,visit


def test_missing_photo_set_is_empty_and_clears_prior_failure(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            r.expected_http_errors.update({("/api/selfies/member",503),("/api/selfies/member",404)})
            status = 503
            async def answer(route):
                await route.fulfill(status=status,json={"error":"mind.db could not be read (FixtureError)"})
            await r.page.route("**/api/selfies/member?*",answer)
            await open_console(r)
            await visit(r.page,"personas")
            await r.page.get_by_role("button",name="Profile of Arkon",exact=True).click()
            await expect(r.page.locator("#p-photonow")).to_contain_text("FixtureError")
            status = 404
            await r.page.evaluate("async () => (await import('/static/js/selfies.mjs')).showMemberPhoto(101,'Arkon')")
            await expect(r.page.locator("#p-photonow")).to_contain_text("No photos yet")
            await expect(r.page.locator("#personas-stale")).to_be_hidden()
            assert not r.errors,r.errors
    asyncio.run(run())


def test_gallery_arrows_keep_focus_across_windows(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            async def gallery(route):
                response = await route.fetch()
                data = await response.json()
                original = data["view"]["photos"][0]
                data["view"]["photos"] = [dict(original,id=f"fixture-{i:02}",rendered=100-i,cover=False) for i in range(40)]
                await route.fulfill(response=response,json=data)
            await r.page.route("**/api/selfies/gallery?*",gallery)
            await open_console(r)
            await r.page.evaluate("async () => (await import('/static/js/gallery.mjs')).openGallery(101,'Arkon',document.querySelector('#t-comms'))")
            dialog = r.page.get_by_role("dialog",name="Arkon",exact=True)
            await dialog.get_by_role("button",name="Photo 32",exact=True).click()
            await r.page.keyboard.press("ArrowRight")
            await expect(dialog.get_by_role("button",name="Photo 33",exact=True)).to_be_focused()
            await r.page.keyboard.press("ArrowLeft")
            await expect(dialog.get_by_role("button",name="Photo 32",exact=True)).to_be_focused()
            assert not r.errors,r.errors
    asyncio.run(run())


def test_roster_total_and_page_click_during_poll(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            entered,release = asyncio.Event(),asyncio.Event()
            hold = False
            async def roster(route):
                offset = int(parse_qs(urlsplit(route.request.url).query).get("offset",[0])[0])
                response = await route.fetch()
                data = await response.json()
                source = data["members"][0]
                data["members"] = [dict(source,name=f"Fixture {i}") for i in range(offset,min(offset+50,120))]
                data["page"] = dict(offset=offset,limit=50,total=120,personas=117,more=offset+50<120)
                if hold and offset==0:
                    entered.set()
                    await release.wait()
                await route.fulfill(response=response,json=data)
            await r.page.route("**/api/personas?*",roster)
            await open_console(r)
            await visit(r.page,"personas")
            await expect(r.page.locator("#p-count")).to_have_text("120 members, 117 with a persona")
            await expect(r.page.get_by_role("group",name="Member pages")).to_contain_text("Members 1–50 of 120")
            hold = True
            await visit(r.page,"guild")
            await visit(r.page,"personas")
            await asyncio.wait_for(entered.wait(),5)
            await r.page.get_by_role("button",name="Next members",exact=True).click()
            release.set()
            await expect(r.page.get_by_role("group",name="Member pages")).to_contain_text("Members 51–100 of 120")
            assert not r.errors,r.errors
    asyncio.run(run())


def test_status_live_regions_exclude_room_and_controls(rehearsal):
    async def run():
        async with rehearsal() as r:
            await open_console(r)
            assert await r.page.locator(".strip[aria-live]").count()==0
            for selector in ("#s-mind","#s-bridge","#s-vlink"):
                assert await r.page.locator(selector).get_attribute("aria-live")=="polite"
            assert not await r.page.locator("#s-room").get_attribute("aria-live")
            assert not r.errors,r.errors
    asyncio.run(run())


def test_one_shot_gallery_error_clears_without_retry_promise(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            r.expected_http_errors.add(("/api/selfies/gallery",503))
            async def broken(route):
                await route.fulfill(status=503,json={"error":"Gallery fixture unavailable"})
            await r.page.route("**/api/selfies/gallery?*",broken)
            await open_console(r)
            await visit(r.page,"personas")
            await r.page.get_by_role("button",name="Browse photos of Arkon",exact=True).first.click()
            dialog = r.page.get_by_role("dialog",name="Arkon",exact=True)
            await expect(dialog.get_by_role("status")).to_contain_text("Gallery fixture unavailable")
            await expect(r.page.locator("#personas-stale")).to_be_hidden()
            await r.page.unroute("**/api/selfies/gallery?*",broken)
            await r.page.evaluate("async () => (await import('/static/js/core.mjs')).emit('selfie',{guid:101})")
            await expect(dialog.locator(".gallery-image")).to_be_visible()
            await expect(dialog.get_by_role("status")).to_have_text("")
            assert not r.errors,r.errors
    asyncio.run(run())


def test_selfie_images_offer_explicit_retry_and_clear_error(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            r.expected_http_errors.add(("/api/selfies/images",503))
            async def broken(route):
                await route.fulfill(status=503,json={"error":"Images fixture unavailable"})
            await r.page.route("**/api/selfies/images?*",broken)
            await open_console(r)
            await visit(r.page,"personas")
            row = r.page.locator('.sfrow[data-guid="101"]')
            await expect(row.get_by_role("status")).to_contain_text("Images fixture unavailable")
            await expect(r.page.locator("#personas-stale")).to_be_hidden()
            await r.page.unroute("**/api/selfies/images?*",broken)
            await row.get_by_role("button",name="Retry photos").click()
            await expect(row.locator(".shot-error")).to_have_count(0)
            await expect(row.locator(".shots img")).to_have_count(4)
            assert not r.errors,r.errors
    asyncio.run(run())


def test_library_server_error_recovers_without_retry_banner(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            r.expected_http_errors.add(("/api/library",503))
            async def broken(route):
                await route.fulfill(status=503,json={"error":"library.db could not be read (FixtureError)"})
            await open_console(r)
            await visit(r.page,"library")
            await r.page.route("**/api/library?*",broken)
            await r.page.locator("#library-q").fill("fixture")
            await r.page.locator("#library-go").click()
            await expect(r.page.locator("#library-banner")).to_contain_text("FixtureError")
            await expect(r.page.locator("#library-stale")).to_be_hidden()
            await r.page.unroute("**/api/library?*",broken)
            await r.page.locator("#library-go").click()
            await expect(r.page.locator("#library-banner")).to_be_hidden()
            assert not r.errors,r.errors
    asyncio.run(run())


def test_comms_wake_has_one_discord_read(rehearsal):
    async def run():
        async with rehearsal() as r:
            await open_console(r)
            await r.page.wait_for_function("async () => (await import('/static/js/comms/discordstate.mjs')).DISCORD.loaded")
            await r.page.wait_for_timeout(150)
            calls = []
            r.page.on("request",lambda req:calls.append(req.url) if urlsplit(req.url).path=="/api/discord" else None)
            await r.page.evaluate("""() => {
                Object.defineProperty(document,'hidden',{configurable:true,value:true});
                Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            await r.page.evaluate("""() => {
                Object.defineProperty(document,'hidden',{configurable:true,value:false});
                Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            await r.page.wait_for_timeout(300)
            assert len(calls)==1,calls
            assert not r.errors,r.errors
    asyncio.run(run())


def test_unanswered_say_is_uncertain_even_after_switching_views(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal() as r:
            await open_console(r)
            await r.page.locator("#say-text").fill("Invented unconfirmed line")
            await r.page.evaluate("""() => {
                window.fixtureFetch=window.fetch;
                window.fetch=(path,options) => path==='/api/say' ? new Promise((resolve,reject) => {
                    window.refuseFixtureSay=() => reject(new Error('fixture disconnect'));
                }) : window.fixtureFetch(path,options);
            }""")
            await r.page.locator("#say-send").click()
            await r.page.wait_for_function("!!window.refuseFixtureSay")
            await r.page.evaluate("async () => (await import('/static/js/comms/feedview.mjs')).openView('officer')")
            await r.page.evaluate("window.refuseFixtureSay()")
            await expect(r.page.locator("body")).to_contain_text("Unconfirmed send to #guild")
            await expect(r.page.locator("body")).not_to_contain_text("Not sent to #guild")
            assert not r.errors,r.errors
    asyncio.run(run())
