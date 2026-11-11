"""World cards use the real read-only endpoint, on the isolated fixture only."""
import asyncio

from leeroy_mind.world.protocol import WorldMember
from leeroy_mind.world.targets import save
from .test_tabs import open_console, visit


def test_world_filter_is_read_only_and_returns_to_cast_on_phone(rehearsal):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=390) as r:
            m = r.app.store.cast()[105]
            save(r.app.store,[WorldMember(m.guid,90,50,"Test rivals",m.name,
                m.race,m.class_,m.gender,24,m.rank,m.online,m.map,m.zone)],
                own=21,human=1,now=r.app.clock.now())
            with r.app.store.transaction():
                r.app.store.conn.execute("INSERT OR REPLACE INTO persona_tier VALUES(105,'world')")
            r.app.personas.invalidate(105)
            await open_console(r)
            await visit(r.page,"personas")
            toggle = r.page.locator("#p-world-filter")
            await expect(toggle).not_to_be_checked()
            await expect(r.page.locator("#p-world")).to_be_hidden()
            await toggle.check()
            panel = r.page.locator("#p-world")
            await expect(panel).to_contain_text(m.name + " · Test rivals · level 24")
            await panel.locator("summary").click()
            await expect(panel).to_contain_text("Character level: 24")
            await expect(panel.locator("input,select,textarea")).to_have_count(0)
            await expect(panel.get_by_role("button",name="Previous")).to_be_disabled()
            await expect(panel.get_by_role("button",name="Next")).to_be_disabled()
            await expect(r.page.locator("#p-tuning")).to_be_hidden()
            await expect(r.page.locator("#p-selfies")).to_be_hidden()
            assert await r.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            await toggle.uncheck()
            await expect(panel).to_be_hidden()
            await expect(r.page.locator("#p-tuning")).to_be_visible()
            await visit(r.page,"realm")
            await expect(r.page.locator("#realm-world")).to_contain_text(m.name)
            assert not r.errors,r.errors
    asyncio.run(run())
