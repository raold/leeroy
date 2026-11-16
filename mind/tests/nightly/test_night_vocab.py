"""Vocabulary stays public, remembers its actual speakers, and promotes slang once."""
import json
from dataclasses import replace

import pytest

from nightly.conftest import no_real_name
from leeroy_mind.nightly import lexicon, vocab


def rows(h):
    return {r["word"]: dict(r) for r in h.store.conn.execute("SELECT * FROM vocab")}


def terms(h):
    return [dict(r) for r in h.store.conn.execute("SELECT * FROM lexicon WHERE kind='slang' ORDER BY id")]


def say_word(h, word="zug", users=(101, 102, 103), *, at=None, **kwargs):
    at = h.T if at is None else at
    return [h.say(at + n, guid, word, **kwargs) for n, guid in enumerate(users)]


def promote(h, ctx=None):
    ctx = ctx or h.ctx()
    vocab.vocab(ctx)
    vocab.slang(ctx)
    assert ctx.reasoner.llm.calls == []
    return ctx


def test_bootstrap_reads_history_and_window_but_not_future(h):
    h.say(h.T - 30 * 86400, 101, "raid")
    h.say(h.T - 30 * 86400, 999, "outsider")
    h.say(h.T - 30 * 86400, 101, "secretword", channel="whisper", to=1)
    ctx = h.ctx()
    h.say(ctx.window[0], 102, "newword")
    h.say(ctx.window[1], 103, "futureword")
    vocab.vocab(ctx)
    found = rows(h)
    assert set(found) == {"raid", "newword"}
    assert found["raid"]["first_seen"] == h.T - 30 * 86400
    assert json.loads(found["raid"]["users"]) == [101]


def test_after_bootstrap_only_new_window_is_read_and_replays_are_immutable(h):
    first = h.say(h.T, 101, "zug")
    ctx = h.ctx()
    vocab.vocab(ctx)
    h.store.conn.execute("UPDATE chat_line SET text='rewritten' WHERE id=?", (first,))
    h.say(h.T + 5, 102, "lateinsert")
    vocab.vocab(ctx)
    assert set(rows(h)) == {"zug"}
    h.say(h.T - 30 * 86400, 103, "backfill")
    later = h.ctx(night="2026-10-30")
    h.say(later.window[0], 103, "laterword")
    vocab.vocab(later)
    assert set(rows(h)) == {"zug", "laterword"}
    assert "backfill" not in rows(h)
    with pytest.raises(ValueError, match="window changed"):
        vocab.vocab(h.ctx(window=(ctx.window[0], ctx.window[1] + 1)))


def test_public_lines_strip_markup_and_exclude_all_name_tokens_in_any_case(h):
    say_word(h, "|cffffffffZUG|r Arkon ARKON belra Testy TESTMAGE Quillon quillon QUILLON")
    promote(h)
    assert set(rows(h)) == {"zug"}
    assert [r["term"] for r in terms(h)] == ["zug"]
    assert no_real_name(h.store) == []


def test_multiword_real_name_tokens_are_not_vocabulary(h):
    ctx = h.ctx()
    ctx.ident = replace(ctx.ident, real_name="Quiet Quillon")
    say_word(h, "quiet QUIET quillon zug")
    promote(h, ctx)
    assert set(rows(h)) == {"zug"}


def test_only_cast_and_human_public_speakers_count(h):
    say_word(h, users=(101, 1, 999))
    h.say(h.T + 10, 102, "zug", channel="whisper", to=1)
    h.say(h.T + 11, 103, "zug", origin="human")
    h.say(h.T + 12, 1, "zug", origin="game")
    promote(h)
    assert json.loads(rows(h)["zug"]["users"]) == [1, 101]
    assert terms(h) == []


def test_cast_game_chat_is_valid_without_fabricated_output_pair(h):
    say_word(h, origin="game")
    promote(h)
    assert [r["term"] for r in terms(h)] == ["zug"]


@pytest.mark.parametrize("bad", ["dropped", "unpaired", "bad_memory_ids", "private", "missing"])
def test_unsafe_paired_output_cannot_seed_vocabulary(h, bad):
    ids = say_word(h)
    if bad == "dropped":
        h.store.conn.execute("UPDATE utterance SET dropped='screened'")
    elif bad == "unpaired":
        h.store.conn.execute("DELETE FROM utterance")
    elif bad == "bad_memory_ids":
        h.store.conn.execute("UPDATE utterance SET memory_ids='[true]'")
    elif bad == "missing":
        h.store.conn.execute("UPDATE utterance SET memory_ids='[99999]'")
    else:
        root = h.mem(101, "Private context.", scope="private", scope_with=1)
        for _ in range(5):
            root = h.mem(101, "Old open wrapper.", evidence=(f"memory:{root}",))
        h.store.conn.execute("UPDATE utterance SET memory_ids=?", (json.dumps([root]),))
    promote(h)
    assert rows(h) == {} and terms(h) == []
    assert all(h.store.get_meta(f"nightly:vocab-line:{i}") == "rejected" for i in ids)


