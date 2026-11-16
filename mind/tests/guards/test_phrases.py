"""The phrase cooldown (owner's Turing-test push, 2026-11-15): a member's 2-5 word phrases and
the stock list are rationed per conversation, per day and across the cast; the retry is told the
phrase in her own words, and a retry that keeps it loses the phrase, or the line."""
from datetime import datetime, timezone
import json

import pytest

from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, Verdict, parse_config, review
from leeroy_mind.guards import phrases
from leeroy_mind.guards.review import Reader, rewrite_hint
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import TurnMeta

from .test_live import rig, speak, turn  # noqa: F401  (the rig fixture)

NOW = datetime(2026, 10, 29, 23, tzinfo=timezone.utc).timestamp()
MENACE = 'send it now you absolute menace'


def put(store, text, *, guid=101, channel='guild', to=0, ts=NOW - 60, audience=None):
    with store.transaction():
        cur = store.conn.execute(
            'INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin,audience) '
            'VALUES (?,?,?,?,?,?,?,?)', (ts, channel, guid, 'Invented', to, text,
                                         'human' if guid == 1 else 'mind',
                                         json.dumps(audience) if audience is not None else None))
    return cur.lastrowid


def draft(text, **kw):
    return Draft(kw.pop('guid', 101), kw.pop('channel', 'guild'), text, now=kw.pop('now', NOW), **kw)


def found(store, d):
    return phrases.check(Reader(store.conn), d, d.now)


# -- words ---------------------------------------------------------------------------------------

def test_words_fold_the_casts_typing():
    assert [t.norm for t in phrases.tokens("u're gonna lose ur mind rn")] == \
        ['youre', 'going', 'to', 'lose', 'your', 'mind', 'right', 'now']
    assert [t.norm for t in phrases.tokens('finaly got it, jsut gonna loose my mind ngl')] == \
        ['finally', 'got', 'it', 'just', 'going', 'to', 'lose', 'my', 'mind', 'not', 'going', 'to', 'lie']
    toks = phrases.tokens('Bring the {{item:42}} to Naxx, Arkon. Then rest')
    assert [t.norm for t in toks] == ['bring', 'the', 'to', 'naxx', 'arkon', 'then', 'rest']
    assert [t.clause for t in toks] == [0, 0, 1, 1, 2, 3, 3]
    assert [t.proper for t in toks] == [False, False, False, True, True, False, False]


@pytest.mark.parametrize('text,phrase,eligible', [
    ('you absolute menace', ('absolute', 'menace'), True),
    ('i want to', ('i', 'want', 'to'), False),                  # nothing outside STOP
    ('im sitting here', ('sitting', 'here'), True),
    ('we got over here', ('over', 'here'), False),             # two words need two outside STOP
    ('hey rabbi you there', ('hey', 'rabbi', 'you'), False),   # his name
    ('the Lich King again', ('lich', 'king', 'again'), False), # a proper noun
    ('we see the lich king', ('the', 'lich', 'king'), False),  # a game name typed in lowercase
    ('wipe in icecrown citadel again', ('icecrown', 'citadel', 'again'), False),
    ('death of me', ('death', 'of', 'me'), True),
])
def test_eligible_phrases(text, phrase, eligible):
    _, found_ = phrases.candidates(text, frozenset({('rabbi',)}) | phrases.game_names())
    assert (phrase in found_) is eligible


def test_lowercase_game_names_are_never_worn_phrases(store):
    for guid in (102, 103, 104):
        put(store, 'we are so close to the lich king', guid=guid, channel='discord:general', ts=NOW - 3600)
    assert found(store, draft('one more pull and we see the lich king')).codes == ()


def test_stock_list_loads_and_covers_the_audit():
    names = {name for name, _ in phrases.stock()}
    assert {'menace', 'death of me', 'lose my mind', 'sitting here', 'staring at', 'cry for help',
            'there he is', 'at least'} <= names
    anchored = dict(phrases.stock())['at least']
    assert anchored == ((True, ('at', 'least')),)
    raw = (phrases.resources.files('leeroy_mind.guards') / 'stock_phrases.txt').read_text(encoding='utf-8')
    assert 'How to extend' in raw and '\r' not in raw


