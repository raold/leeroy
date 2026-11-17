"""A member's wrong pronoun for another member (evening DM audit, 2026-11-16: asked about a
guildmate who is a woman, a member called her "he"): `member_misgender` on a
guild line, his whisper and his DM alike, the retry told the member and the pronouns only.
Invented members and lines only."""
import pytest

from engine_rig import save_member
from leeroy_mind.discord import store as dstore
from leeroy_mind.guards import Draft, GuardsCfg, GuardStores, HistoryLine, Verdict, review
from leeroy_mind.guards import pronouns
from leeroy_mind.guards.review import HARD_CODES, rewrite_hint
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.pipeline import TurnMeta

from .test_live import rig, source, speak, turn  # noqa: F401  (the rig fixture)

ROSTER = {"belra": ("Belra", "female", False), "coros": ("Coros", "male", False),
          "hope": ("Hope", "female", True)}
HIS = "you and belra ok? guild chat was tense last night"
DRAFT = "all good between us. we argued about the pull timer and he got loud about it"


def found(text, his_line="", **kw):
    return pronouns.misgendered(text, his_line, ROSTER, **kw)


def test_his_line_names_her_and_the_draft_calls_her_he():
    assert found(DRAFT, HIS) == (("Belra", "he", "she/her"),)


@pytest.mark.parametrize("text,expected", [
    ("belra missed two raids. he's not coming tonight", (("Belra", "he's", "she/her"),)),
    ("coros said she'd tank it", (("Coros", "she'd", "he/him"),)),
    ("Hope topped the meters. he was on fire", (("Hope", "he", "she/her"),)),
    ("belra missed two raids. she's not coming tonight", ()),        # right
    ("belra missed two raids, he says sorry and she's on tomorrow", ()),  # hers there too
    ("hope so. he was on fire", ()),                                  # an everyday word, lowercase
    ("belra stop. he'll answer later", ()),                            # she speaks to her
    ("careful belra, he'll pull the whole room", ()),
    ("back off belra he's taken", ()),
    ("belra and coros fought and he started it", ()),                  # both genders named
    ("belra's fine, my husband says he saw it", ()),                   # someone else's "he"
    ("belra pulled patchwerk early and he hits hard", ()),             # the boss's "he"
    ("he's baking for belra's party", ()),                             # someone named before
    ('belra quoted it, "he did it"', ()),                             # someone else's words
    ("belra's cat knocked my drink over, he's a menace", ()),         # a pet's
])
def test_a_name_in_her_draft(text, expected):
    assert found(text) == expected


def test_only_his_line_names_whom_she_answers_about():
    assert found("he got loud about it", "") == ()
    assert found("he got loud about it", HIS, his_names=("Zan",)) == (
        ("Belra", "he", "she/her"),)
    assert found("zan says he got loud", HIS, his_names=("Zan",)) == ()  # him, by name
    assert found("dunna says he got loud", HIS, others=frozenset({"dunna"})) == ()


def test_the_hint_names_the_member_and_the_pronouns_only():
    hint = rewrite_hint(("member_misgender",), (), (("Belra", "he", "she/her"),))
    assert 'You called Belra "he": use "she" and "her" for Belra.' in hint
    assert "member_misgender" not in hint and "woman" not in hint
    assert "member_misgender" not in HARD_CODES


# -- the review, on a guild line, his whisper and his DM ------------------------------------------

def _her(rig):
    save_member(rig.store, 102, player_gender="female")  # Belra: the guard reads persona_core


def _case(rig, channel, his, text):
    if channel == "dm":
        mid = dstore.post(rig.store, channel="dm:101", author=1, origin="human", text=his,
                          now=rig.clock.now())
        history = (HistoryLine(mid, rig.clock.now(), 1, his, "dm", to_guid=101, room="101",
                               readers=(1, 101), origin="human"),)
        return (GuardStores(rig.store.conn, GuardsCfg(enabled=True), history=history),
                Draft(101, "dm", text, room="101", parent_id=mid, to_guid=1, now=rig.clock.now()))
    parent = source(rig, his, channel=channel, to=101 if channel == "whisper" else 0)
    return (GuardStores(rig.store.conn, GuardsCfg(enabled=True)),
            Draft(101, channel, text, parent_id=parent, to_guid=1 if channel == "whisper" else 0,
                  now=rig.clock.now()))


@pytest.mark.parametrize("channel", ["guild", "whisper", "dm"])
def test_review_asks_once_more_on_every_channel(rig, channel):
    _her(rig)
    verdict = review(*_case(rig, channel, HIS, DRAFT))
    assert verdict.action == "rewrite" and "member_misgender" in verdict.codes
    assert verdict.pronouns == (("Belra", "he", "she/her"),) and "Belra" not in repr(verdict)
    right = review(*_case(rig, channel, HIS, DRAFT.replace(" he ", " she ")))
    assert "member_misgender" not in right.codes and right.pronouns == ()


def test_a_members_line_naming_her_is_not_his(rig):
    _her(rig)
    parent = source(rig, HIS, who=103)
    verdict = review(GuardStores(rig.store.conn, GuardsCfg(enabled=True)),
                     Draft(101, "guild", DRAFT, parent_id=parent, now=rig.clock.now()))
    assert "member_misgender" not in verdict.codes


def test_the_retry_is_told_the_pronouns(rig, monkeypatch):
    def review_(stores, d):
        if " he " in f" {d.text} ":
            return Verdict("rewrite", ("member_misgender",), pronouns=(("Belra", "he", "she/her"),))
        return Verdict("send")
    monkeypatch.setattr(live, "review", review_)
    rig.script(DRAFT, "all good between us. we argued about the pull timer, she got loud about it")
    sent, meta = speak(rig, turn("whisper"), TurnMeta("owed", why="reply:named",
                                                       answers_human=True, whisper_reply=True))
    assert sent and "she got loud" in " ".join(sent)
    assert 'You called Belra "he": use "she" and "her" for Belra.' in rig.last_user()