def test_nearest_dropped_output_cannot_borrow_an_older_sent_match(h):
    say_word(h)
    h.store.conn.execute("UPDATE utterance SET dropped='screened'")
    for n, guid in enumerate((101, 102, 103)):
        h.utter(h.T + n - 1, guid, "zug")
    promote(h)
    assert rows(h) == {}


def test_slang_requires_three_distinct_users_and_a_recent_origin(h):
    say_word(h, "zug", users=(101, 101, 102))
    say_word(h, "raid", at=h.T - 30 * 86400)
    promote(h)
    assert json.loads(rows(h)["zug"]["users"]) == [101, 102]
    assert terms(h) == []


def test_age_uses_inclusive_fourteen_day_window_boundary_not_delayed_clock(h):
    ctx = h.ctx()
    at = ctx.window[1] - 14 * 86400
    say_word(h, "zug", at=at)
    say_word(h, "stale", at=at - 1)
    h.clock.set(ctx.window[1] + 20 * 86400)  # delayed processing keeps the original window
    promote(h, ctx)
    assert [r["term"] for r in terms(h)] == ["zug"]


@pytest.mark.parametrize("word", ["rizz", "sus", "yeet", "delulu", "skibidi"])
def test_modern_slang_never_promotes(h, word):
    say_word(h, word)
    promote(h)
    assert word in rows(h) and terms(h) == []


def test_each_nonorigin_user_gets_own_public_evidence_and_one_memory(h):
    origin = h.say(h.T - 2 * 86400, 101, "zug")
    second = h.say(h.T, 102, "zug")
    third = h.say(h.T + 1, 103, "zug")
    later_duplicate = h.say(h.T + 2, 102, "zug")
    ctx = promote(h)
    vocab.slang(ctx)
    term, = terms(h)
    assert term["originator"] == 101 and term["origin_ref"] == f"chat:{origin}"
    assert h.rows(101) == []
    for guid, own in ((102, second), (103, third)):
        memory, = h.rows(guid)
        assert memory.text == "Picked up 'zug' from Arkon."
        assert memory.kind == "reflection" and memory.source == "chat" and memory.importance == 3
        assert memory.key == "slang:zug" and memory.actors == (101,)
        assert {f"chat:{origin}", f"chat:{own}"} <= set(memory.evidence)
        assert f"chat:{later_duplicate}" not in memory.evidence
        assert (memory.scope, memory.scope_with, memory.status) == ("open", 0, "active")
    assert h.rows(0) == h.rows(h.human) == []


@pytest.mark.parametrize("users", [(1, 101, 102), (101, 1, 102)])
def test_human_contributes_usage_and_origin_but_never_gets_memory(h, users):
    say_word(h, users=users)
    promote(h)
    assert len(terms(h)) == 1
    assert json.loads(rows(h)["zug"]["users"]) == sorted(users)
    assert h.rows(h.human) == h.rows(0) == []
    origin = users[0]
    for guid in set(users) - {origin, h.human}:
        memory, = h.rows(guid)
        assert memory.text == f"Picked up 'zug' from {'Testy' if origin == 1 else 'Arkon'}."
    if origin != h.human:
        assert h.store.conn.execute("SELECT 1 FROM lexicon_adopter WHERE guid=?", (h.human,)).fetchone()


def test_away_member_retains_historical_usage_and_slang_copy(h):
    say_word(h)
    h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (103,'a break',?,?)",
                         (h.T, h.T + 86400))
    promote(h)
    assert len(terms(h)) == 1 and h.rows(103)[0].key == "slang:zug"


@pytest.mark.parametrize("change", ["text", "whisper", "dropped", "private_ancestor", "missing"])
def test_source_changes_after_ingestion_cannot_publish_slang(h, change):
    ids = say_word(h)
    ctx = h.ctx()
    vocab.vocab(ctx)
    if change == "text":
        h.store.conn.execute("UPDATE chat_line SET text='zug edited' WHERE id=?", (ids[0],))
    elif change == "whisper":
        h.store.conn.execute("UPDATE chat_line SET channel='whisper',to_guid=1 WHERE id=?", (ids[0],))
    elif change == "dropped":
        h.store.conn.execute("UPDATE utterance SET dropped='screened' WHERE guid=101")
    elif change == "missing":
        h.store.conn.execute("DELETE FROM chat_line WHERE id=?", (ids[0],))
    else:
        mem = h.mem(101, "Private context.", scope="private", scope_with=1)
        h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE guid=101", (json.dumps([mem]),))
    vocab.slang(ctx)
    assert terms(h) == [] and all(h.rows(g) == [] for g in (102, 103))