# -- the rules -----------------------------------------------------------------------------------

def test_same_conversation_flags_a_second_use(store):
    put(store, 'god you are such an absolute menace', channel='whisper', to=1, ts=NOW - 600)
    result = found(store, draft(MENACE, channel='whisper', to_guid=1))
    assert 'phrase_conversation' in result.codes and 'absolute menace' in result.phrases


def test_a_whisper_and_her_dm_with_him_are_one_conversation(store):
    put(store, 'god you are such an absolute menace', channel='dm:101', to=1, ts=NOW - 600)
    assert 'phrase_conversation' in found(store, draft(MENACE, channel='whisper', to_guid=1)).codes


@pytest.mark.parametrize('channel,to,ts', [('whisper', 102, NOW - 600), ('guild', 0, NOW - 600),
                                           ('whisper', 1, NOW - 31 * 60)])
def test_another_conversation_or_an_old_one_needs_the_daily_count(store, channel, to, ts):
    put(store, 'god you are such an absolute menace', channel=channel, to=to, ts=ts)
    codes = found(store, draft('so proud of you, absolute menace', channel='whisper', to_guid=1)).codes
    assert 'phrase_conversation' not in codes and 'phrase_day' not in codes


@pytest.mark.parametrize('channel,who,to,mine', [
    ('whisper', 1, 101, dict(channel='whisper', to_guid=1)),         # his whisper to her
    ('dm:101', 1, 101, dict(channel='dm', room='101', to_guid=1)),  # his DM to her
    ('guild', 102, 0, dict(channel='guild'))])                       # another member here
def test_a_phrase_someone_else_in_the_conversation_used_is_its_topic(store, channel, who, to, mine):
    # twice: one earlier use in the conversation is no tic (owner's live DM test, 2026-11-16)
    put(store, 'grilled cheese with sharp cheddar wins', channel=channel, to=1 if to else 0, ts=NOW - 1200)
    put(store, 'nothing beats grilled cheese', channel=channel, to=1 if to else 0, ts=NOW - 900)
    d = draft('grilled cheese forever', **mine)
    assert 'phrase_conversation' in found(store, d).codes
    put(store, 'grilled cheese is the only answer', guid=who, channel=channel, to=to, ts=NOW - 600)
    assert 'phrase_conversation' not in found(store, d).codes


def test_the_third_use_in_a_day_anywhere_is_flagged(store):
    put(store, 'that absolute menace again', channel='guild', ts=NOW - 5 * 3600)
    put(store, 'what an absolute menace', channel='discord:general', ts=NOW - 3 * 3600)
    result = found(store, draft('you absolute menace', channel='whisper', to_guid=1))
    assert 'phrase_day' in result.codes
    # 20 h later the first is 25 h old: one use left in the day
    assert 'phrase_day' not in found(store, draft('you absolute menace', now=NOW + 20 * 3600)).codes


def test_three_other_members_make_a_phrase_worn(store):
    for guid in (102, 103):
        put(store, 'that is a cry for help honestly', guid=guid, ts=NOW - 3600)
    d = draft('this spreadsheet is a cry for help lol', channel='discord', room='general')
    assert 'phrase_cast' not in found(store, d).codes
    put(store, 'a cry for help if i ever saw one', guid=104, ts=NOW - 40 * 3600)
    assert 'phrase_cast' in found(store, d).codes
    assert 'phrase_cast' not in found(store, draft('a cry for help', now=NOW + 9 * 3600)).codes


def test_the_conversations_topic_is_never_worn_either(store):
    # three members in this guild talk just said it: naming the subject is no tic (review 326, P2-2)
    for guid, ago in ((102, 600), (103, 420), (104, 180)):
        put(store, 'did anyone read the new patch notes', guid=guid, channel='guild', ts=NOW - ago)
    assert 'phrase_cast' not in found(store, draft('the new patch notes nerfed my class')).codes
    # said elsewhere, it is the cast's worn phrase as before
    assert 'phrase_cast' in found(store, draft('the new patch notes nerfed my class', channel='party')).codes


