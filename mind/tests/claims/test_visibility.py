import pytest

from leeroy_mind.claims.visibility import audience_class, claim_visible
from leeroy_mind.guards import Draft
from leeroy_mind.guards.review import _line, _visible


@pytest.mark.parametrize('source,target,ok', [
    ('guild','discord:general',True), ('discord:general','guild',True),
    ('discord:officer-chat','officer',True), ('officer','discord:officer-chat',True),
    ('officer','discord:general',False), ('discord:officer-chat','guild',False),
    ('whisper','dm:101',True), ('dm:101','whisper',True), ('dm:101','guild',False),
    ('discord:general','dm:101',True), ('party','discord:general',False),
    ('discord:missing','guild',False),
])
def test_claims_cross_medium_only_within_the_same_audience(source, target, ok):
    assert claim_visible(source_channel=source, source_from=101, source_to=1,
        source_readers=frozenset({1,101,102}), draft_channel=target, speaker=101,
        to_guid=1, officer=True, cross_medium=True) is ok


@pytest.mark.parametrize('source', ['guild','officer','whisper','party','say','discord:general','discord:officer-chat','dm:101','voice:officers'])
@pytest.mark.parametrize('target', ['guild','officer','whisper','party','discord:general','dm:101'])
def test_cross_medium_off_is_todays_guard_rule(source, target):
    def line(channel):
        return _line(dict(id=1,ts=1,from_guid=101,to_guid=1,channel=channel,
                         origin='mind',text='',audience='[1,101,102]'), {})
    previous, current = line(source), line(target)
    draft = Draft(101, current.channel, '', to_guid=1, room=current.room)
    assert claim_visible(source_channel=source, source_from=101, source_to=1,
        source_readers=frozenset({1,101,102}), draft_channel=target, speaker=101,
        to_guid=1, officer=True, cross_medium=False) == _visible(previous, draft, True)


@pytest.mark.parametrize('source,target,readers,officer,to', [
    ('discord:general','guild',None,True,1),
    ('discord:general','guild',frozenset({102}),True,1),
    ('officer','discord:officer-chat',None,False,1),
    ('dm:101','whisper',frozenset({1,101}),True,102),
    ('whisper','dm:102',None,True,102),
])
def test_missing_audience_wrong_pair_and_nonofficer_are_refused(source,target,readers,officer,to):
    assert not claim_visible(source_channel=source,source_from=101,source_to=1,
        source_readers=readers,draft_channel=target,speaker=101,to_guid=to,
        officer=officer,cross_medium=True)


def test_unknown_and_local_channels_are_not_public():
    assert audience_class('discord:unknown', readers=frozenset({101})) == 'other'
    assert audience_class('say', readers=frozenset({101})) == 'other'