def test_ancestor_becoming_private_is_rechecked_without_output_mutation(h):
    root = h.mem(101, "A public observation.")
    say_word(h)
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE guid=101", (json.dumps([root]),))
    ctx = h.ctx()
    vocab.vocab(ctx)
    h.store.conn.execute("UPDATE memory SET scope='private',scope_with=1 WHERE id=?", (root,))
    vocab.slang(ctx)
    assert terms(h) == []


@pytest.mark.parametrize("bad", ["private", "dropped"])
def test_nested_chat_output_cannot_hide_private_or_dropped_input(h, bad):
    ancestor = h.say(h.T - 86400, 101, "oldphrase")
    recalled = h.mem(101, "Old public wrapper.", evidence=(f"chat:{ancestor}",))
    say_word(h)
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE text='zug'",
                         (json.dumps([recalled]),))
    if bad == "private":
        secret = h.mem(101, "Private context.", scope="private", scope_with=1)
        h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE text='oldphrase'",
                             (json.dumps([secret]),))
    else:
        h.store.conn.execute("UPDATE utterance SET dropped='screened' WHERE text='oldphrase'")
    promote(h)
    assert "zug" not in rows(h) and terms(h) == []


def test_nested_output_mutation_after_ingestion_invalidates_slang_sources(h):
    ancestor = h.say(h.T - 86400, 101, "oldphrase")
    recalled = h.mem(101, "Old public wrapper.", evidence=(f"chat:{ancestor}",))
    say_word(h)
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE text='zug'",
                         (json.dumps([recalled]),))
    ctx = h.ctx()
    vocab.vocab(ctx)
    secret = h.mem(101, "Private context.", scope="private", scope_with=1)
    h.store.conn.execute("UPDATE utterance SET memory_ids=? WHERE text='oldphrase'",
                         (json.dumps([secret]),))
    vocab.slang(ctx)
    assert terms(h) == []


def test_cap_is_durable_across_retries_and_remaining_word_can_promote_next_night(h):
    say_word(h, "zug zugga")
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, slang_per_night=1)
    promote(h, ctx)
    vocab.slang(h.ctx())  # raising the cap permits only its genuine remaining capacity
    assert len(terms(h)) == 2
    # A fresh fixture-style second pair isolates the unchanged-cap retry contract.
    later = h.ctx(night="2026-10-30")
    say_word(h, "zugbar zugbaz", at=later.window[0])
    later.settings = replace(later.settings, slang_per_night=1)
    promote(h, later)
    vocab.slang(later)
    assert len(terms(h)) == 3
    next_night = h.ctx(night="2026-10-31")
    promote(h, next_night)
    assert len(terms(h)) == 4


def test_vetoed_term_is_not_reborn_or_given_new_copies(h):
    ids = say_word(h)
    h.store.conn.execute("INSERT INTO lexicon(kind,term,origin_ref,originator,born,last_used,status) "
        "VALUES ('slang','zug',?,101,?,?,'vetoed')", (f"chat:{ids[0]}", h.T, h.T))
    promote(h)
    assert terms(h)[0]["status"] == "vetoed"
    assert all(h.rows(g) == [] for g in (101, 102, 103))


def test_forged_vocab_users_without_public_receipts_do_not_count(h):
    h.store.conn.execute("INSERT INTO vocab(word,first_seen,origin,users) VALUES ('zug',?,101,'[101,102,103]')",
                         (h.T,))
    vocab.slang(h.ctx())
    assert terms(h) == []


def test_slang_term_copies_flags_and_receipt_rollback_together(h, monkeypatch):
    say_word(h)
    ctx = h.ctx()
    vocab.vocab(ctx)
    adopt = lexicon.adopt

    def fail_second(store, context, term_id, guid, **kwargs):
        result = adopt(store, context, term_id, guid, **kwargs)
        if guid == 103:
            raise RuntimeError("synthetic failure after second copy")
        return result

    monkeypatch.setattr(lexicon, "adopt", fail_second)
    with pytest.raises(RuntimeError, match="second copy"):
        vocab.slang(ctx)
    assert terms(h) == [] and rows(h)["zug"]["slang"] == 0
    assert h.store.get_meta(f"nightly:slang-created:{ctx.night}") is None
    assert h.rows(102) == h.rows(103) == []
    monkeypatch.setattr(lexicon, "adopt", adopt)
    vocab.slang(ctx)
    assert len(terms(h)) == 1