def test_an_ordinary_idiom_is_never_worn_across_the_cast(store):
    for guid in (102, 103, 104, 105):
        put(store, f'{guid} games in a row', guid=guid)
    assert found(store, draft('three wipes in a row')).codes == ()


def test_stock_phrases_once_per_member_in_three_days(store):
    put(store, 'you will be the death of me', channel='whisper', to=1, ts=NOW - 70 * 3600)
    result = found(store, draft('rabbi, the death of me, i swear', channel='guild'))
    assert 'stock_member' in result.codes and 'death of me' in result.phrases
    assert 'stock_member' not in found(store, draft('the death of me', now=NOW + 3 * 3600)).codes


def test_stock_phrases_three_times_a_day_across_the_cast(store):
    for guid in (102, 103, 104):
        put(store, 'staring at the ceiling again', guid=guid, ts=NOW - 3600)
    assert 'stock_cast' in found(store, draft('just staring at my phone')).codes
    assert found(store, draft('just staring at my phone', now=NOW + 86400)).codes == ()


def test_an_anchored_stock_phrase_is_only_a_clause_opener(store):
    put(store, 'at least you get paid for it', ts=NOW - 3600)
    assert 'stock_member' in found(store, draft('rough day. at least the boss died')).codes
    assert 'stock_member' not in found(store, draft('we need at least ten for the run')).codes


def test_names_never_make_a_phrase(store):
    with store.transaction():
        store.conn.execute("INSERT INTO meta(key,value) VALUES ('human_name','Rabbi')")
    put(store, 'hey rabbi hey rabbi', channel='whisper', to=1, ts=NOW - 60)
    assert found(store, draft('hey rabbi', channel='whisper', to_guid=1)).codes == ()


def test_replay_excludes_the_line_itself_and_later_rows(store):
    first = put(store, 'god you are such an absolute menace', channel='whisper', to=1, ts=NOW - 600)
    second = put(store, MENACE, channel='whisper', to=1, ts=NOW - 300)
    assert found(store, draft(MENACE, channel='whisper', to_guid=1, line_id=first)).codes == ()
    assert found(store, draft(MENACE, channel='whisper', to_guid=1, line_id=second)).codes


def test_group_dm_lines_count_but_are_never_quoted(store):
    put(store, 'SECRETWORD absolute menace SECRETWORD', channel='gdm:7', ts=NOW - 600, audience=[1, 101, 102])
    put(store, 'another absolute menace moment', channel='guild', ts=NOW - 3600)
    d = draft('you absolute menace', channel='discord', room='general')
    verdict = review(GuardStores(store.conn, GuardsCfg(enabled=True)), d)
    assert 'phrase_day' in verdict.codes and verdict.action == 'rewrite'
    hint = rewrite_hint(verdict.codes, verdict.phrases)
    assert verdict.phrases == ('absolute menace',) and 'SECRETWORD' not in hint + repr(verdict)
    assert 'phrase_conversation' in found(store, draft('the absolute menace', channel='group', room='7')).codes


# -- review and config ---------------------------------------------------------------------------

def test_review_rewrites_and_keeps_its_words_out_of_repr(store):
    put(store, 'god you are such an absolute menace', channel='whisper', to=1, ts=NOW - 600)
    verdict = review(GuardStores(store.conn, GuardsCfg(enabled=True)), draft(MENACE, channel='whisper', to_guid=1))
    assert verdict.action == 'rewrite' and 'phrase_conversation' in verdict.codes
    assert 'menace' not in repr(verdict) and verdict.phrases == ('absolute menace',)


def test_a_whole_line_repeat_stays_a_drop(store):
    put(store, MENACE, ts=NOW - 60)
    verdict = review(GuardStores(store.conn, GuardsCfg(enabled=True)), draft(MENACE))
    assert verdict.action == 'drop' and not set(verdict.codes) & set(phrases.CODES)


