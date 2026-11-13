import asyncio
import pytest
from leeroy_mind.groups.config import GroupsConfig
from .test_tabs import open_console
from .test_flows import submit


@pytest.mark.parametrize('theme',['dark','light'])
def test_create_name_leave_group_mobile_keyboard(rehearsal,tmp_path,theme):
    async def run():
        from playwright.async_api import expect
        async with rehearsal(width=390,theme=theme) as r:
            r.app.groups.config=r.console.groups=GroupsConfig(enabled=True)
            await open_console(r)
            await r.page.get_by_role('button',name='New group',exact=True).click()
            await expect(r.page.locator('.group-dialog')).to_be_visible()
            await r.page.locator('#group-dialog-name').fill('Tea and raids')
            await r.page.locator('.group-dialog input[value="101"]').check()
            await r.page.locator('.group-dialog input[value="102"]').check()
            result=await submit(r,r.page.locator('.group-dialog button[type="submit"]'),'/api/discord')
            assert result['channel'].startswith('gdm:')
            await expect(r.page.locator('#dc-to')).to_have_text('Tea and raids')
            await r.page.locator('#dc-text').fill('Our tea is ready for the fixture raid.')
            await submit(r,r.page.locator('#dc-send'),'/api/discord')
            await expect(r.page.locator('#feed')).to_contain_text('Our tea is ready for the fixture raid.')
            await r.page.screenshot(path=str(tmp_path/f'group-{theme}.png'),full_page=True)
            assert await r.page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await r.page.get_by_role('button',name='Add members or rename').click()
            await r.page.locator('#group-dialog-name').fill('Tea friends')
            await r.page.locator('#group-dialog-name').press('Escape')
            await expect(r.page.locator('.group-dialog')).to_have_count(0)
            await r.page.get_by_role('button',name='Leave group',exact=True).click()
            await submit(r,r.page.locator('.group-dialog button[type="submit"]'),'/api/discord')
            await expect(r.page.locator('#dc-text')).to_be_disabled()
            await expect(r.page.locator('#dc-why')).to_contain_text('closed')
            assert not r.errors,r.errors
    asyncio.run(run())
