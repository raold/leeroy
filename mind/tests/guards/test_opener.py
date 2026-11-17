"""His line said back in her opening (evening DM audit, 2026-11-16: echoes were 10 of 38 answers
to him, none of them a copy the echo guard held): `echo_opener` on a guild line, his whisper and
his DM alike, the retry told in words, and a retry that still opens on it loses the clauses
(an answer owed to him is never dropped for it). Invented lines only, none from the audit."""
import pytest

from leeroy_mind.discord import store as dstore
from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, HistoryLine, Verdict, review
from leeroy_mind.guards.opener import cut, echo_opener
from leeroy_mind.guards.review import HARD_CODES, rewrite_hint
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import CUT_CODES, TurnMeta

from .test_live import rig, source, speak, turn  # noqa: F401  (the rig fixture)

ASK = "remind me what your main is. i forgot"
HOLIDAY = "im at my brothers this week. so i logged in from my phone to chat with you"


@pytest.mark.parametrize("parent,reply", [
    # his short line repeated as her opener, as a question or with filler round it
    ("raid friday?", "raid friday? raid friday. yeah im in"),
    ("did you check the loot council after ulduar? 12 items lol", "12 items? thats harsh"),
    ("ok gotta go, dentist appointment. later?", "yeah dentist appointment of course, have fun"),
    ("link the gear list. from the armory", "link the gear list? from the armory? cheeky"),
    # quoted back
    ("lol sure", '"lol sure" - thats all i get? after the night i had'),
    ("you seem tired tonight", "you say 'seem tired' like i hide anything from you"),
    # what he said he did, said back to him
    (ASK, "you forgot my main?? resto shaman, always"),
    (HOLIDAY, "youre at your brothers and you logged in from your phone to chat? sweet"),
    (HOLIDAY, "honestly you are ridiculous arent you... logging in from his sofa to chat with me"),
    # what he asks, reported back
    ("do you have time to farm some herbs with me?", "youre asking to farm herbs again? fine"),
])
def test_her_opening_says_his_line_back(parent, reply):
    assert echo_opener(reply, parent)


@pytest.mark.parametrize("parent,reply", [
    ("fish", "fish??"),                                      # one word said back is a reaction
    ("i caught a fish", "fish?? since when do you fish"),
    ("i love you", "i love you"),                            # said as her own
    ("ok. lets do this then. ready?", "ok lets do this then"),
    ("morning sunshine", "morning sunshine, coffee yet?"),    # a greeting said back
    ("sleep well", "sleep well x"),
    ("you drive me wild", "you drive me wild too"),          # given back to him
    ("i missed raiding with you", "missed raiding with you too"),
    ("are you coming to the raid tonight?", "coming to the raid, yes, after dinner"),
    ("Are the spare flasks ready for the next raid?", "The spare flasks are ready for the next raid."),
    # what he said he did, remarked on after her answer to his question, is no echo
    (ASK, "resto shaman, always. you forgot everything lol"),
    ("im staying in a cabin by the lake. ever been fishing?",
     "never, but i went camping once and youre in a cabin by the lake being all fancy"),
    ("i want to cook for you", "you cook for me already"),    # what he wants, answered
    ("what if i ordered pizza to your place?", "you had me at pizza"),
    ("i bring flasks every week", "i want u to bring flasks tonight"),  # you as an object
    ("repeat after me: raid at eight", "raid at eight"),      # he asked for the words
    ("", "anything at all"),
    ("you catch the recount?", "yeah, mages topped it again lol"),
])
def test_an_answer_is_no_echo(parent, reply):
    assert not echo_opener(reply, parent)


@pytest.mark.parametrize("reply,parent,left", [
    ("raid friday? raid friday. yeah im in, bring flasks", "raid friday?", "yeah im in, bring flasks"),
    ('"lol sure" - thats all i get? wow', "lol sure", "thats all i get? wow"),
    ("youre at your brothers and you logged in from your phone to chat? thats sweet honestly",
     HOLIDAY, "thats sweet honestly"),
    ("Raid friday? Yes, bring flasks", "raid friday?", "Yes, bring flasks"),
    ("12 items?", "did you check the loot council? 12 items lol", None),  # nothing left
])
def test_cut_takes_the_echo_out_when_the_rest_stands_alone(reply, parent, left):
    assert cut(reply, parent) == left


def test_the_code_is_soft_and_a_retry_cut():
    assert "echo_opener" not in HARD_CODES and "echo_opener" in CUT_CODES
    hint = rewrite_hint(("echo_opener",))
    assert "echo_opener" not in hint and "without saying it back" in hint


# -- the review, on a guild line, his whisper and his DM ------------------------------------------

def _draft(rig, channel, text, parent):
    if channel == "guild":
        return Draft(101, "guild", text, parent_id=parent, now=rig.clock.now())
    if channel == "whisper":
        return Draft(101, "whisper", text, parent_id=parent, to_guid=1, now=rig.clock.now(),
                     real_answer_due=True)
    return Draft(101, "dm", text, room="101", parent_id=parent, to_guid=1, now=rig.clock.now(),
                 real_answer_due=True)


