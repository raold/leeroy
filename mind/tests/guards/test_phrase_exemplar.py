"""The phrase cooldown and her example lines ([talk] wow_grounding, talk/grounding.py): a draft
that copies words from one of her own earlier lines her prompt showed as an example is
`phrase_exemplar`, however long ago the line was said, and the retry is told the words."""
from datetime import datetime, timezone

import pytest

from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, review
from leeroy_mind.guards import phrases
from leeroy_mind.guards.review import HINTS, Reader, rewrite_hint

NOW = datetime(2026, 11, 20, 21, tzinfo=timezone.utc).timestamp()
EXAMPLE = 'tank swaps are always late on this boss'


def draft(text, **kw):
    return Draft(kw.pop('guid', 101), kw.pop('channel', 'guild'), text, now=kw.pop('now', NOW), **kw)


def found(store, d):
    return phrases.check(Reader(store.conn), d, d.now)


@pytest.mark.parametrize('text', [
    'tank swaps are always late lol',             # a four-word run of it
    'ugh tank swaps are always late on this boss',  # the whole line again
    'tank swaps are a pain',                      # three words, two of them content words
])
def test_copying_words_of_a_shown_example_is_flagged(store, text):
    assert 'phrase_exemplar' in found(store, draft(text, exemplars=(EXAMPLE,))).codes


@pytest.mark.parametrize('text', [
    'tank swaps took forever tonight',  # two words of it only
    'we need better swaps on this one',  # its words, never its run
    'no idea why we keep wiping',
])
def test_her_own_words_in_a_new_line_are_fine(store, text):
    assert 'phrase_exemplar' not in found(store, draft(text, exemplars=(EXAMPLE,))).codes


def test_without_examples_nothing_is_an_example_copy(store):
    assert 'phrase_exemplar' not in found(store, draft('tank swaps are always late lol')).codes


def test_the_conversations_subject_is_never_a_copy(store):
    with store.transaction():
        store.conn.execute(
            "INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) "
            "VALUES (?,?,?,?,?,?,?)", (NOW - 30, 'guild', 1, 'Zan', 0,
                                       'why are tank swaps always late', 'human'))
    assert 'phrase_exemplar' not in found(
        store, draft('tank swaps are always late lol', exemplars=(EXAMPLE,))).codes


def test_review_rewrites_with_the_words_named(store):
    verdict = review(GuardStores(store.conn, GuardsCfg(enabled=True)),
                     draft('honestly tank swaps are always late', exemplars=(EXAMPLE,)))
    assert verdict.action == 'rewrite' and 'phrase_exemplar' in verdict.codes
    assert verdict.phrases and all(p in EXAMPLE for p in verdict.phrases)
    hint = rewrite_hint(verdict.codes, verdict.phrases)
    assert HINTS['phrase_exemplar'] in hint and 'phrase_exemplar' not in hint


def test_the_phrase_cooldown_switch_turns_it_off(store):
    off = GuardsCfg(enabled=True, phrase_cooldown=False)
    verdict = review(GuardStores(store.conn, off),
                     draft('honestly tank swaps are always late', exemplars=(EXAMPLE,)))
    assert 'phrase_exemplar' not in verdict.codes


def test_the_code_is_a_phrase_code_the_pipeline_cuts():
    from leeroy_mind.talk.pipeline import CUT_CODES
    assert 'phrase_exemplar' in phrases.CODES and 'phrase_exemplar' in CUT_CODES
    assert phrases.strip('honestly tank swaps are always late, so lets just pull and see',
                         ('tank swaps are always late',)) == 'lets just pull and see'


@pytest.mark.parametrize('bad', [['a list'], ('x' * 5000,), tuple('abcdefghij'), (3,)])
def test_examples_are_validated(store, bad):
    with pytest.raises(ValueError, match='guard_exemplars_invalid'):
        review(GuardStores(store.conn, GuardsCfg(enabled=True)), draft('fine', exemplars=bad))


def test_examples_never_show_in_repr():
    assert EXAMPLE not in repr(draft('fine', exemplars=(EXAMPLE,)))
