"""[talk] coherence, gender and game facts and the smaller fixes: every member's chat line and
a member he names carry their pronoun and role, a second wrong pronoun on a rewrite is swapped
in code, library item facts are kept to what the speaker's class can use (and she is told to
say she is not sure when none is left), an owed answer's kept draft loses an echo opener, his
short line said back as a question is cut, her attendance counts stay out of talk that is not
about raids. Fakes only; invented members and lines."""
from dataclasses import replace

import pytest

from conftest import HUMAN, seed_test_cast
from engine_rig import FakeLibrary, chunk, engine_clock, make_rig, save_member
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.guards import GuardsCfg, Verdict, pronouns
from leeroy_mind.guards.opener import echo_opener, without_question_echo
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.protocol import Chat, Group
from leeroy_mind.talk import awareness as live
from leeroy_mind.talk.gearfit import for_speaker, usable
from leeroy_mind.talk.pipeline import UNSURE, TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

from .test_live import rig, source, speak, turn  # noqa: F401  (the rig fixture)

HIS = "you and belra ok? guild chat was tense last night"
DRAFT = "all good between us. we argued about the pull timer and he got loud about it"
FOUND = (("Belra", "he", "she/her"),)


# -- pronouns ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,found,fixed", [
    (DRAFT, FOUND, "all good between us. we argued about the pull timer and she got loud about it"),
    ("Belra missed it. He's sorry, his alarm failed and i told him so", FOUND,
     "Belra missed it. She's sorry, her alarm failed and i told her so"),
    ("coros said she would bring her flask but she forgot, typical her",
     (("Coros", "she", "he/him"),),
     "coros said he would bring his flask but he forgot, typical him"),
    ("ask coros, she knows. i gave her the list", (("Coros", "she", "he/him"),),
     "ask coros, he knows. i gave him the list"),
    ("nothing wrong here", (), "nothing wrong here"),
])
def test_a_wrong_pronoun_is_swapped_in_code(text, found, fixed):
    assert pronouns.fix(text, found) == fixed


def test_told_about_a_member_is_no_longer_read_as_speaking_to_her():
    roster = {"belra": ("Belra", "female", False)}
    assert pronouns.misgendered("tell Belra he needs to pull better", "", roster) == FOUND
    assert pronouns.misgendered("belra he's taken", "", roster) == ()  # still spoken to


def misgender_review(stores, d):
    if " he " in f" {d.text} ":
        return Verdict("rewrite", ("member_misgender",), pronouns=FOUND)
    return Verdict("send")


@pytest.mark.parametrize("channel", ["whisper", "dm:101", "guild"])
def test_a_second_miss_on_the_rewrite_is_fixed_never_sent(rig, monkeypatch, channel):
    monkeypatch.setattr(live, "review", misgender_review)
    rig.script(DRAFT, DRAFT)  # the rewrite still says "he"
    owed = channel != "guild"
    meta = (TurnMeta("owed", why="reply:named", answers_human=True,
                     whisper_reply=channel == "whisper") if owed else None)
    sent, meta = speak(rig, turn(channel), meta)
    said = " ".join(sent)
    assert sent and "she got loud" in said and " he " not in f" {said} "
    assert len(rig.chat_calls()) == 2


def test_with_coherence_off_the_second_miss_goes_as_before(store, tmp_path, monkeypatch):
    r = make_rig(store, engine_clock(), tmp_path, reply=DRAFT, coherence=False)
    r.clock._yields = 0
    r.engine.live_review.config = GuardsCfg(enabled=True)
    r.world.apply(Group(0, 1, True, (1, 101, 102, 103, 104, 105)), r.clock.now())
    monkeypatch.setattr(live, "review", misgender_review)
    try:
        sent, meta = speak(r, turn("whisper"), TurnMeta("owed", why="reply:named",
                                                        answers_human=True, whisper_reply=True))
        assert sent and " he " in f" {' '.join(sent)} "  # the owed draft, kept as written
    finally:
        r.close()


# -- echoes -------------------------------------------------------------------------------------------

ECHO_HIS = "ok gotta go, dentist appointment. later?"
ECHO = "yeah dentist appointment of course. have fun and text me after"


def echo_review(stores, d):
    if echo_opener(d.text, ECHO_HIS):
        return Verdict("rewrite", ("echo_opener",), echo_of=ECHO_HIS)
    return Verdict("send")


@pytest.mark.parametrize("channel", ["whisper", "dm:101"])
def test_an_owed_draft_kept_after_a_failed_rewrite_loses_the_echo(rig, monkeypatch, channel):
    """The audit's turn: echo_opener fired, the rewrite came to nothing, and the first draft
    was sent with its echo. The kept draft is cut before it goes."""
    monkeypatch.setattr(live, "review", echo_review)
    rig.script(ECHO, "(nothing)", "(nothing)", "(nothing)")
    sent, meta = speak(rig, turn(channel), TurnMeta("owed", why="reply:named", answers_human=True,
                                                    whisper_reply=channel == "whisper"))
    said = " ".join(sent).casefold()
    assert sent and "dentist" not in said and "me after" in said and "have fun" in said