# -- the junk filter: ordinary words are never slang ----------------------------------------------

@pytest.mark.parametrize("word", ["you", "now", "can", "the", "doing", "literally", "tonight", "Valley"])
def test_everyday_words_are_not_slang(word):
    from leeroy_mind.talk.everyday import is_everyday
    assert is_everyday(word) and vocab.junk_reason(word.casefold(), 101) == "common"


@pytest.mark.parametrize("word", ["lol", "gonna", "grats", "congrats", "didn", "isn", "dont", "tht", "dkp", "loot"])
def test_chat_shorthand_contraction_stubs_and_stock_jargon_are_not_slang(word):
    assert vocab.junk_reason(word, 101) == "common"


@pytest.mark.parametrize("word", ["zug", "zugzug", "gank", "noob", "nerf", "kekw", "pewpew", "bonkfest"])
def test_coined_words_and_game_slang_pass_the_word_lists(word):
    assert vocab.junk_reason(word, 101) is None


def test_ordinary_words_said_by_three_members_never_promote_but_a_coinage_beside_them_does(h):
    say_word(h, "zug you now can the doing still here")
    promote(h)
    assert {"you", "now", "can", "doing", "zug"} <= set(rows(h))
    assert [r["term"] for r in terms(h)] == ["zug"]
    assert {m.key for g in (102, 103) for m in h.rows(g)} == {"slang:zug"}


def test_a_word_the_cast_already_says_commonly_is_not_slang(h):
    # More than COMMON_MIN_LINES public lines of a corpus this small: the cast's common word.
    for n in range(vocab.COMMON_MIN_LINES + 1):
        h.say(h.T + n, (101, 102, 103)[n % 3], "blorp")
    promote(h)
    assert terms(h) == []


def test_ten_lines_are_still_rare_enough(h):
    for n in range(vocab.COMMON_MIN_LINES):
        h.say(h.T + n, (101, 102, 103)[n % 3], "blorp")
    promote(h)
    assert [r["term"] for r in terms(h)] == ["blorp"]


def _corpus(h, origin_lines, filler=17):
    """101 first says blorp in `origin_lines` of their own lines (the others are filler), then
    102 and 103 say it once; 104 fills the corpus with ordinary words."""
    t = h.T
    for n in range(origin_lines[1]):
        h.say(t, 101, "blorp" if n < origin_lines[0] else "the raid was good")
        t += 1
    for guid in (102, 103):
        h.say(t, guid, "blorp")
        t += 1
    for _ in range(filler):
        h.say(t, 104, "see you all tonight")
        t += 1


def test_a_first_speaker_who_uses_it_no_more_than_anyone_coined_nothing(h, monkeypatch, caplog):
    monkeypatch.setattr(vocab, "DISTINCT_LINES", 20)
    _corpus(h, (1, 11))  # 101: 1 of 11 lines; the cast: 3 of 30
    with caplog.at_level("INFO", logger="leeroy_mind"):
        promote(h)
    assert terms(h) == []
    assert "slang_screened" in caplog.text and "not_distinct=1" in caplog.text


def test_a_first_speaker_who_keeps_saying_it_coined_it(h, monkeypatch):
    monkeypatch.setattr(vocab, "DISTINCT_LINES", 20)
    _corpus(h, (3, 4))  # 101: 3 of 4 lines; the cast: 5 of 23
    promote(h)
    assert [r["term"] for r in terms(h)] == ["blorp"]


def test_distinctiveness_waits_for_a_corpus_large_enough_to_measure(h):
    _corpus(h, (1, 11))  # 30 public lines, under DISTINCT_LINES
    promote(h)
    assert [r["term"] for r in terms(h)] == ["blorp"]


def test_corpus_counts_only_public_lines_of_eligible_speakers_before_the_end(h):
    h.say(h.T, 101, "blorp blorp")
    h.say(h.T + 1, 102, "blorp", channel="whisper", to=1)
    h.say(h.T + 2, 999, "blorp")
    h.say(h.T + 3, 1, "blorp", origin="game")
    h.say(h.T + 4, 1, "blorp")
    h.say(h.T + 5, 103, "blorp")
    counts = vocab.corpus(h.store.conn, ["blorp"], eligible={1, 101, 102, 103}, human=1, end=h.T + 5)
    assert counts.lines == 2 and dict(counts.uses["blorp"]) == {101: 1, 1: 1}
    assert dict(counts.authors) == {101: 1, 1: 1}