def test_switch_off_and_config_parsing(store):
    put(store, 'god you are such an absolute menace', channel='whisper', to=1, ts=NOW - 600)
    off = GuardsCfg(enabled=True, phrase_cooldown=False)
    assert review(GuardStores(store.conn, off), draft(MENACE, channel='whisper', to_guid=1)).action == 'send'
    assert parse_config({'enabled': True, 'phrase_cooldown': False}).phrase_cooldown is False
    assert parse_config({'enabled': True}).phrase_cooldown is True
    assert parse_config({'enabled': True, 'phrase_cooldown': 1}) == GuardsCfg()


def test_hint_names_her_words_in_quotes():
    hint = rewrite_hint(('phrase_day', 'stock_member'), ('absolute menace', 'death of me'))
    assert "Don't say 'absolute menace' or 'death of me' again; say it your own way or not at all." in hint
    assert 'phrase_day' not in hint and hint.startswith('Rewrite your reply once in your own words.')
    assert rewrite_hint(('nothing_new',)) == ('Rewrite your reply once in your own words. Your draft has '
                                              'a problem: it only repeats what was already said')


# -- strip ---------------------------------------------------------------------------------------

@pytest.mark.parametrize('text,phrase,expected', [
    (MENACE, 'absolute menace', 'send it now'),
    (MENACE, 'menace', 'send it now'),
    ('doing it on purpose, you absolute menace!', 'absolute menace', 'doing it on purpose!'),
    ('God, you are such a menace, doing it on purpose. I love it though.', 'menace',
     'God, doing it on purpose. I love it though.'),
    ("you're gonna be the death of me. but fine, ill bring the flasks", 'death of me',
     'fine, ill bring the flasks'),                              # the "but" left hanging goes too
    ("sexy back at you. come here and i'll show you exactly what that means.", 'what that means',
     'sexy back at you.'),
    ('first part stays.\n\nI am sitting here at my desk. thinking about tonight\nlast line',
     'sitting here', 'first part stays.\n\nthinking about tonight\nlast line'),
    ('the death of me', 'death of me', None),
    ('lol ;) at least you tried', 'at least', None),
    # review 326: a cut across but/if/unless/though/because would reverse the line
    ("that sounds really fun, but i'd rather stay in tonight lol", "i'd rather", None),
    ("I'd love to come, but at this point I'm way too tired", 'at this point', None),
    ("Of course I'll come, if you stop staring at me like that", 'staring at', None),
    ('i would never say that, at least not to his face', 'at least', 'i would never say that'),
    ("i swear i got it. lol you're so impatient", 'i swear', "lol you're so impatient"),
    ('honestly i swear the boss bugged out, we wiped twice', 'i swear', 'we wiped twice'),
    ('no. i swear i never told her. you believe me right?', 'i swear', 'no. you believe me right?'),
    ("you're trouble, but yes, i'll come", 'youre trouble', "yes, i'll come"),
    ('it was good, how was your day?', 'how was your day', 'it was good.'),  # never "it was good?"
    # an anchored entry is cut only where a clause opens with it: "at least ten" is not the entry
    ('at least we tried. we need at least ten for the run though', 'at least',
     'we need at least ten for the run though'),
])
def test_strip_cuts_the_epithet_clause_or_sentence(text, phrase, expected):
    assert phrases.strip(text, [phrase]) == expected


@pytest.mark.parametrize('text,phrase,expected', [
    (MENACE, 'absolute menace', 'send it now'),
    ('doing it on purpose, you absolute menace!', 'absolute menace', 'doing it on purpose!'),
    ("that sounds really fun, but i'd rather stay in tonight lol", "i'd rather", None),
    ("i swear i got it. lol you're so impatient", 'i swear', None),
    ('it was good, how was your day?', 'how was your day', None),
])
def test_an_owed_answer_only_loses_a_closing_epithet(text, phrase, expected):
    assert phrases.strip(text, [phrase], epithet_only=True) == expected


def test_strip_keeps_emoticons():
    assert phrases.strip('ugh :) you absolute menace. bring the flasks ;)', ['absolute menace']) == \
        'bring the flasks ;)'


# -- the pipeline --------------------------------------------------------------------------------

def menace_review(stores, d):
    if 'menace' in d.text:
        return Verdict('rewrite', ('phrase_day',), phrases=('absolute menace',))
    return Verdict('send')


