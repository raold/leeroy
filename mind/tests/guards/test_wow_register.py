"""[talk] wow_topic in the guards: a WoW draft written as prose gets the soft `chat_register`
rewrite (never a drop), her own words are rationed tighter on a WoW turn (once in the
conversation, twice a day), and the stock list's new entries (generic talk, scripted openers,
theatre metaphors) are slop or common as their lines say. Invented sheets and lines only."""
from datetime import datetime, timezone

import pytest

from conftest import seed_test_cast
from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, phrases, register, review, voice
from leeroy_mind.guards.review import HARD_CODES, DROP_CODES, HINTS, Reader, rewrite_hint
from sheets import GRUFF, give

NOW = datetime(2026, 10, 29, 23, tzinfo=timezone.utc).timestamp()
MARKS = {**GRUFF, "says": ["noted", "top marks", "sound as", "well so"]}
SHEET = voice.sheet_of({"speech": MARKS})


def put(store, text, *, guid=101, channel='guild', to=0, ts=NOW - 60):
    with store.transaction():
        store.conn.execute(
            'INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) '
            "VALUES (?,?,?,?,?,?,'mind')", (ts, channel, guid, 'Invented', to, text))


def draft(text, **kw):
    return Draft(kw.pop('guid', 101), kw.pop('channel', 'guild'), text, now=NOW, **kw)


@pytest.fixture
def cast(store):
    seed_test_cast(store)
    give(store, 101, MARKS)
    return store


def stores(store):
    return GuardStores(store.conn, GuardsCfg(enabled=True))


def test_prose_in_raid_chat_is_a_soft_rewrite_on_a_wow_turn_only(cast):
    text = "It is my pleasure to say that we will clear the whole place tonight."
    on = review(stores(cast), draft(text, wow_topic=True))
    assert register.CODE in on.codes and on.action == 'rewrite'
    assert register.CODE not in HARD_CODES and register.CODE not in DROP_CODES
    assert register.CODE not in review(stores(cast), draft(text)).codes
    assert HINTS[register.CODE] in rewrite_hint(on.codes)


def test_an_owed_answer_is_never_dropped_for_its_register(cast):
    v = review(stores(cast), draft("thanks, honey", wow_topic=True, real_answer_due=True))
    assert register.CODE in v.codes and v.action == 'rewrite'


def test_draft_rejects_a_wow_flag_that_is_no_boolean(cast):
    with pytest.raises(ValueError, match='guard_wow_topic_invalid'):
        review(stores(cast), draft("ok", wow_topic=1))


def test_her_own_word_once_in_the_conversation_is_enough_on_a_wow_turn(cast):
    put(cast, 'top marks on that pull')
    d = draft('top marks again, next one', wow_topic=True)
    assert 'phrase_conversation' in phrases.check(Reader(cast.conn), d, NOW, SHEET).codes
    plain = draft('top marks again, next one')
    assert phrases.check(Reader(cast.conn), plain, NOW, SHEET).codes == ()  # within ration


def test_her_own_word_twice_today_anywhere_is_enough_on_a_wow_turn(cast):
    put(cast, 'top marks to you', channel='whisper', to=1, ts=NOW - 8 * 3600)
    put(cast, 'top marks, all of you', channel='officer', ts=NOW - 6 * 3600)
    d = draft('top marks, pulling', wow_topic=True)
    assert 'phrase_day' in phrases.check(Reader(cast.conn), d, NOW, SHEET).codes
    assert phrases.check(Reader(cast.conn), draft('top marks, pulling'), NOW, SHEET).codes == ()


def test_the_new_stock_entries():
    slop = phrases.slop()
    names = {name for name, _ in phrases.stock()}
    for name in ("curtain call", "standing ovation", "okay so listen", "second thing first",
                 "take a bow", "the show must go on"):
        assert name in slop, name
    for name in ("rng gods", "fingers crossed"):
        assert name in names and name not in slop, name
    toks = phrases.tokens("ok so listen, the rng gods hate me")
    assert {n for n, _, _ in phrases._stock_spans(toks)} == {"okay so listen", "rng gods"}
    # anchored: "ok so listen" only where a clause opens with it
    assert not phrases._stock_spans(phrases.tokens("i said ok so listen to me"))
