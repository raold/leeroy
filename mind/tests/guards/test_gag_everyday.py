"""The gag ration never rations an everyday word, even one still stored as slang."""
from datetime import datetime, timezone

import pytest

from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, record_sent, review

NOW = datetime(2026, 10, 29, 23, tzinfo=timezone.utc).timestamp()


def term(store, ident, word, kind="slang"):
    with store.transaction():
        store.conn.execute("INSERT INTO lexicon(id,kind,term,origin_ref,originator,born,last_used) "
                           "VALUES (?,?,?,'chat:1',101,?,?)", (ident, kind, word, NOW - 100, NOW - 100))
        store.conn.execute("INSERT INTO lexicon_adopter(term,guid,first_use) VALUES (?,101,?)",
                           (ident, NOW - 100))


def sent(store, s, text, n):
    with store.transaction():
        return record_sent(s, Draft(101, 'guild', text, now=NOW), receipt_id=f'd{n}')


@pytest.mark.parametrize('word', ['you', 'the', 'now', 'can', 'lol'])
def test_a_stored_everyday_slang_word_is_never_rationed_nor_recorded(store, word):
    term(store, 7, word)
    s = GuardStores(store.conn, config=GuardsCfg(enabled=True, gag_caps={7: 1}, stock_daily_cap=100))
    text = f'{word} will see the copper lantern'
    for n in range(3):
        sent(store, s, text, n)
        assert review(s, Draft(101, 'guild', text, now=NOW)).action == 'send'
    receipts = store.conn.execute("SELECT value FROM meta WHERE key LIKE 'guards:sent:%'").fetchone()[0]
    assert 'gag:' not in receipts


def test_coined_slang_and_a_running_joke_of_common_words_are_still_rationed(store):
    term(store, 7, 'zugzug')
    term(store, 8, 'the floor', kind='joke')
    s = GuardStores(store.conn, config=GuardsCfg(enabled=True, gag_daily_cap=1))
    for n, text in enumerate(('zugzug before the pull', 'mind the floor')):
        assert sent(store, s, text, n)
        result = review(s, Draft(101, 'guild', text, now=NOW))
        assert result.action == 'rewrite' and result.codes == ('gag_ration',)