def test_the_retry_is_told_the_phrase(rig, monkeypatch):
    monkeypatch.setattr(live, 'review', menace_review)
    rig.script(MENACE, 'fine, i will wait by the bank')
    sent, meta = speak(rig, turn())
    assert sent and 'menace' not in ' '.join(sent)
    assert "Don't say 'absolute menace' again" in rig.last_user() and meta.guard_rewritten


def test_a_retry_that_keeps_the_phrase_loses_it(rig, monkeypatch):
    monkeypatch.setattr(live, 'review', menace_review)
    rig.script(MENACE, 'fine, the raid waits for nobody. you absolute menace')
    sent, meta = speak(rig, turn())
    assert sent and 'menace' not in ' '.join(sent).casefold()
    assert 'raid waits for nobody' in ' '.join(sent) and len(rig.chat_calls()) == 2


def test_a_retry_with_nothing_else_is_dropped(rig, monkeypatch):
    monkeypatch.setattr(live, 'review', menace_review)
    rig.script(MENACE, 'absolute menace')
    sent, meta = speak(rig, turn())
    assert not sent and meta.guard_verdict == 'drop'
    assert rig.utterances()[-1]['dropped'] == 'guard_phrase_refused'


@pytest.mark.parametrize('channel', ['whisper', 'dm:101'])
def test_an_owed_answer_is_never_dropped_for_a_phrase(rig, monkeypatch, channel):
    monkeypatch.setattr(live, 'review', menace_review)
    rig.script('the first draft, absolute menace', 'absolute menace, yes')
    sent, meta = speak(rig, turn(channel), TurnMeta('owed', why='reply:named', answers_human=True,
                                                     whisper_reply=channel == 'whisper'))
    assert len(sent) == 1 and 'yes' in sent[0] and 'first draft' not in sent[0]
    assert meta.guard_rewritten


def rather_review(stores, d):
    if 'rather' in d.text:
        return Verdict('rewrite', ('stock_member',), phrases=("i'd rather",))
    return Verdict('send')


RATHER = "that sounds really fun, but i'd rather stay in tonight lol"


def test_an_owed_answer_keeps_its_clause_and_goes_as_written(rig, monkeypatch):
    # review 326, P2-1: "that sounds really fun" alone would say the opposite to him
    monkeypatch.setattr(live, 'review', rather_review)
    rig.script("i'd rather not tonight", RATHER)
    sent, meta = speak(rig, turn('whisper'), TurnMeta('owed', why='reply:named', answers_human=True,
                                                       whisper_reply=True))
    assert len(sent) == 1 and 'rather' in sent[0] and 'really fun' in sent[0]
    assert meta.guard_verdict == 'rewrite' and len(rig.chat_calls()) == 2


def test_an_owed_answer_still_loses_a_closing_epithet(rig, monkeypatch):
    monkeypatch.setattr(live, 'review', menace_review)
    rig.script('the first draft, absolute menace', 'doing it on purpose, you absolute menace!')
    sent, meta = speak(rig, turn('whisper'), TurnMeta('owed', why='reply:named', answers_human=True,
                                                       whisper_reply=True))
    assert len(sent) == 1 and 'menace' not in sent[0] and 'on purpose' in sent[0]


def test_a_bystander_line_is_dropped_rather_than_reversed(rig, monkeypatch):
    monkeypatch.setattr(live, 'review', rather_review)
    rig.script("i'd rather not tonight", RATHER)
    sent, meta = speak(rig, turn())
    assert not sent and rig.utterances()[-1]['dropped'] == 'guard_phrase_refused'


@pytest.mark.parametrize('owed', [True, False])
def test_the_strips_review_never_expires_an_owed_answer(rig, monkeypatch, owed):
    monkeypatch.setattr(live, 'review', menace_review)
    import sys
    def expired(deadline, **kw):  # the deadline lapses exactly during the strip's re-review
        return sys._getframe(1).f_code.co_name == '_without_phrases'
    monkeypatch.setattr(rig.engine.scheduler, 'expired', expired)
    rig.script('the first draft, absolute menace', 'doing it on purpose, you absolute menace!')
    meta = (TurnMeta('owed', why='reply:named', answers_human=True, whisper_reply=True) if owed
            else TurnMeta('bystander', why='test'))
    sent, meta = speak(rig, turn('whisper' if owed else 'guild'), meta)
    if owed:
        assert len(sent) == 1 and 'on purpose' in sent[0]
    else:
        assert not sent and rig.utterances()[-1]['dropped'] == 'expired'