@pytest.mark.parametrize("reply,his,left", [
    ("brb? sure take your time love", "brb", "sure take your time love"),
    ("be nice to him? you know im no angel", "be nice to him", "you know im no angel"),
    ("oh wait k? k then, be like that", "k", "k then, be like that"),
    ("loser?", "loser", "loser?"),                          # a reaction: one word and no more
    ("brb? ok", "brb", "brb? ok"),                          # too little would be left
    ("say it? say it then you coward", "say it", "say it? say it then you coward"),  # he asked
    ("raid at eight, dont be late", "raid at eight", "raid at eight, dont be late"),  # her answer
    ("dalaran fountain? in this weather, really", "meet me at the dalaran fountain after the raid",
     "dalaran fountain? in this weather, really"),          # not his whole line
])
def test_his_short_line_said_back_as_a_question_is_cut(reply, his, left):
    assert without_question_echo(reply, his) == left


def test_a_whisper_answer_never_opens_with_his_line_as_a_question(store, tmp_path):
    for coherence, opens in ((True, False), (False, True)):
        r = make_rig(store, engine_clock(), tmp_path / str(coherence),
                     reply="brb? sure take your time love", coherence=coherence)
        try:
            save_member(r.store, 101, typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0,
                        abbreviations={}, words_per_line=[3, 14])
            lid = r.store.insert_chat_line(Chat(0, "whisper", HUMAN, "Testmage", 101, "brb", 0, 10),
                                           session_id=None, origin="human")
            r.world.add_line(r.store.chat_lines([lid])[0], r.clock.now())
            sent = r.speak_turn(Turn(TurnKind.REPLY_HUMAN, 101, "whisper", addressee=HUMAN,
                                     to_guid=HUMAN, trigger_line=lid))
            assert sent and " ".join(sent).casefold().startswith("brb") is opens
        finally:
            r.close()


# -- game facts ---------------------------------------------------------------------------------------

LINES = ["Blade of Jin: epic one-hand sword, item level 200.",
         "Frost Trash (Icecrown, 25-player loot table): 2 items from: Grim Edge (251 one-hand "
         "axe), Bone Ward (251 shield), Chill Band (251 finger), Frost Hood (251 cloth head), "
         "Rime Legs (264 mail legs; 95 Emblem of Ice).",
         "Mind Gem: epic trinket, item level 232. Classes: Mage.",
         "Old Axe: rare two-hand axe, item level 100.",
         "Marrowgar is the first boss of Icecrown Citadel."]


def test_a_loot_table_name_with_a_comma_stays_whole():
    line = ("Frost Trash (Icecrown, 25-player loot table): 2 items from: Mantle of Fire, Ice and "
            "Shadow (251 mail shoulder), Grim Edge (251 one-hand sword), Frost Hood (251 cloth "
            "head; 60 Emblem of Frost), Chill Band (251 finger).")
    assert for_speaker([line], "Shaman") == [
        "Frost Trash (Icecrown, 25-player loot table): 2 items from: Mantle of Fire, Ice and "
        "Shadow (251 mail shoulder), Chill Band (251 finger)."]
    mage = for_speaker([line], "Mage")[0]
    assert "Mantle" not in mage and "Ice and Shadow" not in mage
    assert "Grim Edge (251 one-hand sword), Frost Hood (251 cloth head; 60 Emblem of Frost)" in mage


def test_item_facts_are_kept_to_what_her_class_can_use():
    shaman = for_speaker(LINES, "Shaman")
    assert shaman == [
        "Frost Trash (Icecrown, 25-player loot table): 2 items from: Grim Edge (251 one-hand "
        "axe), Bone Ward (251 shield), Chill Band (251 finger), Rime Legs (264 mail legs; 95 "
        "Emblem of Ice).", LINES[4]]
    mage = for_speaker(LINES, "Mage")
    assert LINES[0] in mage and LINES[2] in mage and "Frost Hood (251 cloth head)" in mage[1]
    assert "Grim Edge" not in mage[1] and "Rime Legs" not in mage[1]
    assert LINES[3] not in for_speaker(LINES, "Warrior")          # far under her level
    assert LINES[3] in for_speaker(LINES, "Warrior", level=40)
    assert for_speaker(LINES, "") == LINES                        # an unknown class: as written
    assert for_speaker(["Robe Pile: Ice Robe (264 cloth chest)."], "Warrior") == []


@pytest.mark.parametrize("desc,cls,ok", [
    ("one-hand sword", "Shaman", False), ("one-hand axe", "Shaman", True),
    ("two-hand sword", "Rogue", False), ("one-hand sword", "Rogue", True),
    ("mail legs", "Shaman", True), ("cloth shoulder", "Shaman", False),
    ("plate chest", "Paladin", True), ("finger", "Mage", True), ("totem", "Druid", False),
    ("wand", "Priest", True), ("bow", "Hunter", True), ("bow", "Paladin", False),
])
def test_usable_by_class(desc, cls, ok):
    assert usable(desc, cls) is ok


