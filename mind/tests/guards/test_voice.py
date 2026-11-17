"""A member's voice sheet in the guards (guards/voice.py, 2026-11-17): her own words are
rationed by the phrase cooldown instead of cooled, a word she never uses and a sample line said
again are soft rewrites, and an answer owed to him is never dropped for any of them. Every sheet
here is invented (tests/sheets.py)."""
from datetime import datetime, timezone
import json

import pytest

from conftest import seed_test_cast
from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, Verdict, review
from leeroy_mind.guards import phrases, voice
from leeroy_mind.guards.review import HARD_CODES, HINTS, Reader, rewrite_hint
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import CUT_CODES, TurnMeta
from sheets import LEDGER, give

from .test_live import rig, speak, turn  # noqa: F401  (the rig fixture)

NOW = datetime(2026, 10, 29, 23, tzinfo=timezone.utc).timestamp()
GOLD = {**LEDGER, "says": ["noted", "gold star", "fair enough", "right then"]}
SHEET = voice.sheet_of({"speech": GOLD})


def put(store, text, *, guid=101, channel='guild', to=0, ts=NOW - 60):
    with store.transaction():
        cur = store.conn.execute(
            'INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) '
            'VALUES (?,?,?,?,?,?,?)', (ts, channel, guid, 'Invented', to, text,
                                       'human' if guid == 1 else 'mind'))
    return cur.lastrowid


def draft(text, **kw):
    return Draft(kw.pop('guid', 101), kw.pop('channel', 'guild'), text, now=kw.pop('now', NOW), **kw)


def found(store, d, sheet=SHEET):
    return phrases.check(Reader(store.conn), d, d.now, sheet)


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    give(store, 101, GOLD)
    return store


# -- the sheet as the guards read it -------------------------------------------------------------

def test_the_sheet_of_a_stored_persona():
    assert SHEET.says == (('noted',), ('gold', 'star'), ('fair', 'enough'), ('right', 'then'))
    assert SHEET.never == (('awesome', ('awesome',), False), ('lol', ('lol',), False))
    assert SHEET.rationed == SHEET.says
    assert ('depends', 'what', 'did', 'the', 'log', 'say') in SHEET.samples
    for stored in (None, {}, {'speech': None}, {'speech': 'dry'}, {'speech': {'says': 'noted'}},
                   ['speech']):
        assert voice.sheet_of(stored) == voice.NONE
    assert 'noted' not in repr(SHEET)  # never in a log line


# -- her own words: rationed, not cooled ----------------------------------------------------------

def test_her_own_phrase_is_not_cooled_where_anyone_elses_would_be(store):
    for n in range(2):
        put(store, f'gold star, that makes {n}', channel='whisper', to=1, ts=NOW - 600 + n)
    d = draft('gold star for you, we go at eight', channel='whisper', to_guid=1)
    assert 'phrase_conversation' in found(store, d, None).codes       # no sheet: her tic
    assert found(store, d).codes == ('phrase_conversation',)          # hers, a third in three lines
    for n in range(4):
        put(store, f'plain line number {n} here', channel='whisper', to=1, ts=NOW - 500 + n)
    assert 'phrase_conversation' in found(store, d, None).codes       # still her tic without one
    assert found(store, d).codes == ()                                # her word: within ration


def test_her_own_phrase_is_free_of_the_day_and_the_per_member_stock_rules(store):
    for n in range(3):  # three lines today in other conversations
        put(store, f'gold star it works {n}', channel='guild', ts=NOW - 7200 - n * 2000)
    d = draft('gold star we are fine tonight', channel='whisper', to_guid=1)
    assert 'phrase_day' in found(store, d, None).codes
    assert found(store, d).codes == ()
    stock = voice.sheet_of({'speech': {**GOLD, 'says': ['sitting here', 'noted']}})
    put(store, 'sitting here with the dog', channel='guild', ts=NOW - 86400 * 2)
    again = draft('sitting here waiting on the tank')
    assert 'stock_member' in found(store, again, None).codes
    assert found(store, again, stock).codes == ()


def test_the_cast_rules_still_count_her_word(store):
    for guid in (102, 103, 104):
        put(store, 'gold star then lads', guid=guid, channel='discord:general', ts=NOW - 3600)
    result = found(store, draft('gold star then lads, eight it is'))
    assert 'phrase_cast' in result.codes


@pytest.mark.parametrize('uses,lines,over', [
    (0, 0, False), (1, 1, False),            # twice is never a tic
    (2, 2, True), (2, 5, True), (2, 6, False),   # a third use needs seven lines
    (3, 8, True), (3, 9, False),             # a fourth needs ten: about one in three
    (4, 9, True)])
