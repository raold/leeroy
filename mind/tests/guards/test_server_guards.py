from dataclasses import replace
import importlib

import pytest

from conftest import seed_test_cast, unit
from guards.test_review import NOW, sources
from leeroy_mind.guards import Draft, HistoryLine, review
from leeroy_mind.guards.config import parse_config
from leeroy_mind.storage.rows import pack_vec

TEXT = 'The invented ferry runs from the harbor each morning'
REVIEW = importlib.import_module('leeroy_mind.guards.review')  # the module; `review` is its function


@pytest.fixture
def reacting(monkeypatch):
    """Every filler reply becomes its reaction (the rest of the time it is posted, below)."""
    monkeypatch.setattr(REVIEW, 'FILLER_REACT_SHARE', 1.0)


@pytest.fixture
def posting(monkeypatch):
    """Every filler reply is posted as its short message."""
    monkeypatch.setattr(REVIEW, 'FILLER_REACT_SHARE', 0.0)


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
def test_server_filler_becomes_its_reaction(store,text,emoji,reacting):
    seed_test_cast(store)
    result = review(sources(store,history=(old('What a lovely sunrise',guid=102,room='general'),)),draft(text,parent_id=10))
    assert result.action == 'react' and result.reaction == emoji


@pytest.mark.parametrize('text', ['same','lmao','gz','rip','thanks','+1','lol xD'])
def test_server_filler_can_post_as_its_short_message(store,text,posting):
    """A short reply is a real message too: no reaction, no repeat, nothing-new or relevance
    code, though the channel said the same today and it shares no words with its parent."""
    seed_test_cast(store)
    vector = pack_vec(unit(0))
    history = (old('What a lovely sunrise',guid=102,room='general',embedding=pack_vec(unit(3))),
               replace(old(text,guid=103,room='general',embedding=vector),id=11),
               replace(old(text,room='irl',embedding=vector),id=12))
    result = review(sources(store,history=history),draft(text,parent_id=10,embedding=vector))
    assert result.action == 'send' and result.codes == () and result.reaction is None
    assert any(s.code == 'filler_post' for s in result.scores)


def test_about_four_in_ten_filler_replies_react_and_the_draw_is_stable(store):
    """The share is drawn per speaker, message answered and words, never the clock: the
    recheck before sending gives the same answer."""
    seed_test_cast(store)
    source = sources(store)
    outcomes = []
    for parent in range(1, 401):
        first = review(source,draft('lol',parent_id=parent))
        again = review(source,replace(draft('lol',parent_id=parent),now=NOW+30))
        assert first.action == again.action
        outcomes.append(first.action)
    assert set(outcomes) == {'react','send'}
    assert .3 < outcomes.count('react')/len(outcomes) < .5


@pytest.mark.parametrize('text,emoji', [('lol :P','😂'),('gz :D','🎉'),('lol xD','😂'),
    ('same :)','👍'),('oof :(','🫡'),('ty <3','❤️'),('lol:P','😂'),
    ('lol :P haha :D hehe xD kek <3','😂')])
def test_ascii_emoticons_are_neutral_for_server_filler(store,text,emoji,reacting):
    seed_test_cast(store)
    source = sources(store,history=(old('What a lovely sunrise',guid=102,room='general'),))
    result = review(source,draft(text,parent_id=10))
    assert result.action=='react' and result.reaction==emoji


@pytest.mark.parametrize('text', ['lol haha hehe kek lmao :P','lol the ferry left :D',
    ':P :D xD :) :( <3','lol :plain','lol xdimension'])
def test_emoticons_do_not_hide_substantive_words_or_extend_filler_limit(store,text):
    seed_test_cast(store)
    result = review(sources(store),draft(text))
    assert 'server_filler' not in result.codes and 'filler_opener' not in result.codes


def test_ascii_emoticon_change_is_server_only(store):
    seed_test_cast(store)
    source = sources(store,history=(old('What a lovely sunrise',guid=102,room='general'),))
    result = review(source,replace(draft('lol :P',parent_id=10),server=False))
    assert result.action=='send' and 'server_filler' not in result.codes


@pytest.mark.parametrize('share', [0.0, 1.0])
def test_an_opener_that_is_filler_drops(store,monkeypatch,share):
    monkeypatch.setattr(REVIEW, 'FILLER_REACT_SHARE', share)
    seed_test_cast(store)
    result = review(sources(store),draft('lol'))
    assert result.action == 'drop' and 'filler_opener' in result.codes


def test_repeated_filler_still_becomes_a_reaction(store,reacting):
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