ITEM_CHUNKS = [chunk("Blade of Jin", "Blade of Jin: epic one-hand sword, item level 200.",
                     kind="item", source="world")]
ITEM_Q = "where does the blade of jin drop?"


def facts_prompt(store, tmp_path, *, cls, core=True, coherence=True):
    seed_test_cast(store)
    r = make_rig(store, engine_clock(), tmp_path / f"{cls}-{core}-{coherence}",
                 library=FakeLibrary(ITEM_CHUNKS), intent=core, coherence=coherence)
    try:
        async def go():
            m = r.world.members[103]
            r.world._touch_member(103, class_=cls)
            await r.human(ITEM_Q, channel="whisper", to_guid=103)
            await r.advance(45, step=2)
            assert m is not None
        r.sync(go())
        return r.prompts()[-1]
    finally:
        r.close()


def test_an_item_of_the_wrong_class_is_no_fact_and_she_says_she_is_not_sure(store, tmp_path):
    shaman = facts_prompt(store, tmp_path, cls=7)
    assert "Blade of Jin: epic" not in shaman and UNSURE in shaman
    warrior = facts_prompt(store, tmp_path, cls=1)
    assert "Blade of Jin: epic one-hand sword" in warrior and UNSURE not in warrior
    # without the switch, or without a core switch: the shared facts as before
    for kw in ({"coherence": False}, {"core": False}):
        old = facts_prompt(store, tmp_path, cls=7, **kw)
        assert "Blade of Jin: epic one-hand sword" in old and UNSURE not in old


# -- who is who in the prompt --------------------------------------------------------------------------

def said(r, text, *, guid, channel="guild", to_guid=0):
    lid = r.store.insert_chat_line(Chat(0, channel, guid, "x", to_guid, text, 0, 10),
                                   session_id=None, origin="human" if guid == HUMAN else "mind")
    row = r.store.chat_lines([lid])[0]
    r.world.add_line(row, r.clock.now())
    return row


def compose(r, t, meta):
    r.engine._draw_situation(t, meta, r.engine.personas.get(t.speaker), r.clock.now())
    return r.sync(r.engine._compose(t, meta, live=True)).messages[-1]["content"]


def test_every_chat_line_and_a_named_member_carry_who_they_are(store, tmp_path):
    seed_test_cast(store)
    for core in (True, False):
        r = make_rig(store, engine_clock(), tmp_path / str(core), intent=core)
        try:
            for g, gender in ((102, "female"), (103, "male"), (104, "female")):
                save_member(r.store, g, player_gender=gender)
            names = r.engine._names("guild")
            said(r, "first line from her", guid=102)
            said(r, "a line from him", guid=103)
            said(r, "second line from her", guid=102)
            q = said(r, f"is {names[104]} tanking tonight or what", guid=HUMAN)
            user = compose(r, Turn(TurnKind.REPLY_HUMAN, 101, "guild", addressee=HUMAN,
                                   trigger_line=q.id), TurnMeta(r.engine._new_id(),
                                                                answers_human=True))
            p104 = PersonaCache(r.store).get(104)
            roster = f"{names[104]} (she): plays"
            if core:
                assert f"{names[102]} (she): first line from her" in user
                assert f"{names[102]} (she): second line from her" in user
                assert roster in user and f"({p104.play.raid_role}, {p104.play.guild_role})" in user
            else:
                assert roster not in user
                assert user.count(f"{names[102]} (she)") <= 1  # once, as before
        finally:
            r.close()


def test_attendance_counts_only_where_the_talk_is_about_raids(store, tmp_path):
    seed_test_cast(store)
    store.conn.execute("INSERT INTO guild_raid VALUES (7,?,?,'icc',10,0,0,101,101,102,3,2,NULL)",
                       (1000.0, 2000.0))
    store.conn.execute("INSERT INTO guild_attendance VALUES (7, 101, 'benched')")
    store.conn.commit()
    for coherence in (True, False):
        r = make_rig(store, engine_clock(), tmp_path / str(coherence), intent=True,
                     coherence=coherence)
        r.clock._yields = 0
        r.engine.cfg = replace(r.engine.cfg, talk=replace(r.engine.cfg.talk, aware=True))
        try:
            def card(text):
                parent = source(r, text)
                meta = TurnMeta(r.engine._new_id(), why="test", answers_human=True)
                return compose(r, turn(trigger_line=parent), meta)
            for filler in ("what weapons do you want this winter", "the fishing was good today",
                           "anyone seen my hat"):
                other = card(filler)
            assert ("Attendance:" in other) is not coherence
            assert "Attendance: 1 benched" in card("who is on the bench for the raid tonight")
        finally:
            r.close()
