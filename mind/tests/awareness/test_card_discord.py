from dataclasses import replace

import pytest

from awareness.test_card import populated, section, claim, context
from leeroy_mind.awareness import CardContext, card
from leeroy_mind.protocol import Chat, TextEmote
from leeroy_mind.text import estimate_tokens


def discord(clock, channel='discord:general', **kw):
    return CardContext(channel=channel, now=clock.now(), server=True, readers=(1,101,102),
        self_line="You're idle (at work), not playing WoW", here=('Beryl',), **kw)


def test_server_off_discord_card_is_unchanged(populated, clock):
    ctx = context(clock, channel='discord:general', readers=(101,102), purpose='Guild talk')
    before = card(populated,101,context=ctx).render()
    assert card(populated,101,context=replace(ctx,server=False,here=('Beryl',),self_line='hidden')).render() == before
    assert before == 'Conversation:\n- Channel: discord:general\n- Purpose: Guild talk'


def test_discord_card_has_here_and_guild_facts(populated, clock):
    result = card(populated,101,context=discord(clock))
    assert [p.key for p in result.parts][:2] == ['context','here']
    assert 'at work' in section(result,'here') and 'Beryl' in section(result,'here')
    assert '3 kills, 2 wipes' in section(result,'raid')
    assert 'Next raid' in section(result,'calendar')
    assert 'DKP: 20' in section(result,'standing')
    assert not section(result,'game') and not section(result,'bank')


@pytest.mark.parametrize('channel,role,bank', [
    ('discord:general','officer',False), ('discord:officer-chat','officer',True),
    ('discord:bank-requests','officer',True), ('discord:officer-chat','raider',False)])
def test_bank_only_in_officer_channels_for_officers(populated, clock, channel, role, bank):
    populated.conn.execute("UPDATE persona_core SET json=json_set(json,'$.play.guild_role',?) WHERE guid=101", (role,))
    result = card(populated,101,context=discord(clock,channel))
    assert bool(section(result,'bank')) is bank
    if role != 'officer':
        assert result.parts == ()


def test_dm_card_has_no_guild_bank(populated, clock):
    result = card(populated,101,context=discord(clock,'dm:101',to_guid=1))
    assert {p.key for p in result.parts} <= {'context','here','claims','drama'}
    assert not any(word in result.render(450) for word in ('Guild bank','DKP','Next raid','In game'))


def test_his_recent_emotes_follow_her_to_discord_only_with_server_on(populated, clock):
    populated.insert_game_event(TextEmote(0,1,'wave',101), session_id=None)
    populated.insert_game_event(TextEmote(0,102,'dance',101), session_id=None)
    ctx = discord(clock)
    result = card(populated,101,context=ctx).render()
    assert 'Directed at you: Emote wave from #1' in result
    assert 'Emote dance' not in result
    assert 'Emote wave' not in card(populated,101,context=replace(ctx,server=False)).render()


def test_card_fits_its_budget_dropping_from_the_end(populated, clock):
    result = card(populated,101,context=discord(clock))
    for budget in (100,450,600):
        rendered = result.render(budget)
        assert estimate_tokens(rendered) <= budget
        kept = [p for p in result.parts if p.render() in rendered]
        assert kept == list(result.parts[:len(kept)])


def test_game_card_shows_a_general_claim_with_server_on_only(populated, clock):
    mid = populated.insert_chat_line(Chat(0,'discord:general',101,'Arkon',0,'I like the invented ferry',0,0),
        session_id=None, origin='mind', audience=(1,101,102))
    claim(populated,'I like the invented ferry',source_line=mid)
    ctx = context(clock,channel='guild')
    assert 'invented ferry' not in card(populated,101,context=ctx).render()
    assert 'invented ferry' in card(populated,101,context=replace(ctx,server=True)).render()
