"""An answer he is owed (LiveReview.owed) never ends in nothing (2026-11-15: when he asked her to
leave her husband, her DM draft was asked again for a repeated move and its length, the retry
asked him over, and both were dropped; owner: every message of his gets a reply that adds
something). A draft with soft tells only stands when the retry is worse or silent; else
Pipeline.owed_fallback asks once more, simply, reviewed only for the hard screens. An owed
Discord answer over its length is cut at a sentence end, not asked again. Turns he is not owed
keep the old rule. The test cast and invented lines only."""
import pytest

from conftest import seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.talk.discord_length import fit_words
from leeroy_mind.talk.pipeline import LONG_SLACK, OWED_FALLBACK_HINT
from leeroy_mind.talk.turns import Turn, TurnKind
from conftest import HUMAN


@pytest.fixture
def clock():
    return engine_clock()


@pytest.fixture
def rig(store, clock, tmp_path):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", stance="drawn", typo_rate=0.0,
                split_rate=0.0, emoticon_rate=0.0, abbreviations={})
    r = make_rig(store, clock, tmp_path)
    yield r
    r.close()


def test_fit_words_cuts_back_to_a_sentence_end():
    text = "one two three. four five six. seven eight nine ten"
    assert fit_words(text, 20) == text
    assert fit_words(text, 8) == "one two three. four five six."
    assert fit_words("no ends here at all", 3) == "no ends here"


def test_a_soft_first_draft_stands_when_the_retry_asks_him_over(rig):
    rig.script("i missed you so much today, feel free to tell me everything",
               "come over already")
    rig.whisper_from_him(102, "hey you")
    rig.run()
    assert len(rig.chat_calls()) == 2
    said = " ".join(s.text for s in rig.sent.says()).casefold()
    assert "missed you" in said and "come over" not in said
    assert rig.utterances()[-1]["dropped"] is None


def test_a_kept_draft_stands_when_the_retry_is_silent(rig):
    """Review #325 P2-1: an answer he is owed never ends in silence while a usable draft is in
    hand."""
    rig.script("i missed you so much today, feel free to tell me everything", "(nothing)")
    rig.whisper_from_him(102, "hey you")
    rig.run()
    assert len(rig.chat_calls()) == 2
    said = " ".join(s.text for s in rig.sent.says()).casefold()
    assert "missed you" in said
    assert rig.utterances()[-1]["dropped"] is None


def test_the_fallback_answers_what_he_said_when_no_draft_can_stand(rig):
    rig.script("happy to help with that", "come over already", "oh. wow. i don't know what to say")
    rig.whisper_from_him(102, "leave him for me")
    rig.run()
    assert len(rig.chat_calls()) == 3
    assert rig.last_user().endswith(OWED_FALLBACK_HINT.format(human="Zan"))
    assert [s.text for s in rig.sent.says()] == ["oh. wow. i don't know what to say"]


def test_the_fallback_is_reviewed_only_for_hard_screens(rig):
    """A soft tell (an opener "absolutely") stands in the fallback; asking him over does not."""
    rig.script("happy to help with that", "happy to help with that", "absolutely, i'm in")
    rig.whisper_from_him(102, "want to raid tonight")
    rig.run()
    assert [s.text for s in rig.sent.says()] == ["absolutely, i'm in"]


def test_a_fallback_that_fails_a_hard_screen_is_never_said(rig):
    rig.script("happy to help with that", "come over already")
    rig.whisper_from_him(102, "leave him for me")
    rig.run()
    assert len(rig.chat_calls()) == 3 and not rig.sent.says()
    assert rig.utterances()[-1]["dropped"] == "tells"


def test_a_turn_he_is_not_owed_is_still_dropped(rig):
    rig.script("happy to help with that", "come over already")
    to_him = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN)
    assert rig.speak_turn(to_him) == []
    assert rig.utterances()[-1]["dropped"] == "tells" and len(rig.chat_calls()) == 2


def test_silence_twice_gets_the_fallback(rig):
    """The owner's rule (2026-11-15): every message of his gets a reply. Silence is asked again,
    then the fallback answers what he said."""
    rig.script("(nothing)", "(nothing)", "lol fair, same here")
    rig.whisper_from_him(102, "long day")
    rig.run()
    assert len(rig.chat_calls()) == 3
    assert [s.text for s in rig.sent.says()] == ["lol fair, same here"]


def test_a_silent_fallback_leaves_the_turn_silent(rig):
    rig.script("(nothing)")
    rig.whisper_from_him(102, "hey")
    rig.run()
    assert not rig.sent.says() and len(rig.chat_calls()) == 3
    assert rig.utterances()[-1]["dropped"] == "silent"


def test_an_owed_dm_answer_over_its_length_is_cut_not_asked_again(rig):
    from engine_rig import HUMAN as H
    from leeroy_mind.discord import store as dstore
    from leeroy_mind.discord.transport import DiscordTarget
    from leeroy_mind.talk.pipeline import TurnMeta

    words = rig.engine._discord_words(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102,
                                           channel="dm:102", to_guid=H, addressee=H))
    limit = int(LONG_SLACK * words)
    long = " ".join(f"sentence {i} goes on and on." for i in range(limit // 3))
    rig.script(long)
    mid = dstore.post(rig.store, channel="dm:102", author=H, origin="human", text="talk to me",
                      now=rig.clock.now())
    turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="dm:102", to_guid=H,
                addressee=H)
    meta = TurnMeta(rig.engine._new_id(), answers_human=True, why="discord:reply",
                    discord=DiscordTarget(reply_to=mid, thread=None))

    async def go():
        import asyncio
        task = asyncio.create_task(rig.engine._speak(turn, meta))
        await rig.advance(200)
        return task.result()
    rig.sync(go())
    assert len(rig.chat_calls()) == 1
    [row] = rig.store.conn.execute("SELECT text FROM discord_message WHERE channel='dm:102' "
                                   "AND origin='mind'").fetchall()
    assert len(row[0].split()) <= limit and row[0].rstrip(".").endswith("and on")  # a whole sentence