def _his(rig, channel, text):
    """His line on `channel`: (its id, the guard's caller-observed history)."""
    if channel == "dm":
        mid = dstore.post(rig.store, channel="dm:101", author=1, origin="human", text=text,
                          now=rig.clock.now())
        return mid, (HistoryLine(mid, rig.clock.now(), 1, text, "dm", to_guid=101, room="101",
                                 readers=(1, 101), origin="human"),)
    return source(rig, text, channel=channel, to=101 if channel == "whisper" else 0), ()


@pytest.mark.parametrize("channel", ["guild", "whisper", "dm"])
def test_review_asks_once_more_on_every_channel(rig, channel):
    parent, history = _his(rig, channel, ASK)
    verdict = review(GuardStores(rig.store.conn, GuardsCfg(enabled=True), history=history),
                     _draft(rig, channel, "you forgot my main?? resto shaman, always", parent))
    assert verdict.action == "rewrite" and "echo_opener" in verdict.codes
    assert verdict.echo_of == ASK and ASK not in repr(verdict)
    clean = review(GuardStores(rig.store.conn, GuardsCfg(enabled=True), history=history),
                   _draft(rig, channel, "resto shaman, always. you forgot everything lol", parent))
    assert "echo_opener" not in clean.codes and clean.echo_of == ""


def test_her_own_line_is_never_her_echo(rig):
    mine = source(rig, "raid friday?", who=101)
    verdict = review(GuardStores(rig.store.conn, GuardsCfg(enabled=True)),
                     Draft(101, "guild", "raid friday? raid friday.", parent_id=mine,
                           now=rig.clock.now()))
    assert "echo_opener" not in verdict.codes


def test_a_copy_stays_the_hard_echo(rig):
    parent = source(rig, "how many kids do you have? what about their dad?", channel="whisper",
                    to=101)
    verdict = review(GuardStores(rig.store.conn, GuardsCfg(enabled=True)),
                     _draft(rig, "whisper", "how many kids do you have? what about their dad?", parent))
    assert "echo_parent" in verdict.codes and "echo_opener" not in verdict.codes


# -- the pipeline ---------------------------------------------------------------------------------

HIS = "ok gotta go, dentist appointment. later?"
ECHO = "yeah dentist appointment of course. have fun and text me after"


def echo_review(stores, d):
    if echo_opener(d.text, HIS):
        return Verdict("rewrite", ("echo_opener",), echo_of=HIS)
    return Verdict("send")


def test_the_retry_is_told_not_to_say_it_back(rig, monkeypatch):
    monkeypatch.setattr(live, "review", echo_review)
    rig.script(ECHO, "have fun, text me after")
    sent, meta = speak(rig, turn())
    assert sent and "dentist" not in " ".join(sent) and meta.guard_rewritten
    assert "without saying it back" in rig.last_user()


def test_a_bystander_retry_that_still_echoes_loses_the_opener(rig, monkeypatch):
    monkeypatch.setattr(live, "review", echo_review)
    rig.script(ECHO, "dentist appointment? have fun, text me after")
    sent, meta = speak(rig, turn())
    assert sent and "dentist" not in " ".join(sent) and "text me after" in " ".join(sent)
    assert len(rig.chat_calls()) == 2


def test_a_bystander_retry_with_nothing_else_is_dropped(rig, monkeypatch):
    monkeypatch.setattr(live, "review", echo_review)
    rig.script(ECHO, "dentist appointment?")
    sent, meta = speak(rig, turn())
    assert not sent and rig.utterances()[-1]["dropped"] == "guard_rewrite_refused"


@pytest.mark.parametrize("channel", ["whisper", "dm:101"])
def test_an_owed_answer_loses_the_opener_and_is_never_dropped(rig, monkeypatch, channel):
    monkeypatch.setattr(live, "review", echo_review)
    rig.script(ECHO, "yeah dentist appointment of course. good luck in there, text me after")
    sent, meta = speak(rig, turn(channel), TurnMeta("owed", why="reply:named", answers_human=True,
                                                     whisper_reply=channel == "whisper"))
    said = " ".join(sent).casefold()
    assert sent and "dentist" not in said and "good luck in there" in said
    assert len(rig.chat_calls()) == 2


@pytest.mark.parametrize("channel", ["whisper", "dm:101"])
def test_an_owed_answer_with_too_little_left_goes_as_written(rig, monkeypatch, channel):
    monkeypatch.setattr(live, "review", echo_review)
    rig.script(ECHO, "dentist appointment? ok")
    sent, meta = speak(rig, turn(channel), TurnMeta("owed", why="reply:named", answers_human=True,
                                                     whisper_reply=channel == "whisper"))
    assert sent and meta.guard_verdict == "rewrite"