def test_the_ration_is_about_one_line_in_three(uses, lines, over):
    assert voice.over_ration(uses, lines) is over


def test_a_word_over_its_ration_is_named_as_her_conversation_tic(store):
    for n in range(3):
        put(store, f'noted. try {n}' if n else 'noted, will do', channel='whisper', to=1,
            ts=NOW - 900 + n * 60)
    result = found(store, draft('noted. and the flask?', channel='whisper', to_guid=1))
    assert result.codes == ('phrase_conversation',) and result.phrases == ('noted',)
    assert "Don't say 'noted' again" in rewrite_hint(result.codes, result.phrases)
    # a single word is no phrase without a sheet: only her own words are rationed
    assert found(store, draft('noted. and the flask?', channel='whisper', to_guid=1), None).codes == ()


def test_the_ration_counts_only_this_conversation_and_her_last_lines(store):
    for n in range(4):  # another conversation: the guild's
        put(store, f'right then, pull {n}', channel='guild', ts=NOW - 900 + n * 60)
    assert found(store, draft('right then, you first', channel='whisper', to_guid=1)).codes == ()
    for n in range(2):  # twice long ago in this one, then nine lines without it
        put(store, f'right then, attempt {n}', channel='whisper', to=1, ts=NOW - 1700 + n)
    for n in range(9):
        put(store, f'plain line number {n} here', channel='whisper', to=1, ts=NOW - 1500 + n * 100)
    assert found(store, draft('right then, you first', channel='whisper', to_guid=1)).codes == ()


def test_a_tic_cannot_take_over_a_conversation(store):
    """The pilot saw one phrase in 6 of 10 replies: with the ration at most 4 of 10 pass."""
    passed = 0
    for n in range(10):
        text = f'fair enough, point {n} taken'
        d = draft(text, channel='whisper', to_guid=1, now=NOW + n * 60)
        if not found(store, d).codes:
            passed += 1
            put(store, text, channel='whisper', to=1, ts=NOW + n * 60)
        else:
            put(store, f'plain answer number {n}', channel='whisper', to=1, ts=NOW + n * 60)
    assert 2 <= passed <= 4


def test_a_phrase_is_hers_only_when_her_word_carries_it():
    assert voice.hers(('gold', 'star'), SHEET) and voice.hers(('gold', 'star', 'then'), SHEET)
    assert voice.hers(('a', 'gold', 'star', 'it', 'is'), SHEET) and voice.hers(('star',), SHEET)
    assert not voice.hers(('gold', 'star', 'mate'), SHEET)        # another content word
    assert not voice.hers(('absolute', 'menace'), SHEET) and not voice.hers((), voice.NONE)


# -- a word she never uses ------------------------------------------------------------------------

def test_a_never_word_is_a_soft_rewrite_that_names_it(cast):
    stores = GuardStores(cast.conn, GuardsCfg(enabled=True))
    verdict = review(stores, draft('that pull was awesome, Lol'))
    assert verdict.action == 'rewrite' and 'voice_never' in verdict.codes
    assert verdict.never == ('awesome', 'lol') and 'awesome' not in repr(verdict)
    hint = rewrite_hint(verdict.codes, verdict.phrases, verdict.pronouns, verdict.never)
    assert HINTS['voice_never'] in hint and hint.endswith("You never say 'awesome' or 'lol'.")
    assert review(stores, draft('that pull was fine, loll')).action == 'send'  # whole words only
    assert review(stores, draft('that pull was awesome', guid=102)).action == 'send'  # no sheet


def test_a_never_mark_without_words_is_matched_as_written():
    marks = voice.sheet_of({'speech': {**LEDGER, 'never': ['!!', 'no way']}})
    assert voice.never_used('what!! really', marks) == ('!!',)
    assert voice.never_used('there is No Way that works', marks) == ('no way',)
    assert voice.never_used('no. way too early', marks) == ('no way',)  # words, whatever between
    assert voice.never_used('what! really', marks) == () and voice.never_used('x', voice.NONE) == ()
    assert voice.hint(('!!', 'no way')) == "You never say '!!' or 'no way'." and voice.hint(()) == ''


def test_a_typing_shortcut_is_never_the_word_it_stands_for(store):
    """The cooldown folds "u" to "you" and "tbh" to "to be honest"; her sheet's own shortcuts
    must not make every "you" a never-word or a tic."""
    short = voice.sheet_of({'speech': {**LEDGER, 'never': ['u', 'ur', 'gonna'],
                                       'says': ['u', 'tbh', 'rn', 'noted']}})
    assert voice.never_used('are you sure your tank is going to pull', short) == ()
    assert voice.never_used('r u sure, ur tank is gonna pull', short) == ('u', 'ur', 'gonna')
    # "u" stands for one function word: no tic; "tbh" and "rn" fold to a run that is one
    assert short.rationed == (('to', 'be', 'honest'), ('right', 'now'), ('noted',))
    for n in range(4):
        put(store, f'you there? attempt {n}, to be honest', channel='whisper', to=1,
            ts=NOW - 900 + n * 60)
    result = found(store, draft('you coming or not, to be honest', channel='whisper', to_guid=1),
                   short)
    assert result.codes == ('phrase_conversation',) and result.phrases == ('to be honest',)
    assert found(store, draft('you coming or not', channel='whisper', to_guid=1), short).codes == ()


