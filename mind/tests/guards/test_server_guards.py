from dataclasses import replace

import pytest

from conftest import seed_test_cast, unit
from guards.test_review import NOW, sources
from leeroy_mind.guards import Draft, HistoryLine, review
from leeroy_mind.guards.config import parse_config
from leeroy_mind.storage.rows import pack_vec

TEXT = 'The invented ferry runs from the harbor each morning'


def draft(text=TEXT, **kw):
    return Draft(101,'discord',text,room='general',now=NOW,server=True,**kw)


def old(text=TEXT, *, guid=101, room='irl', channel='discord', age=60, **kw):
    return HistoryLine(10,NOW-age,guid,text,channel=channel,room=room,readers=(1,101,102),**kw)


def test_own_repeat_across_discord_channels_drops(store):
    seed_test_cast(store)
    verdict = review(sources(store,history=(old(),)),draft())
    assert verdict.action == 'drop' and 'own_repeat_discord' in verdict.codes


@pytest.mark.parametrize('age,drop', [(86399,True),(86401,False)])
def test_channel_repeat_within_a_day_drops(store,age,drop):
    seed_test_cast(store)
    verdict = review(sources(store,history=(old(guid=102,room='general',age=age),)),draft())
    assert ('channel_repeat' in verdict.codes) is drop


@pytest.mark.parametrize('guid,drop', [(101,True),(102,False)])
def test_dm_lines_compare_only_for_her_own_repeats(store,guid,drop):
    seed_test_cast(store)
    verdict = review(sources(store,history=(old(guid=guid,channel='dm',room=str(guid),to_guid=1),)),draft())
    assert (verdict.action == 'drop') is drop
    assert not any(s.code in ('parent_relevance','thread_relevance') for s in verdict.scores)


def test_off_anchor_opener_rewrites(store):
    seed_test_cast(store)
    result = review(sources(store),draft(anchor='Raid planning',embedding=pack_vec(unit(0)),anchor_embedding=pack_vec(unit(1))))
    assert result.action == 'rewrite' and result.codes == ('off_anchor',)


@pytest.mark.parametrize('text,emoji', [('same','👍'),('lmao','😂'),('gz','🎉'),('rip','🫡'),('thanks','❤️'),('+1','👍')])
def test_server_filler_becomes_its_reaction(store,text,emoji):
    seed_test_cast(store)
    result = review(sources(store,history=(old('What a lovely sunrise',guid=102,room='general'),)),draft(text,parent_id=10))
    assert result.action == 'react' and result.reaction == emoji


def test_an_opener_that_is_filler_drops(store):
    seed_test_cast(store)
    result = review(sources(store),draft('lol'))
    assert result.action == 'drop' and 'filler_opener' in result.codes


def test_repeated_filler_still_becomes_a_reaction(store):
    seed_test_cast(store)
    vector = pack_vec(unit(0))
    result = review(sources(store,history=(old('same',room='general',embedding=vector),)),
                    draft('same',parent_id=10,embedding=vector))
    assert result.action == 'react' and result.reaction == '👍'


def test_non_server_turns_keep_todays_rules(store):
    seed_test_cast(store)
    source = sources(store,history=(old(),))
    assert review(source,replace(draft(),server=False)).action == 'send'
    assert review(source,replace(draft('same'),server=False)).action == 'send'
    assert review(source,replace(draft(anchor='Raid planning',embedding=pack_vec(unit(0)),
        anchor_embedding=pack_vec(unit(1))),server=False)).action == 'send'


def test_semantic_repeats_are_forced_on_for_server_only(store):
    seed_test_cast(store)
    source = sources(store,history=(old('A different lexical line',embedding=pack_vec(unit(0))),))
    assert review(source,draft(embedding=pack_vec(unit(0)))).action == 'drop'
    assert review(source,replace(draft(embedding=pack_vec(unit(0))),server=False)).action == 'send'


@pytest.mark.parametrize('value,enabled', [(0,True),(1,True),(.2,True),(-.1,False),(1.1,False),(True,False)])
def test_anchor_threshold_is_validated(value,enabled):
    assert parse_config({'enabled':True,'anchor_cosine':value}).enabled is enabled