def test_a_disabled_guards_table_gives_review_required_turns_no_cooldown(rig):
    review_ = rig.engine.live_review
    review_.config = GuardsCfg(enabled=False)
    assert review_.config_for(TurnMeta('t', review_required=True)) == GuardsCfg(enabled=True, phrase_cooldown=False)
    review_.config = GuardsCfg(enabled=True)
    assert review_.config_for(TurnMeta('t', review_required=True)).phrase_cooldown is True


def test_another_problem_on_the_retry_keeps_the_old_rule(rig, monkeypatch):
    calls = []
    def review_(stores, d):
        calls.append(d.text)
        return Verdict('rewrite', ('phrase_day', 'claim_conflict'), phrases=('absolute menace',))
    monkeypatch.setattr(live, 'review', review_)
    rig.script(MENACE, 'fine, the raid waits for nobody. you absolute menace')
    sent, meta = speak(rig, turn())
    assert not sent and rig.utterances()[-1]['dropped'] == 'guard_rewrite_refused'


# -- the conversation's subject (owner's live DM test, 2026-11-16) -------------------------------
# A sandbox replay of his DM session had every answer rewritten, four of them for the place and
# plans he was writing about, said back. Each rule on a guild line, his whisper and his DM alike.

ROOMS = [('guild', 0, 0, dict(channel='guild')),
         ('whisper', 1, 101, dict(channel='whisper', to_guid=1)),
         ('dm:101', 1, 101, dict(channel='dm', room='101', to_guid=1))]


def _his(store, text, channel, his_to, ts):
    put(store, text, guid=1, channel=channel, to=his_to, ts=ts)


def _hers(store, text, channel, her_to, ts):
    put(store, text, channel=channel, to=her_to, ts=ts)


@pytest.mark.parametrize('channel,her_to,his_to,mine', ROOMS)
def test_a_phrase_of_his_subject_is_never_her_tic(store, channel, her_to, his_to, mine):
    _his(store, 'we rented a cabin by a norway fjord', channel, his_to, NOW - 3000)
    for ago in (1500, 900):
        _hers(store, 'that norwegian fjord cabin sounds unreal', channel, her_to, NOW - ago)
    # every content word his ("norwegian" folds to his "norway"): the topic, not her tic
    assert found(store, draft('the norwegian fjord cabin again', **mine)).codes == ()


@pytest.mark.parametrize('channel,her_to,his_to,mine', ROOMS)
def test_a_two_word_phrase_needs_words_nobody_else_used(store, channel, her_to, his_to, mine):
    _his(store, 'the cabin is tiny but the view', channel, his_to, NOW - 3000)
    for ago in (1500, 900):
        _hers(store, 'cozy cabin vibes honestly, cozy cabin', channel, her_to, NOW - ago)
    # "cabin" is his: "cozy cabin" is no candidate, though "cozy" is only hers
    assert found(store, draft('cozy cabin forever', **mine)).codes == ()
    for ago in (1400, 800):
        _hers(store, 'sleepy kitten energy today', channel, her_to, NOW - ago)
    assert 'phrase_conversation' in found(store, draft('sleepy kitten again', **mine)).codes


@pytest.mark.parametrize('channel,her_to,his_to,mine', ROOMS)
def test_one_earlier_use_is_no_conversation_tic_unless_stock(store, channel, her_to, his_to, mine):
    _hers(store, 'that marble staircase was wild', channel, her_to, NOW - 900)
    assert 'phrase_conversation' not in found(store, draft('the marble staircase again', **mine)).codes
    _hers(store, 'okay the marble staircase though', channel, her_to, NOW - 600)
    assert 'phrase_conversation' in found(store, draft('the marble staircase again', **mine)).codes
    _hers(store, 'you are such an absolute menace', channel, her_to, NOW - 300)  # stock: once is enough
    assert 'phrase_conversation' in found(store, draft('my absolute menace', **mine)).codes