# -- a sample line said again ---------------------------------------------------------------------

def test_a_sample_copied_word_for_word_is_a_soft_rewrite(cast):
    stores = GuardStores(cast.conn, GuardsCfg(enabled=True))
    verdict = review(stores, draft('Depends... what did the log say? i was afk'))
    assert verdict.action == 'rewrite' and verdict.codes == ('voice_sample_copy',)
    assert HINTS['voice_sample_copy'] in rewrite_hint(verdict.codes)
    assert review(stores, draft('depends what the log said, i was afk')).action == 'send'


def test_a_short_sample_is_a_copy_only_as_the_whole_line():
    short = voice.sheet_of({'speech': {**LEDGER, 'samples': {'answer': 'not today'}}})
    assert voice.copies_sample('Not today.', short)
    assert not voice.copies_sample('not today, maybe friday', short)
    assert not voice.copies_sample('anything', voice.NONE)


def test_the_voice_codes_are_soft_and_never_cut_or_dropped():
    assert not set(voice.CODES) & HARD_CODES and not set(voice.CODES) & CUT_CODES
    assert all(code in HINTS for code in voice.CODES)


def test_switching_the_phrase_cooldown_off_keeps_the_voice_checks(cast):
    off = GuardStores(cast.conn, GuardsCfg(enabled=True, phrase_cooldown=False))
    assert review(off, draft('that pull was awesome')).codes == ('voice_never',)
    assert review(GuardStores(cast.conn, GuardsCfg(enabled=False)),
                  draft('that pull was awesome')).action == 'send'


def test_review_passes_her_sheet_to_the_cooldown(cast):
    for guid in (101, 102):
        for n in range(2):
            put(cast, f'gold star, that makes {n}', guid=guid, channel='whisper', to=1,
                ts=NOW - 600 + n)
        for n in range(4):
            put(cast, f'plain line number {n} here', guid=guid, channel='whisper', to=1,
                ts=NOW - 500 + n)
    stores = GuardStores(cast.conn, GuardsCfg(enabled=True))
    mine = review(stores, draft('gold star for you, we go at eight', channel='whisper', to_guid=1))
    assert mine.codes == () and mine.action == 'send'
    other = review(stores, draft('gold star for you, we go at eight', guid=102, channel='whisper',
                                 to_guid=1))  # 102 has no sheet: the cooldown as before
    assert other.codes == ('phrase_conversation', 'phrase_day')


# -- the pipeline ---------------------------------------------------------------------------------

def never_review(stores, d):
    if 'awesome' in d.text:
        return Verdict('rewrite', ('voice_never',), never=('awesome',))
    return Verdict('send')


def test_the_retry_is_told_the_word_she_never_uses(rig, monkeypatch):
    monkeypatch.setattr(live, 'review', never_review)
    rig.script('awesome pull that', 'tidy pull. no notes')
    sent, meta = speak(rig, turn())
    assert sent and 'awesome' not in ' '.join(sent) and meta.guard_rewritten
    assert "You never say 'awesome'." in rig.last_user()


def test_a_bystander_line_that_keeps_the_word_is_dropped(rig, monkeypatch):
    monkeypatch.setattr(live, 'review', never_review)
    rig.script('awesome pull that', 'still awesome though')
    sent, meta = speak(rig, turn())
    assert not sent and meta.guard_verdict == 'drop'


@pytest.mark.parametrize('channel', ['whisper', 'dm:101'])
@pytest.mark.parametrize('code,kw', [('voice_never', {'never': ('awesome',)}),
                                     ('voice_sample_copy', {})])
def test_an_owed_answer_is_never_dropped_for_her_sheet(rig, monkeypatch, channel, code, kw):
    def refuse(stores, d):
        return Verdict('rewrite', (code,), **kw)
    monkeypatch.setattr(live, 'review', refuse)
    rig.script('awesome, the first draft', 'awesome again, the second draft')
    sent, meta = speak(rig, turn(channel), TurnMeta('owed', why='reply:named', answers_human=True,
                                                     whisper_reply=channel == 'whisper'))
    assert len(sent) == 1 and 'draft' in sent[0] and meta.guard_rewritten
    assert code in meta.guard_codes