@pytest.mark.parametrize('channel,her_to,his_to,mine', ROOMS)
def test_his_own_stock_words_said_back_are_no_stock_tic(store, channel, her_to, his_to, mine):
    for guid in (102, 103, 104):  # the cast's sameness: still rationed
        put(store, 'just staring at the ceiling', guid=guid, ts=NOW - 3600, channel='party')
    assert 'stock_cast' in found(store, draft('staring at the menu', **mine)).codes
    _his(store, 'been staring at the sea all day', channel, his_to, NOW - 300)
    assert 'stock_cast' not in found(store, draft('staring at the menu', **mine)).codes


def test_the_subject_folds_simple_inflections():
    assert phrases._same_word('norway', 'norwegian') and phrases._same_word('fast', 'faster')
    assert phrases._same_word('thinking', 'think') and not phrases._same_word('food', 'fool')
    assert not phrases._same_word('run', 'running')  # under four letters: only itself
    assert phrases._topic(('in', 'norwegian', 'waffles'), frozenset({'norway', 'waffle'}))
    assert not phrases._topic(('norwegian', 'salmon'), frozenset({'norway'}))


# -- slop and common stock entries (controller ruling, 2026-11-16) -------------------------------
# A "!" line is slop: over its ration it is a rewrite on every turn. Any other line is common: a
# rewrite only on a turn not owed to him; on an owed answer it is no code, only `pending`, named
# on her next turn. Each on a guild line, his whisper and his DM, owed and not.

def test_the_list_marks_its_slop():
    names = phrases.slop()
    assert {'menace', 'you absolute tease', 'proper mental', 'chaos gremlin', 'the audacity',
            'i cant even', 'living for this', 'shivers down'} <= names
    assert not {'sitting here', 'every single', 'at least', 'staring at', 'i swear'} & names
    assert dict(phrases.stock())['proper mental'] == ((True, ('proper', 'mental')),)


@pytest.mark.parametrize('channel,her_to,his_to,mine', ROOMS)
@pytest.mark.parametrize('owed', [False, True])
def test_a_common_entry_waits_for_her_next_turn_only_when_owed(store, channel, her_to, his_to,
                                                               mine, owed):
    _hers(store, 'just sitting here with tea', 'party', 0, NOW - 7200)  # hers, in 72 h
    result = found(store, draft('sitting here waiting for the raid', real_answer_due=owed, **mine))
    if owed:
        assert result.codes == () and result.pending == ('sitting here',)
    else:
        assert 'stock_member' in result.codes and result.pending == ()


@pytest.mark.parametrize('channel,her_to,his_to,mine', ROOMS)
@pytest.mark.parametrize('owed', [False, True])
def test_slop_is_a_rewrite_owed_or_not(store, channel, her_to, his_to, mine, owed):
    _hers(store, 'that boss is a menace', 'party', 0, NOW - 7200)
    result = found(store, draft('you menace, eight it is', real_answer_due=owed, **mine))
    assert 'stock_member' in result.codes and result.pending == ()


@pytest.mark.parametrize('owed', [False, True])
def test_the_casts_common_phrase_waits_too_on_his_answer(store, owed):
    for guid in (102, 103, 104):
        put(store, 'im sitting here bored', guid=guid, ts=NOW - 3600, channel='party')
    result = found(store, draft('im sitting here with soup', channel='whisper', to_guid=1,
                                real_answer_due=owed))
    if owed:
        assert result.codes == () and result.pending == ('sitting here',)
    else:
        assert {'phrase_cast', 'stock_cast'} <= set(result.codes)


def test_a_draft_with_both_waits_on_the_common_and_rewrites_the_slop(store):
    _hers(store, 'just sitting here', 'party', 0, NOW - 7200)
    _hers(store, 'what a menace', 'party', 0, NOW - 7000)
    result = found(store, draft('sitting here, you menace', channel='whisper', to_guid=1,
                                real_answer_due=True))
    assert result.codes == ('stock_member',) and result.pending == ('sitting here',)
    assert 'menace' in ' '.join(result.phrases)

