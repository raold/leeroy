"""The talk engine: replies to the human and the line pipeline (Task 16). Event reactions are
in test_engine_events.py."""
import asyncio
import json
import logging
import math
import random
import sqlite3
from dataclasses import replace

import pytest

from conftest import HUMAN, TEST_CAST, make_mem, make_persona, run, seed_test_cast
from engine_rig import (NICK, OVERLAY, REAL, FakeLibrary, chunk, engine_clock, make_rig,
                        numbered, save_member)
from leeroy_mind.bridge import AckTimeout, ConnectionLost, NotConnected
from leeroy_mind.config import HumanCfg, TalkCfg
from leeroy_mind.llm.presets import LIVE
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.memory.extract import Extractor
from leeroy_mind.memory.threads import ThreadTracker
from leeroy_mind.memory.writer import Embedder, MemoryWriter
from leeroy_mind.personas.io import PersonaCache, save_persona
from leeroy_mind.personas.schema import Bit
from leeroy_mind.personas.seed import human_seed
from leeroy_mind.protocol import (Achievement, Ack, Boss, Chat, Death, Group, Login, Logout,
                                  Member, Resurrect, TextEmote, Zone)
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.talk.budget import BurstPlan
from leeroy_mind.talk.engine import flirt_chance, share_reason_pool
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.romance import INVARIANTS
from leeroy_mind.talk.select import ReplyTurn
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_llm import script_from


ROMANCE_MARK = INVARIANTS.split(",")[1].strip()  # "two adults": in every romance text


@pytest.fixture
def clock():
    return engine_clock()


def reply_to_human(guid: int, channel: str = "guild") -> Turn:
    return Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid, channel=channel, addressee=HUMAN)


async def spoken(r, turn: Turn, seconds: float = 40, step: float = 1.0) -> list[str]:
    task = asyncio.create_task(r.engine.speak(turn))
    await r.advance(seconds, step)
    assert task.done()
    return task.result()


def first_name(store, guid: int) -> str:
    """A test-cast member's real first name, as the card gives it to the model."""
    return PersonaCache(store).get(guid).identity.real_name.split()[0]


def chat_rows(store) -> int:
    return store.conn.execute("SELECT count(*) FROM chat_line").fetchone()[0]


# -- replies to the human ----------------------------------------------------------------------

def test_reply_to_remark_sends_0_to_2(store, clock, tmp_path):
    async def main():
        counts = []
        for seed in range(20):
            r = make_rig(store, clock, tmp_path, seed=seed)
            await r.human("long day at work, finally home")
            await r.advance(45, step=2)
            says = r.sent.says()
            assert len(says) in (0, 1, 2), (seed, says)
            assert all(s.channel == "guild" and s.guid in r.world.online for s in says)
            assert len({s.guid for s in says}) == len(says)
            counts.append(len(says))
            await r.engine.end_session()
        assert {1, 2} <= set(counts)
    run(main())


def test_name_mention_gets_that_person(store, clock, tmp_path):
    async def main():
        for seed in range(5):
            r = make_rig(store, clock, tmp_path, seed=seed)
            await r.human("arkon you up?")
            await r.advance(40, step=2)
            says = r.sent.says()
            assert says and says[0].guid == 101, seed
            await r.engine.end_session()
    run(main())


def test_a_line_naming_one_member_gets_that_members_answer_only(store, clock, tmp_path):
    """Seen live (fix wave G5): "whatever you need <name>, just link it in chat" drew a second
    replier from the reason pool, who riffed on the first one's answer with the referents
    tangled. A line that names one member and speaks to nobody else is answered by that member
    alone, however many have a reason to answer him."""
    async def main():
        seed_test_cast(store)
        for g in (102, 103, 104):  # friendships he has lived: a real reason to answer him
            store.put_relationship(Relationship(a=g, b=HUMAN, affinity=45, familiarity=60,
                                                interactions=9))
        talk = numbered()  # one count across the rigs: their lines stay in the store
        for seed in range(40):
            r = make_rig(store, clock, tmp_path, seed=seed, script=talk)
            await r.human("whatever you need arkon, just link it in chat")
            await r.advance(45, step=2)
            assert [s.guid for s in r.sent.says()] == [101], seed
            await r.engine.end_session()
        r = make_rig(store, clock, tmp_path, script=talk)  # the pool is there for the guild
        await r.human("anyone up for a run tonight?")
        await r.advance(45, step=2)
        assert len(r.sent.says()) >= 2
        assert any(u["guid"] in (102, 103, 104) for u in r.utterances())
    run(main())


def test_a_line_naming_one_member_and_speaking_to_the_guild_keeps_the_guild(store, clock,
                                                                           tmp_path):
    async def main():
        talk = numbered()
        for seed in range(6):
            r = make_rig(store, clock, tmp_path, seed=seed, script=talk)
            await r.human("arkon, anyone up for a run tonight?")
            await r.advance(45, step=2)
            says = r.sent.says()
            assert says[0].guid == 101 and 2 <= len(says) <= 3, seed
            await r.engine.end_session()
    run(main())


def test_a_line_naming_two_members_is_answered_by_both(store, clock, tmp_path):
    async def main():
        talk = numbered()
        for seed in range(6):
            r = make_rig(store, clock, tmp_path, seed=seed, script=talk)
            await r.human("arkon and belra, just link it in chat")
            await r.advance(45, step=2)
            assert [s.guid for s in r.sent.says()][:2] == [101, 102], seed
            await r.engine.end_session()
    run(main())


def test_human_bot_command_gets_no_reply(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 101, 102)))
        for command in ("follow", "co +wait for attack", "attack"):
            await r.human(command, channel="party")
        await r.advance(30)
        assert r.sent.says() == [] and r.chat_calls() == []
        await r.human("you two ready for the next pull?", channel="party")  # positive control
        await r.advance(40)
        says = r.sent.says()
        assert says and all(s.channel == "party" and s.guid in (101, 102) for s in says)
    run(main())


def test_typing_delay_never_instant(store, clock, tmp_path):
    async def main():
        for seed in range(5):
            r = make_rig(store, clock, tmp_path, seed=seed)
            t0 = clock.now()
            await r.human("arkon you up?")
            await r.advance(30, step=0.5)
            first, _ = r.sent.timed_says()[0]
            assert first - t0 >= 2.0
            await r.engine.end_session()
    run(main())


def test_goodbye_gets_two_or_three_goodnights(store, clock, tmp_path):
    async def main():
        for seed in range(4):
            r = make_rig(store, clock, tmp_path, seed=seed)
            await r.human("gn all, off to bed")
            await r.advance(60, step=2)
            assert 2 <= len(r.sent.says()) <= 3
            assert all("is logging off" in p for p in r.prompts())
            await r.engine.end_session()
    run(main())


def test_quest_talk_brings_in_the_women(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 104, player_gender="female")
        for seed in range(6):
            r = make_rig(store, clock, tmp_path, seed=seed)
            await r.human("where do i turn in this quest?")
            await r.advance(45, step=2)
            assert 104 in {s.guid for s in r.sent.says()}, seed
            await r.engine.end_session()
    run(main())


def test_her_reply_to_his_quest_line_ends_on_an_offer_to_come_along(store, clock, tmp_path):
    """Rehearsal 2 (judge C 5): nobody offered to come along on his quest, the help rule sat in
    the standing rules. It ends a woman's reply to his quest line now, and nobody else's."""
    async def main():
        seed_test_cast(store)
        save_member(store, 104, player_gender="female", stance="drawn")
        r = make_rig(store, clock, tmp_path)
        quest = his_line(r, store, "anyone know where the argent tournament dailies start?").id
        chat = his_line(r, store, "dunna how was work today").id

        async def last(guid, line, kind=TurnKind.REPLY_HUMAN):
            msgs, _ = await r.engine.compose(Turn(kind=kind, speaker=guid, channel="guild",
                                                  addressee=HUMAN, trigger_line=line))
            return msgs[0]["content"], msgs[1]["content"].rsplit("\n\n", 1)[1]
        system, end = await last(104, quest)
        assert end.endswith("You only know which quests Zan has done if he told you.")
        assert "end your line with an offer to help him with it or to come along" in end
        assert "quest" not in system
        for guid, line, kind in ((101, quest, TurnKind.REPLY_HUMAN),
                                 (104, chat, TurnKind.REPLY_HUMAN),
                                 (104, None, TurnKind.GREET)):
            system, end = await last(guid, line, kind)
            assert "jump at it" not in system + end, (guid, kind)
    run(main())


def test_who_answers_him_comes_down_to_real_reasons(store, clock, tmp_path):
    """Spec I1 and its ruling, through the engine: a lust woman's seeded row and a friend row
    relinked from a persona (no interactions) are no reason to answer him; a friendship he has
    lived with a member is, and so is an open thread (he answered him a minute ago)."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust")
        store.put_relationship(human_seed(102, HUMAN, "lust"))
        store.put_relationship(Relationship(a=103, b=HUMAN, affinity=40, trust=40,
                                            familiarity=60, label="friend"))
        store.put_relationship(Relationship(a=104, b=HUMAN, affinity=45, familiarity=60,
                                            interactions=9))
        r = make_rig(store, clock, tmp_path, script=numbered())

        def whys(since):
            return {u["guid"]: u["why"] for u in store.conn.execute(
                "SELECT guid, why FROM utterance WHERE id > ? AND kind = 'reply_human'",
                (since,))}

        def last_id():
            return store.conn.execute("SELECT coalesce(max(id), 0) FROM utterance").fetchone()[0]
        await r.human("evening all")
        await r.advance(60, step=2)
        first = whys(0)
        assert first.get(104) == "reply:reason_pool"
        assert all(w != "reply:reason_pool" for g, w in first.items() if g != 104)
        seen = last_id()
        await r.human("ebbo you still around?")
        await r.advance(40, step=2)
        assert whys(seen).get(105) == "reply:named"
        talked = set(whys(0))  # everyone who has answered him so far
        ctx = r.engine._context(clock.now(), set())
        _, reasons = r.engine._weigh([101, 102, 103, 104, 105], last=HUMAN, ctx=ctx, reply=True)
        assert reasons == talked | {104} and 105 in reasons  # open threads, the lived friend
        await r.advance(301)
        _, reasons = r.engine._weigh([101, 102, 103, 104, 105], last=HUMAN,
                                     ctx=r.engine._context(clock.now(), set()), reply=True)
        assert reasons == {104}  # the threads have gone quiet
    run(main())


def test_2010s_slang_is_asked_again_in_2009s_words(store, clock, tmp_path):
    """Rehearsal 2 (judge A 7): "the drake is fire" from a 2009 guildie. A soft tell: asked
    again with the hint, and the retry goes out; the rules say it too."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, abbreviations={}, typo_rate=0.0, split_rate=0.0,
                    emoticon_rate=0.0)
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["the drake is fire", "the drake is sick, worth the grind"]))
        assert await spoken(r, reply_to_human(102)) == ["the drake is sick, worth the grind"]
        system, user = r.chat_calls()[-1]["messages"][0]["content"], r.prompts()[-1]
        assert "Say it the way people typed in 2009." in user
        assert "- Your slang is 2009 slang." in system
    run(main())


def test_her_card_and_the_chat_back_what_she_says_of_the_past(store, clock, tmp_path):
    """Fix round 1 (review I5): the history tell dropped her own life ("we've been married for
    10 years", her household) and tonight's chat. Her card and the chat shown back a claim."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0,
                    abbreviations={}, household="married, two kids", words_per_line=[3, 20],
                    money="the bike shop has been losing money all year")
        for line in ("we've been married for 10 years and he still can't boil an egg",
                     "by the time we pull the buffs are gone",
                     "the shop has been losing money for 2 years"):
            r = make_rig(store, clock, tmp_path, script=lambda m, msgs, line=line: line)
            await r.advance(70)
            assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=102, channel="guild")) == [
                line], line
        again = "coros you died first again"
        r = make_rig(store, clock, tmp_path, script=lambda m, msgs: again)
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=102, channel="guild")) == []
        r = make_rig(store, clock, tmp_path, script=lambda m, msgs: again)
        member_said(r, store, 101, "coros died first on the last pull, again")
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=102, channel="guild")) == [
            again]  # the chat shows it
        r = make_rig(store, clock, tmp_path, script=lambda m, msgs: again)
        line = his_line(r, store, "coros died first on the last pull, again").id
        await r.advance(70)
        turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN,
                    trigger_line=line)
        assert await spoken(r, turn) == []  # the line she answers backs nothing she adds
    run(main())


def member_said(r, store, guid: int, text: str):
    """A member's line in the store and the world (no turn planned for it)."""
    row = store.chat_lines([store.insert_chat_line(Chat(
        ts=0, channel="guild", from_guid=guid, from_name=r.world.members[guid].name, to_guid=0,
        text=text, map=0, zone=10), session_id=None, origin="game")])[0]
    r.world.add_line(row, r.clock.now())
    return row


def test_shared_history_nobody_remembers_is_asked_again_then_dropped(store, clock, tmp_path):
    """The controller's item 1b: an invented "remember when" is asked again with the hint;
    still there on the retry, the line is dropped. A claim a memory backs goes out."""
    async def main():
        seed_test_cast(store)
        for g in (102, 103):
            save_member(store, g, abbreviations={}, typo_rate=0.0, split_rate=0.0,
                        emoticon_rate=0.0)
        invented = "remember when you pulled that entire wing in vanilla?"
        r = make_rig(store, clock, tmp_path, script=script_from([invented, invented]))
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=102, channel="guild")) == []
        assert r.utterances()[-1]["dropped"] == "tells"
        assert "Only bring up the past from what you remember." in r.prompts()[-1]
        r = make_rig(store, clock, tmp_path, script=script_from([invented, "ok who wants ony"]))
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=102, channel="guild")) == [
            "ok who wants ony"]
        t = int(clock.now()) - 400 * 86400
        store.insert_memory(make_mem(owner=103, text="Zan pulled the entire east wing in "
                                     "vanilla and we all died.", actors=(HUMAN,), t_event=t,
                                     t_created=t, t_accessed=t, source="owner"))
        r = make_rig(store, clock, tmp_path, script=script_from([invented]))
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=103, channel="guild")) == [
            invented]
    run(main())


def test_no_kind_of_turn_copies_a_line_in_its_chat(store, clock, tmp_path):
    """Rehearsal 2, finding 2: a member's opener was another's line from 35 s before, word for
    word; only answers to him were checked against other members. Every kind is now: a copy
    is asked again with the hint, then dropped."""
    async def main():
        seed_test_cast(store)
        for g in (102, 103):
            save_member(store, g, abbreviations={}, typo_rate=0.0, split_rate=0.0,
                        emoticon_rate=0.0)
        copied = "arkon reading poetry to zan is the most perfect thing ive ever heard"
        r = make_rig(store, clock, tmp_path, script=script_from(
            [copied, copied, copied, copied, "who's got flasks"]))
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=102, channel="guild")) == [
            copied]
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=103, channel="guild")) == []
        assert r.utterances()[-1]["dropped"] == "tells"
        assert "Don't copy a line from the chat" in r.prompts()[-1]
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.CONTINUE, speaker=103, channel="guild",
                                    addressee=101)) == ["who's got flasks"]
    run(main())


def test_his_question_answered_from_memory_is_no_echo(store, clock, tmp_path):
    """Judge B 2: a member's right first draft repeated his question's words, which the
    member's memory held; the echo retry made it say the opposite. The subject a memory holds
    is allowed."""
    async def main():
        seed_test_cast(store)
        save_member(store, 101, abbreviations={}, typo_rate=0.0, split_rate=0.0,
                    emoticon_rate=0.0, words_per_line=[3, 16])
        t = int(clock.now()) - 300 * 86400
        store.insert_memory(make_mem(owner=101, text="I took over as the guild bank keeper "
                                     "for the alts last spring, after Belra quit it.",
                                     actors=(HUMAN, 102), t_event=t, t_created=t, t_accessed=t,
                                     source="owner"))
        draft = "i'm still the guild bank keeper for the alts zan. belra's got nothing on me"
        r = make_rig(store, clock, tmp_path, script=script_from([draft]))
        line = his_line(r, store, "arkon you still the guild bank keeper for the alts or did "
                                  "belra finally give up on that whole mess?")
        turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild", addressee=HUMAN,
                    trigger_line=line.id)
        assert await spoken(r, turn) == [draft]
    run(main())


def test_a_bystander_with_no_memory_of_it_leaves_their_story_to_them(store, clock, tmp_path):
    """Judge B 2: he asked two members about their running joke, and a third answerer with no
    memory of it invented a span and a winner. A member he didn't name, none of whose
    memories share his line's words, is told it's their story: react, don't add facts."""
    async def main():
        seed_test_cast(store)
        t = int(clock.now()) - 300 * 86400
        store.insert_memory(make_mem(owner=104, text="Belra still owes Arkon for the epic gem "
                                     "he lent her.", actors=(101, 102), t_event=t, t_created=t,
                                     t_accessed=t, source="owner"))
        r = make_rig(store, clock, tmp_path)
        line = his_line(r, store, "arkon did belra ever pay you back for that epic gem?").id
        story = "Zan asked Arkon and Belra: it's their story, so react to it and don't add facts."

        async def user(guid):
            msgs, _ = await r.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN, speaker=guid,
                                                  channel="guild", addressee=HUMAN,
                                                  trigger_line=line))
            return msgs[1]["content"]
        assert story in await user(103)  # no memory of it
        assert story not in await user(104)  # remembers the gem
        assert story not in await user(101)  # he asked Arkon
    run(main())


def test_their_story_is_only_for_his_questions_to_them_in_public(store, clock, tmp_path):
    """Fix round 1 (review minor 1): it fired on any line of his naming a member, a gz and a
    whisper included ("Zan asked Arkon" in a whisper about Arkon)."""
    async def main():
        seed_test_cast(store)
        r = make_rig(store, clock, tmp_path)
        whisper = his_line(r, store, "is arkon ok? he seemed upset tonight", channel="whisper",
                           to_guid=104).id
        msgs, _ = await r.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN, speaker=104,
                                              channel="whisper", to_guid=HUMAN,
                                              addressee=HUMAN, trigger_line=whisper))
        assert "their story" not in msgs[1]["content"]
        gz = his_line(r, store, "gz arkon on the drake!").id
        msgs, _ = await r.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN, speaker=104,
                                              channel="guild", addressee=HUMAN, trigger_line=gz))
        assert "their story" not in msgs[1]["content"]
    run(main())


def test_her_feelings_show_at_greet_and_goodnight_only_at_the_no_brakes_dial(store, clock,
                                                                              tmp_path):
    """Fix round 1 (review minor 2): the ruling's greet and goodnight line is for dial 9-10."""
    async def main():
        seed_test_cast(store)
        save_member(store, 104, player_gender="female", stance="fond")
        r = make_rig(store, clock, tmp_path)
        feel = "Let how you feel about him show, if a memory fits."
        for dial, shown in ((1, False), (8, False), (9, True)):
            r.engine.cfg = replace(r.cfg, romance=replace(r.cfg.romance, dial=dial))
            msgs, _ = await r.engine.compose(Turn(kind=TurnKind.GREET, speaker=104,
                                                  channel="guild", addressee=HUMAN))
            assert (feel in msgs[1]["content"]) is shown, dial
    run(main())


def test_a_whisper_of_a_line_or_two_says_it_is_more_than_usual(store, clock, tmp_path):
    """Fix round 1 (review minor 10): the rules said "one line" while the explicit whisper's
    instruction said "a line or two"; the instruction now says it is the exception."""
    async def main():
        _women(store)
        r = make_rig(store, clock, tmp_path)
        line = his_line(r, store, "meet me at the inn, just us", channel="whisper",
                        to_guid=102).id
        msgs, _ = await r.engine.compose(whisper_back(102, line))
        assert "in a line or two, more than your usual one line" in msgs[1]["content"]
    run(main())


def test_a_whisper_reply_counts_no_guild_line_as_an_answer(store, clock, tmp_path):
    """Judge B 8: her whisper prompt said "Dunna already answered him" for a guild line to
    someone else. Only the same conversation counts: a whisper's own pair."""
    async def main():
        seed_test_cast(store)
        r = make_rig(store, clock, tmp_path, script=numbered())
        line = his_line(r, store, "you around?", channel="whisper", to_guid=104)
        task = asyncio.create_task(r.engine.speak(Turn(kind=TurnKind.OPEN, speaker=102,
                                                       channel="guild")))
        await r.advance(30)
        assert task.result()
        msgs, _ = await r.engine.compose(whisper_back(104, line.id))
        assert "already answered" not in msgs[1]["content"]
        meta = TurnMeta("t", answers_human=True)
        assert r.engine._earlier_answers(whisper_back(104, line.id), meta,
                                         r.world.recent_visible(104)) == []
    run(main())


def test_a_small_reason_pool_leaves_one_reply_to_the_weighted_draw():
    """Task 13 carry: when the members with a reason are no more than the replies, at most
    n-1 come from them and the last is one weighted draw over everyone left."""
    weights = {g: 1.0 for g in (101, 102, 103, 104, 105)}
    two = [ReplyTurn(101, "r-0", TurnKind.REPLY_HUMAN, "guild", "guild"),
           ReplyTurn(102, "r-1", TurnKind.REPLY_HUMAN, "guild", "guild")]
    seen = set()
    for seed in range(40):
        out = share_reason_pool(two, {101, 102}, weights, [], random.Random(seed))
        assert out[0] == two[0] and out[1].turn_id == "r-1" and out[1].speaker != 101
        seen.add(out[1].speaker)
    assert {102} < seen  # the pool member can still win the draw, others do too
    three = [*two, ReplyTurn(104, "r-2", TurnKind.REPLY_HUMAN, "guild", "guild")]
    assert share_reason_pool(three, {101, 102}, weights, [], random.Random(1)) == three
    big = {101, 102, 103}  # a pool larger than the replies is drawn from freely
    assert share_reason_pool(two, big, weights, [], random.Random(1)) == two
    called = [ReplyTurn(103, "r-0", TurnKind.REPLY_HUMAN, "guild", "guild"), two[1]]
    out = share_reason_pool(called, {102, 103}, weights, [103], random.Random(3))
    assert out[0].speaker == 103  # someone named keeps their reply


# -- the line pipeline -------------------------------------------------------------------------

def test_tells_regenerate_once_then_drop(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, reply="Happy to help! Let me know.")
        assert await spoken(r, reply_to_human(101)) == []
        assert r.sent.says() == [] and len(r.chat_calls()) == 2
        assert "Say it the way Arkon would type it in game chat" in r.prompts()[1]
        assert r.utterances()[-1]["dropped"] == "tells"
        # a retry whose tells are only soft is accepted
        r = make_rig(store, clock, tmp_path,
                     script=script_from(["sure thing, let me know", "on my way, let me know"]))
        assert await spoken(r, reply_to_human(102))
        assert len(r.chat_calls()) == 2 and len(r.sent.says()) >= 1
    run(main())


def test_speak_leaves_the_callers_turn_alone(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        turn = Turn(kind=TurnKind.OPEN, speaker=101, channel="")  # the pipeline picks a channel
        assert await spoken(r, turn) and r.sent.says()[0].channel == "guild"
        assert (turn.channel, turn.deadline, turn.priority) == ("", 0.0, Priority.SOCIAL)
        await r.advance(60)
        assert await spoken(r, turn)  # proposed again, it is a fresh turn, not an expired one
    run(main())


def test_compose_changes_no_live_state(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        store.put_relationship(Relationship(a=101, b=HUMAN, affinity=60, familiarity=70))
        t = int(clock.now()) - 86400
        mid = store.insert_memory(make_mem(
            owner=101, kind="conversation", text="Zan said he starts the new job on Monday.",
            actors=(HUMAN,), t_event=t, t_created=t, t_accessed=t, source="extract",
            evidence=("chat:1",)))
        save_member(store, 101, player_gender="female", stance="lust")  # her turns draw
        r = make_rig(store, clock, tmp_path)
        live = r.engine.rng.getstate()
        for _ in range(20):
            msgs, ids = await r.engine.compose(reply_to_human(101))
        assert ids == [mid] and "new job on Monday" in msgs[1]["content"]
        assert r.engine.rng.getstate() == live  # its draws came from its own generator
        (m,) = store.get_memories([mid])
        assert m.t_accessed == t and m.access_n == 0  # quizzing marks nothing accessed
    run(main())


def test_a_turn_spends_its_cooldowns_and_rests_its_memories_only_once_said(store, clock,
                                                                          tmp_path):
    """Correctness M6: compose started her romance cooldown, took the guild-wide real-name slot
    and rested the memories it recalled before anyone knew the line would be said, so a turn
    dropped after it ("(nothing)", tells, expired) still kept the name from every inner-circle
    member for ten minutes. Now the three are spent with the turn's first line."""
    said = [False]
    lines = numbered("sure thing, round {n}")

    def script(model, messages):
        return lines(model, messages) if said[0] else "(nothing)"

    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust")
        store.put_relationship(Relationship(a=102, b=HUMAN, affinity=60, trust=40,
                                            familiarity=85, interactions=30))
        t = int(clock.now()) - 86400
        mid = store.insert_memory(make_mem(
            owner=102, kind="conversation", text="Zan said he starts the new job on Monday.",
            actors=(HUMAN,), t_event=t, t_created=t, t_accessed=t, source="extract",
            evidence=("chat:1",)))
        r = make_rig(store, clock, tmp_path, script=script)

        def offered() -> bool:  # each on a share of turns
            last = r.prompts()[-1]
            return f"real name, {REAL}" in last and ROMANCE_MARK in last

        for _ in range(60):
            assert await spoken(r, reply_to_human(102), seconds=15, step=5) == []
            if offered():
                break
        assert offered() and r.utterances()[-1]["dropped"] == "silent"
        assert "new job on Monday" in r.prompts()[-1]
        assert r.engine._real_name_at == -math.inf and 102 not in r.engine._romance_at
        (m,) = store.get_memories([mid])
        assert m.t_accessed == t and m.access_n == 0
        said[0] = True
        for _ in range(60):  # 25 s apart: her lines are said now, ten seconds apart at least
            assert await spoken(r, reply_to_human(102), seconds=25, step=5)
            if offered():
                break
        assert offered()
        assert r.engine._real_name_at > -math.inf and 102 in r.engine._romance_at
        (m,) = store.get_memories([mid])
        assert m.t_accessed > t and m.access_n >= 1
    run(main())


def test_repairable_tell_is_repaired(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, reply="nice pull — gg; ty")
        lines = await spoken(r, reply_to_human(101))
        assert lines and not any("—" in x or ";" in x for x in lines)
        assert len(r.chat_calls()) == 1
    run(main())


def test_silence_output_sends_nothing(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, reply="(nothing)")
        assert await spoken(r, reply_to_human(101)) == []
        assert r.sent.says() == [] and r.utterances()[-1]["dropped"] == "silent"
    run(main())


@pytest.mark.parametrize("reply", [
    "Belra: (nothing)", "Belra: *says nothing*", "[Belra]: (nothing)", "Belra (guild): (nothing)",
    "(whisper to Zan): (nothing)", "[just now] Belra: (nothing)", "{first}: (nothing)",
    '"Belra: (nothing)"'])
def test_silence_behind_a_copied_tag_sends_nothing(store, clock, tmp_path, reply):
    """Prompts I1: "(nothing)" is checked again after every repair ({first} is Belra's real
    first name in the test cast)."""
    async def main():
        seed_test_cast(store)
        first = first_name(store, 102)
        r = make_rig(store, clock, tmp_path, reply=reply.format(first=first))
        assert await spoken(r, reply_to_human(102)) == []
        assert r.sent.says() == [] and r.utterances()[-1]["dropped"] == "silent"
    run(main())


def test_a_copied_tag_never_reaches_chat(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0)
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["[just now] Belra (guild): lol same", f"{first_name(store, 102)}: on my way"]))
        assert await spoken(r, reply_to_human(102)) == ["lol same"]
        await r.advance(15)
        assert await spoken(r, reply_to_human(102)) == ["on my way"]
    run(main())


# The rehearsal's whisper: he teased her, and her reply was his own line with a copied tag.
HIS_WHISPER = ("hey belra, heard you topped the meters again ;) what does a mage have to do to "
               "get a summon to dalaran after raid?")


def his_line(r, store, text: str, *, channel: str = "guild", to_guid: int = 0):
    """His line as the app logs it, without the engine planning its answers."""
    row = store.chat_lines([store.insert_chat_line(Chat(
        ts=0, channel=channel, from_guid=HUMAN, from_name="Testmage", to_guid=to_guid, text=text,
        map=0, zone=10), session_id=None, origin="human")])[0]
    r.world.add_line(row, r.clock.now())
    return row


def whisper_back(speaker: int, line: int) -> Turn:
    return Turn(kind=TurnKind.REPLY_HUMAN, speaker=speaker, channel="whisper", to_guid=HUMAN,
                addressee=HUMAN, trigger_line=line)


# -- romance reaches him (rehearsal 2, judge C 1, 2, 6) ---------------------------------------

def _women(store) -> None:
    seed_test_cast(store)
    for g, stance in ((102, "lust"), (103, "drawn"), (104, "fond")):
        save_member(store, g, player_gender="female", stance=stance)


def test_every_womans_whisper_back_to_him_carries_her_romance(store, clock, tmp_path):
    """His flirty whisper got a coy yes: the share's roll had left her romance text out. Every
    reply on the whisper channel to him carries it now, and so does a public reply to a line
    of his that is a flirt; a public reply to a plain line keeps the share."""
    async def main():
        _women(store)
        r = make_rig(store, clock, tmp_path)
        flirt = his_line(r, store, "hey you ;) wear that red dress tonight? just us").id
        plain = his_line(r, store, "anyone seen my hearthstone").id

        async def carried(turn) -> int:
            got = 0
            for _ in range(20):
                msgs, _ = await r.engine.compose(turn)
                got += ROMANCE_MARK in msgs[1]["content"]
            return got
        for g in (102, 103, 104):
            line = his_line(r, store, "you around later?", channel="whisper", to_guid=g).id
            assert await carried(whisper_back(g, line)) == 20, g
            reply = Turn(kind=TurnKind.REPLY_HUMAN, speaker=g, channel="guild", addressee=HUMAN,
                         trigger_line=flirt)
            assert await carried(reply) == 20, g
        assert await carried(Turn(kind=TurnKind.REPLY_HUMAN, speaker=104, channel="guild",
                                  addressee=HUMAN, trigger_line=plain)) < 20
    run(main())


def test_a_drawn_woman_he_talks_to_carries_it_more_at_dial_nine(store, clock, tmp_path):
    """The controller's ruling (the owner's dial 9, "they all want him"): her share is 0.75
    when he talks to her, 0.55 below dial 9."""
    async def main():
        _women(store)
        r = make_rig(store, clock, tmp_path)
        plain = his_line(r, store, "anyone seen my hearthstone").id
        reply = Turn(kind=TurnKind.REPLY_HUMAN, speaker=103, channel="guild", addressee=HUMAN,
                     trigger_line=plain)

        async def share(dial: int) -> float:
            r.engine.cfg = replace(r.cfg, romance=replace(r.cfg.romance, dial=dial))
            r.engine._compose_rng.seed("share")
            got = 0
            for _ in range(400):
                msgs, _ = await r.engine.compose(reply)
                got += ROMANCE_MARK in msgs[1]["content"]
            return got / 400
        nine, eight = await share(9), await share(8)
        assert 0.68 <= nine <= 0.82 and 0.47 <= eight <= 0.63, (nine, eight)
    run(main())


# -- a member's own dial (fix wave G4) --------------------------------------------------------

def test_a_woman_at_her_own_dial_ten_carries_romance_on_every_turn_to_him(store, clock,
                                                                         tmp_path):
    """Her share is 1.0 at her own dial 10, the cooling-off after a line that carried it
    included, whatever her attitude; a woman with none of her own keeps the share."""
    async def main():
        seed_test_cast(store)
        for g, stance, dial in ((102, "lust", 10), (103, "lust", None), (104, "fond", 10),
                                (105, "drawn", 9)):
            save_member(store, g, player_gender="female", stance=stance, dial=dial)
        r = make_rig(store, clock, tmp_path)
        plain = his_line(r, store, "anyone seen my hearthstone").id
        for g in (102, 103, 104, 105):
            r.engine._romance_at[g] = r.clock.now()  # each has just said a line that carried it

        async def carried(turn, draws: int) -> float:
            r.engine._compose_rng.seed("share")
            got = 0
            for _ in range(draws):
                msgs, _ = await r.engine.compose(turn)
                got += ROMANCE_MARK in msgs[1]["content"]
            return got / draws
        for kind, line in ((TurnKind.REPLY_HUMAN, plain), (TurnKind.GREET, 0)):
            def turn(g):
                return Turn(kind=kind, speaker=g, channel="guild", addressee=HUMAN,
                            trigger_line=line)
            assert await carried(turn(102), 80) == 1.0 and await carried(turn(104), 80) == 1.0
            assert 0.28 <= await carried(turn(103), 300) <= 0.47  # her 0.75, halved cooling
            assert await carried(turn(105), 80) < 1.0  # a 9 is not a 10
    run(main())


def test_the_owners_dial_governs_a_woman_without_her_own_and_hers_replaces_it(store, clock,
                                                                              tmp_path):
    async def main():
        seed_test_cast(store)
        for g, dial in ((102, None), (103, 9), (104, 3)):
            save_member(store, g, player_gender="female", stance="lust", dial=dial)

        async def explicit(owners: int) -> list[bool]:  # the local overlay is the explicit band
            r = make_rig(store, clock, tmp_path, dial=owners)
            out = []
            for g in (102, 103, 104):
                msgs, _ = await r.engine.compose(Turn(kind=TurnKind.FLIRT, speaker=g,
                                                      channel="guild", addressee=HUMAN))
                out.append(OVERLAY in msgs[1]["content"])
            return out
        assert await explicit(3) == [False, True, False]  # the public default
        assert await explicit(9) == [True, True, False]
    run(main())


def test_her_feelings_and_her_talked_to_share_follow_her_own_dial(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        for g, stance, dial in ((102, "fond", 9), (103, "fond", 8), (104, "drawn", 9),
                                (105, "drawn", None)):
            save_member(store, g, player_gender="female", stance=stance, dial=dial)
        feel = "Let how you feel about him show, if a memory fits."
        for owners in (1, 9):
            r = make_rig(store, clock, tmp_path, dial=owners)
            for g, shown in ((102, True), (103, False)):
                msgs, _ = await r.engine.compose(Turn(kind=TurnKind.GREET, speaker=g,
                                                      channel="guild", addressee=HUMAN))
                assert (feel in msgs[1]["content"]) is shown, (owners, g)
        r = make_rig(store, clock, tmp_path, dial=3)
        plain = his_line(r, store, "anyone seen my hearthstone").id

        async def share(g: int) -> float:
            r.engine._compose_rng.seed("share")
            got = 0
            for _ in range(300):
                msgs, _ = await r.engine.compose(Turn(
                    kind=TurnKind.REPLY_HUMAN, speaker=g, channel="guild", addressee=HUMAN,
                    trigger_line=plain))
                got += ROMANCE_MARK in msgs[1]["content"]
            return got / 300
        hers, plain_share = await share(104), await share(105)  # 0.75 at her 9, 0.55 at his 3
        assert 0.67 <= hers <= 0.83 and 0.46 <= plain_share <= 0.64, (hers, plain_share)
    run(main())


def test_the_whisper_overlay_is_the_last_thing_she_reads(store, clock, tmp_path):
    """At the explicit band in a whisper to him the owner's whisper overlay comes after the
    turn's own instruction, and her reply may run to a line or two (rehearsal 2, judge C 2:
    the overlay sat mid-paragraph and "one short line" came last). Absent: as before."""
    async def main():
        _women(store)
        save_member(store, 102, player_gender="female", stance="lust", split_rate=0.0,
                    typo_rate=0.0, emoticon_rate=0.0, abbreviations={}, words_per_line=[3, 12])
        reply = ("you better be serious. i'm wearing the red one and nothing under the cloak "
                 "tonight, so come and find me")
        r = make_rig(store, clock, tmp_path, reply=reply)
        line = his_line(r, store, "meet me at the inn after raid, just us", channel="whisper",
                        to_guid=102).id
        msgs, _ = await r.engine.compose(whisper_back(102, line))  # no overlay: no tail
        assert msgs[1]["content"].endswith("only if it really fits.")
        assert "in a line or two" in msgs[1]["content"] and "WHISPER-OVERLAY" not in str(msgs)
        (tmp_path / "prompts" / "romance_explicit_whisper.txt").write_text(
            "WHISPER-OVERLAY\n", encoding="utf-8")
        msgs, _ = await r.engine.compose(whisper_back(102, line))
        user = msgs[1]["content"]
        assert user.endswith("WHISPER-OVERLAY") and "in a line or two" in user
        assert user.rindex("Answer Zan's last line") < user.rindex("WHISPER-OVERLAY")
        guild = his_line(r, store, "who's up for heroics").id
        msgs, _ = await r.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102,
                                              channel="guild", addressee=HUMAN,
                                              trigger_line=guild))
        assert "WHISPER-OVERLAY" not in msgs[1]["content"]
        assert len(await spoken(r, whisper_back(102, line))) == 2  # a line or two, sent
    run(main())


def test_her_greeting_and_goodnight_let_her_feelings_show(store, clock, tmp_path):
    async def main():
        _women(store)
        r = make_rig(store, clock, tmp_path)
        night = his_line(r, store, "night all").id
        feel = "Let how you feel about him show, if a memory fits."
        for g, kind, line in ((102, TurnKind.GREET, None), (104, TurnKind.GOODNIGHT, night),
                              (101, TurnKind.GREET, None), (105, TurnKind.GOODNIGHT, night)):
            msgs, _ = await r.engine.compose(Turn(kind=kind, speaker=g, channel="guild",
                                                  addressee=HUMAN, trigger_line=line))
            assert (feel in msgs[1]["content"]) is (g in (102, 104)), (g, kind)
    run(main())


def test_a_reply_that_parrots_his_line_is_asked_again_then_dropped(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0)
        parrot = f"(whisper to Belra): {HIS_WHISPER}"
        r = make_rig(store, clock, tmp_path, script=script_from(
            [parrot, "lol a summon costs a drink at the inn"]))
        line = his_line(r, store, HIS_WHISPER, channel="whisper", to_guid=102)
        assert await spoken(r, whisper_back(102, line.id)) == ["lol a summon costs a drink at "
                                                               "the inn"]
        assert len(r.chat_calls()) == 2 and "own words" in r.prompts()[1]
        r = make_rig(store, clock, tmp_path, script=script_from([parrot, HIS_WHISPER]))
        await r.advance(15)
        assert await spoken(r, whisper_back(102, line.id)) == []
        assert r.utterances()[-1]["dropped"] == "tells" and r.sent.says() == []
    run(main())


def test_a_reply_far_over_her_length_is_asked_again_shorter(store, clock, tmp_path):
    long_reply = ("lol yeah that pull was a mess, we should have waited for the healers and the "
                  "tank was not even ready")
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, words_per_line=[1, 4])
        r = make_rig(store, clock, tmp_path, script=script_from([long_reply, "bad pull lol"]))
        assert await spoken(r, reply_to_human(102)) == ["bad pull lol"]
        assert len(r.chat_calls()) == 2 and "Shorter: 1-4 words" in r.prompts()[1]
        # a reply that fits the turn's lines is never asked again
        r = make_rig(store, clock, tmp_path, script=script_from(["bad pull. we wiped"]))
        await r.advance(15)
        assert await spoken(r, reply_to_human(102)) and len(r.chat_calls()) == 1
    run(main())


def test_a_sample_line_word_for_word_is_asked_again(store, clock, tmp_path):
    """Prompts minor 7: the same sample lines ride in every prompt, and one said word for word
    went out as if it were new."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0)
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["ok pulling in 5", "two min, grabbing a drink"]))  # her sample, then her own
        assert await spoken(r, reply_to_human(102)) == ["two min, grabbing a drink"]
        assert len(r.chat_calls()) == 2 and "examples only show how you type" in r.prompts()[1]
    run(main())


def test_own_lines_show_what_was_said(store, clock, tmp_path):
    """Prompts C1: "Your last lines" holds what the guild saw, not the words the style cut."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0,
                    words_per_line=[2, 6])
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["run it for the trinket. emblems rock.", "and bring flasks"]))
        assert await spoken(r, reply_to_human(102)) == ["run it for the trinket"]
        await r.advance(15)
        assert await spoken(r, reply_to_human(102))
        mine = r.prompts()[1].split("Your last lines (don't reuse their wording):")[1]
        assert "run it for the trinket" in mine and "emblems" not in mine
    run(main())


ANSWERS = ["north of icecrown, by the big tents", "talk to the crusader at the gate",
           "you need exalted with your city first", "bring a lance lol",
           "mounted combat, it is a whole thing", "they reset at the daily reset"]


def test_the_second_and_third_answers_may_add_something_or_stay_quiet(store, clock, tmp_path):
    """Prompts I8: replies run one after another; the later ones see the earlier ones and are
    told not to repeat them, with (nothing) as an answer."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=script_from(ANSWERS))
        await r.human("anyone know where the argent tournament dailies start?")
        await r.advance(60, step=2)
        prompts = r.prompts()
        assert len(prompts) >= 2 and len(r.sent.says()) >= 2
        assert "already answered" not in prompts[0]
        said = [r.engine.personas.get(s.guid).character_name for s in r.sent.says()]
        for i, later in enumerate(prompts[1:], 1):
            names = later.split(" already answered him.")[0].rsplit(". ", 1)[-1]
            assert set(names.replace(" and ", ", ").split(", ")) == set(said[:i])
            assert "Add something they didn't, or answer (nothing)." in later
    run(main())


def test_the_chat_window_starts_at_his_login(store, clock, tmp_path):
    """Prompts I2: twenty hours after his goodnight, the greetings never see last night's
    chat ("back already? thought you said gn")."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered())
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(60)
        await r.human("ok that's me done, gn all")
        await r.advance(60)
        assert any("gn all" in p for p in r.prompts())  # positive control: it was seen then
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.engine.end_session()
        await r.advance(20 * 3600, step=3600)
        seen = len(r.prompts())
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(60)
        greets = r.prompts()[seen:]
        assert greets and all("just logged on" in p for p in greets)
        assert not any("gn all" in p for p in greets) and "Chat:" not in greets[0]
    run(main())


def test_every_chat_line_in_a_prompt_carries_its_age(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        his_line(r, store, "anyone up for heroics?")
        await r.advance(300)
        his_line(r, store, "or a quick toc run")
        msgs, _ = await r.engine.compose(reply_to_human(101))
        chat = msgs[1]["content"].split("Chat:\n")[1].split("\n\n")[0].splitlines()
        assert chat == ["[5m ago] Zan: anyone up for heroics?",
                        "[just now] Zan: or a quick toc run"]
    run(main())


def test_idle_talk_sees_only_the_last_five_minutes(store, clock, tmp_path):
    """Rehearsal 2 (judge A 2): openers welded 16-minute-old lines onto fresh topics and said
    "Chat has gone quiet" under lines from just now. Idle kinds see the last five minutes;
    an opener says chat has gone quiet only when it has."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        his_line(r, store, "is the drake worth it")
        await r.advance(400)
        opener = Turn(kind=TurnKind.OPEN, speaker=101, channel="guild")
        msgs, _ = await r.engine.compose(opener)
        user = msgs[1]["content"]
        assert "drake" not in user and "Chat:" not in user
        assert "Chat has gone quiet. Bring up" in user
        his_line(r, store, "anyone up for heroics?")
        for kind in (TurnKind.OPEN, TurnKind.CONTINUE, TurnKind.ASIDE):
            msgs, _ = await r.engine.compose(Turn(kind=kind, speaker=101, channel="guild",
                                                  addressee=HUMAN))
            user = msgs[1]["content"]
            # an opener never sees his lines (fix wave G3), but chat has not gone quiet
            assert ("anyone up for heroics" in user) is (kind is not TurnKind.OPEN), kind
            assert "drake" not in user, kind
            assert "Chat has gone quiet" not in user, kind
        msgs, _ = await r.engine.compose(reply_to_human(101))  # an answer to him sees it all
        assert "drake" in msgs[1]["content"]
    run(main())


def member_line(r, store, guid: int, text: str):
    """A member's guild line as the app logs it."""
    row = store.chat_lines([store.insert_chat_line(Chat(
        ts=0, channel="guild", from_guid=guid, from_name=r.world.members[guid].name, to_guid=0,
        text=text, map=0, zone=10), session_id=None, origin="game")])[0]
    r.world.add_line(row, r.clock.now())
    return row


def people_here(messages: list[dict]) -> str:
    return messages[1]["content"].split("People here:\n")[1].split("\n\n")[0]


def test_the_prompt_card_is_the_live_persona_said_as_behaviour(store, clock, tmp_path):
    """Prompts minor 13: the stored card's schema words ("Attachment: secure.") reached the
    model, and a stored card changes only when its persona is saved again. The prompt renders
    the card from the live persona, with race and class from the cast."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        store.conn.execute("UPDATE persona_core SET card = 'STALE CARD' WHERE guid = 101")
        msgs, _ = await r.engine.compose(reply_to_human(101))
        system = msgs[0]["content"]
        assert "STALE CARD" not in system and "Attachment:" not in system
        assert "You trust people easily" in system
        who = f"You are {first_name(store, 101)}, 34, UK. You play Arkon, an Orc "
        assert system.splitlines()[0].startswith(who)
    run(main())


def test_a_whisper_prompt_names_only_the_one_she_whispers(store, clock, tmp_path):
    """Prompts minor 12: a whisper to him listed the last two guild speakers under "People
    here:"."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        member_line(r, store, 102, "anyone for toc tonight?")
        member_line(r, store, 103, "after dinner maybe")
        whisper, _ = await r.engine.compose(Turn(kind=TurnKind.WHISPER, speaker=101,
                                                 channel="whisper", to_guid=HUMAN,
                                                 addressee=HUMAN))
        assert people_here(whisper).startswith("Zan")
        assert "Belra" not in people_here(whisper) and "Coros" not in people_here(whisper)
        guild, _ = await r.engine.compose(reply_to_human(101))  # in guild chat, who spoke
        assert "Belra" in people_here(guild) and "Coros" in people_here(guild)
    run(main())


def test_prompt_lists_own_recent_lines(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path,
                     script=script_from(["the kettle at work broke again", "still no kettle"]))
        assert await spoken(r, reply_to_human(101))
        await r.advance(15)
        assert await spoken(r, reply_to_human(101))
        mine = r.prompts()[1].split("Your last lines")[1]
        assert "the kettle at work broke again" in mine
    run(main())


def test_chain_cap_stops_bot_bot_burst(store, clock, tmp_path):
    """Lines on one channel without the human, inside one conversation (no 90 s of quiet),
    stop at the rolled cap of 4-6."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        cast = (101, 102, 103, 104, 105)
        results = []
        for i in range(8):  # the human is silent; 20 s apart keeps the social ceiling clear
            turn = Turn(kind=TurnKind.CONTINUE, speaker=cast[i % 5], channel="guild",
                        addressee=cast[(i - 1) % 5])
            task = asyncio.create_task(r.engine.speak(turn))
            await r.advance(20)
            results.append(task.result())
        assert 4 <= sum(bool(x) for x in results) <= 6
        assert "chain" in [u["dropped"] for u in r.utterances()]
        # a human line starts a new chain
        await r.human("you lot still on about the kettle")
        await r.advance(40)
        quiet = [g for g in cast if g not in {s.guid for t, s in r.sent.timed_says()
                                              if t > clock.now() - 60}]
        assert await spoken(r, Turn(kind=TurnKind.CONTINUE, speaker=quiet[0], channel="guild",
                                    addressee=HUMAN), seconds=30)
    run(main())


CAST = (101, 102, 103, 104, 105)


async def cap_the_chain(r) -> float:
    """CONTINUE turns 20 s apart on guild until the chain stops one; the last line's time."""
    for i in range(8):
        task = asyncio.create_task(r.engine.speak(Turn(
            kind=TurnKind.CONTINUE, speaker=CAST[i % 5], channel="guild",
            addressee=CAST[(i - 1) % 5])))
        await r.advance(20)
        task.result()
        if r.utterances()[-1]["dropped"] == "chain":
            return max(t for t, _ in r.sent.timed_says())
    raise AssertionError("the chain never stopped a turn")


def quiet_members(r, now: float, *, but=()) -> list[int]:
    """Cast members who said nothing in the last minute (so the per-person guard is clear)."""
    loud = {s.guid for t, s in r.sent.timed_says() if now - t < 60}
    return [g for g in CAST if g not in loud and g not in but]


async def attempt(r, turn: Turn, seconds: float) -> list[str]:
    task = asyncio.create_task(r.engine.speak(turn))
    await r.advance(seconds, step=0.5)
    assert task.done()
    return task.result()


def test_the_guilds_altered_states_are_capped_a_night(store, clock, tmp_path):
    """Rehearsal 1 and 2: eleven members wired by the end of the evening. At the session's
    roll and on every 10-minute check, at most a share of the guild is in any one altered
    state."""
    from leeroy_mind.talk.states import Altered, state_cap

    async def main():
        seed_test_cast(store)
        coffee = [{"substance": "caffeine", "when": "daily", "odds": 1.0, "max_state": "wired"}]
        for g in range(201, 261):  # sixty coffee drinkers
            save_persona(store, make_persona(g, f"Mem{g}", habits=coffee), source="seed",
                         ts=1000.0, race="Orc", class_name="Warrior")
        r = make_rig(store, clock, tmp_path, chattiness=0)
        await r.session()
        cap = state_cap(len(r.engine.personas.all()))

        def wired():
            return sum(r.engine._altered_of(g) is Altered.WIRED
                       for g in r.engine.personas.all())
        assert 0 < wired() <= cap
        for _ in range(36):  # six hours of 10-minute checks
            await r.advance(600, step=600)
            await r.engine.tick(clock.now())
            assert wired() <= cap
    run(main())


def test_the_chain_starts_over_after_90_s_of_quiet(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        last = await cap_the_chain(r)
        await r.advance(last + 89 - clock.now(), step=0.5)
        turn = Turn(kind=TurnKind.CONTINUE, speaker=101, channel="guild", addressee=102)
        assert await attempt(r, turn, 0.5) == []
        assert r.utterances()[-1]["dropped"] == "chain"
        await r.advance(1.5, step=0.5)  # 91 s of quiet: a new conversation
        assert await attempt(r, turn, 30)
    run(main())


def test_flirts_and_asides_are_capped_like_any_social_turn(store, clock, tmp_path):
    """Controller ruling: the cap covers every social kind, FLIRT and ASIDE included."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="drawn", style="bold")
        r = make_rig(store, clock, tmp_path,
                     script=numbered("you're trouble, zan, take {n}"))  # flirts of his
        await cap_the_chain(r)
        now = clock.now()
        flirt = Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN)
        assert await attempt(r, flirt, 1) == [] and r.utterances()[-1]["dropped"] == "chain"
        man = quiet_members(r, now, but=(102,))[0]
        aside = Turn(kind=TurnKind.ASIDE, speaker=man, channel="guild",
                     instruction="Tease Belra about it.")
        assert await attempt(r, aside, 1) == [] and r.utterances()[-1]["dropped"] == "chain"
        await r.advance(100)  # a quiet channel: a new conversation, and she may flirt again
        assert await attempt(r, flirt, 30)
    run(main())


def test_the_chain_counts_answers_and_reactions_but_never_stops_them(store, clock, tmp_path):
    """Controller ruling: the cap stops the members' own social turns only."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        await cap_the_chain(r)
        await r.advance(15)
        first, second = quiet_members(r, clock.now())[:2]
        answer = Turn(kind=TurnKind.REPLY_HUMAN, speaker=first, channel="guild", addressee=HUMAN)
        assert await attempt(r, answer, 15)  # an answer to him, on a capped chain
        gz = asyncio.create_task(r.engine.say_as(second, "guild", "gz", kind=TurnKind.GZ))
        await r.advance(10)
        assert gz.result()  # a reaction to the game, on the same capped chain
        await r.advance(100)  # quiet: the next line starts a new chain
        for i in range(6):  # six answers count toward it, 12 s apart
            assert await attempt(r, Turn(kind=TurnKind.REPLY_HUMAN, speaker=CAST[i % 5],
                                         channel="guild", addressee=HUMAN), 12)
        await r.advance(60)  # the guild window clears, still inside the conversation
        assert await attempt(r, Turn(kind=TurnKind.CONTINUE, speaker=102, channel="guild",
                                     addressee=101), 15) == []
        assert r.utterances()[-1]["dropped"] == "chain"
    run(main())


def test_guild_wide_six_per_minute(store, clock, tmp_path):
    """The limit counts turns (ruling 4); every reply here is one line, so Says are turns."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        for i in range(60):
            await r.human(f"anyone up for heroics {i}?")
            await r.advance(2)
        await r.advance(40)
        times = [t for t, _ in r.sent.timed_says()]
        assert len(times) >= 12
        assert all(sum(1 for u in times if t <= u < t + 60) <= 6 for t in times)
    run(main())


def test_emote_lines_trigger_nothing(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        await r.human("waves at everyone", channel="emote")
        await r.event(TextEmote(ts=0, guid=HUMAN, emote="hug", target_guid=101))
        await r.event(TextEmote(ts=0, guid=101, emote="wave", target_guid=HUMAN))
        await r.event(Chat(ts=0, channel="emote", from_guid=102, from_name="Belra", to_guid=0,
                           text="dances", map=0, zone=10))
        await r.advance(30)
        assert r.sent.says() == [] and r.chat_calls() == []
        await r.human("anyone up for a run?")  # positive control
        await r.advance(40)
        assert r.sent.says()
    run(main())


def test_echo_of_own_line_is_ignored(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, reply="x marks the spot")
        assert await spoken(r, reply_to_human(101))
        said = r.sent.says()[0]
        rows, seen, calls = chat_rows(store), len(r.world.history), len(r.chat_calls())
        echo = Chat(ts=0, channel="guild", from_guid=101, from_name="Arkon", to_guid=0,
                    text=said.text, map=0, zone=10, via_bridge=True)
        assert r.engine.is_echo(echo, clock.now())
        # recorded on its ack already: confirming it records nothing more
        assert chat_rows(store) == rows and len(r.world.history) == seen
        await r.engine.on_event(echo, clock.now())  # and nobody answers it
        await r.advance(30)
        assert len(r.sent.says()) == 1 and len(r.chat_calls()) == calls
        game = Chat(ts=0, channel="guild", from_guid=101, from_name="Arkon", to_guid=0,
                    text=said.text, map=0, zone=10, via_bridge=False)
        assert not r.engine.is_echo(game, clock.now())  # a line the mind didn't send
    run(main())


def test_offline_ack_marks_member_offline(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path,
                     ack=lambda cmd: (False, "offline") if cmd.guid == 101 else (True, ""))
        assert await spoken(r, reply_to_human(101)) == []
        assert 101 not in r.world.online
        assert r.utterances()[-1]["dropped"] == "offline"
    run(main())


def test_a_member_marked_offline_by_an_ack_answers_him_again_once_back(store, clock, tmp_path):
    """Liveops M4, reproduced: after one "offline" ack her zone, death and resurrect events did
    not bring her back, and his next whisper to her got no reply and no row. Any event of hers
    does now, and the tick lets the mark run out."""
    away = [True]

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered(),
                     ack=lambda cmd: (False, "offline") if away[0] else (True, ""))
        assert await spoken(r, reply_to_human(101)) == [] and 101 not in r.world.online
        away[0] = False
        await r.event(Zone(ts=0, guid=101, zone_id=12, zone_name="Dalaran"))
        await r.human("you there?", channel="whisper", to_guid=101)
        await r.advance(40)
        assert [(u["channel"], u["dropped"]) for u in r.utterances() if u["guid"] == 101] == [
            ("guild", "offline"), ("whisper", None)]
        away[0] = True
        assert await spoken(r, reply_to_human(102)) == [] and 102 not in r.world.online
        away[0] = False
        for _ in range(70):  # no event of hers: the tick ends the mark
            await r.advance(1)
            await r.engine.tick(clock.now())
        assert 102 in r.world.online
    run(main())


@pytest.mark.parametrize("error, reason", [
    (NotConnected("down"), "not_connected"), (ConnectionLost("gone"), "connection_lost"),
    (AckTimeout("slow"), "ack_timeout")])
def test_bridge_errors_drop_the_turn(store, clock, tmp_path, error, reason):
    def ack(cmd):
        raise error

    async def main():
        r = make_rig(store, clock, tmp_path, ack=ack)
        assert await spoken(r, reply_to_human(101)) == []
        await r.engine.tick(clock.now())  # an ack timeout's row waits out the echo window
        assert r.utterances()[-1]["dropped"] == reason
        assert len(r.utterances()) == 1
    run(main())


@pytest.mark.parametrize("error", [AckTimeout("slow"), ConnectionLost("bridge connection lost")])
def test_echo_confirms_a_line_whose_ack_timed_out(store, clock, tmp_path, error):
    """Correctness M4: a line whose connection dropped while its ack was due may still be said
    (the bridge keeps events across a reconnect), so it waits for its echo like a timed-out
    one; it was recorded as dropped and its echo found nothing."""
    def ack(cmd):
        raise error

    async def main():
        r = make_rig(store, clock, tmp_path, ack=ack, reply="on my way")
        assert await spoken(r, reply_to_human(101), seconds=15) == []
        rows = chat_rows(store)
        text = r.sent.says()[0].text
        echo = Chat(ts=0, channel="guild", from_guid=101, from_name="Arkon", to_guid=0,
                    text=text, map=0, zone=10, via_bridge=True)
        assert r.engine.is_echo(echo, clock.now())
        assert chat_rows(store) == rows + 1 and r.world.history[-1].row.text == text
        assert r.world.history[-1].row.origin == "mind"
        await r.advance(40)
        await r.engine.tick(clock.now())
        assert [u["dropped"] for u in r.utterances()] == [None]  # one row: it was said
    run(main())


def test_a_failed_echo_record_never_reaches_the_dispatcher(store, clock, tmp_path, monkeypatch,
                                                           caplog):
    def ack(cmd):
        raise AckTimeout("slow")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    async def main():
        r = make_rig(store, clock, tmp_path, ack=ack, reply="on my way")
        assert await spoken(r, reply_to_human(101), seconds=15) == []
        echo = Chat(ts=0, channel="guild", from_guid=101, from_name="Arkon", to_guid=0,
                    text=r.sent.says()[0].text, map=0, zone=10, via_bridge=True)
        monkeypatch.setattr(store, "insert_chat_line", broken)
        with caplog.at_level(logging.ERROR, logger="leeroy_mind"):
            assert r.engine.is_echo(echo, clock.now())  # logged, not raised
        assert "say_record_failed" in caplog.text
    run(main())


def test_a_failed_drop_row_never_reaches_the_dispatcher_or_the_tick(store, clock, tmp_path,
                                                                    monkeypatch, caplog):
    """Task 16 re-review: the row for a line whose echo never came is written from is_echo
    (the app's dispatcher) and from tick, and a failing write raised into both."""
    def ack(cmd):
        raise AckTimeout("slow")

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    async def main():
        r = make_rig(store, clock, tmp_path, ack=ack, script=numbered())
        assert await spoken(r, reply_to_human(101), seconds=15) == []
        assert await spoken(r, reply_to_human(102), seconds=15) == []
        monkeypatch.setattr(store, "log_utterance", broken)
        await r.advance(20)
        other = Chat(ts=0, channel="guild", from_guid=103, from_name="Coros", to_guid=0,
                     text="unrelated", map=0, zone=10, via_bridge=True)
        with caplog.at_level(logging.ERROR, logger="leeroy_mind"):
            assert r.engine.is_echo(other, clock.now())  # each line is settled here
            await r.advance(20)
            await r.engine.tick(clock.now())  # or here, once its echo window is over
        assert caplog.text.count("say_drop_failed") == 2
        assert r.engine._unacked == {}
    run(main())


def test_expired_social_turn_is_dropped(store, clock, tmp_path):
    async def main():
        gate = asyncio.Event()
        r = make_rig(store, clock, tmp_path, gate=gate, reply="late to the party here")
        task = asyncio.create_task(r.engine.speak(
            Turn(kind=TurnKind.OPEN, speaker=101, channel="guild")))
        await r.advance(25)  # past the 20 s deadline while the model is held
        assert not task.done()
        gate.set()
        await r.advance(0.5, step=0.5)  # dropped as soon as the late words arrive, unstyled
        assert task.done() and task.result() == []
        await r.advance(30)
        assert r.sent.says() == [] and r.utterances()[-1]["dropped"] == "expired"
    run(main())


@pytest.mark.parametrize("held, said", [(35, False), (25, True)])
def test_a_reply_to_him_has_thirty_seconds(store, clock, tmp_path, held, said):
    """Tests lens M2: only an OPEN turn's deadline was tested, so a reply deadline of 3000 s
    passed: a reply held behind a slow model would go out minutes late."""
    async def main():
        gate = asyncio.Event()
        r = make_rig(store, clock, tmp_path, gate=gate, reply="yeah on my way")
        task = asyncio.create_task(r.engine.speak(reply_to_human(101)))
        await r.advance(held)
        gate.set()
        await r.advance(40)
        assert bool(task.result()) is said
        assert r.utterances()[-1]["dropped"] == (None if said else "expired")
    run(main())


SLOW_LINE = ("i still use the same bread recipe my nan wrote out for me, the one with the honey "
             "and the oats and the seeds in it")


@pytest.mark.parametrize("cps, high, chars, reply, held", [
    (4.5, False, 106, False, 0.5),  # the rehearsal's baker: ~26 s of typing, 20 s deadline
    (5.5, True, 53, False, 0.5),    # and twice a high member at 5.5 a second, ~20 s each
    (5.5, True, 64, False, 0.5),
    (4.5, True, 70, True, 3.0),     # a high member's 70-character answer to him, 30 s
])
def test_the_first_lines_typing_never_drops_a_turn(store, clock, tmp_path, cps, high, chars,
                                                   reply, held):
    """Rehearsal 2, finding 5: three social lines the model gave in half a second were lost
    to typing time alone. The deadline governs the model's answer; the first line is typed
    faster to go out within it, and never dropped."""
    from leeroy_mind.talk.states import Altered

    async def main():
        seed_test_cast(store)
        save_member(store, 102, chars_per_sec=cps, typo_rate=0.0, emoticon_rate=0.0,
                    split_rate=0.0, abbreviations={}, words_per_line=[3, 30])
        cut = SLOW_LINE.find(" ", chars)
        line = SLOW_LINE[:cut] if cut > 0 else SLOW_LINE  # at least `chars` characters
        assert len(line) >= chars
        gate = asyncio.Event()
        r = make_rig(store, clock, tmp_path, reply=line, gate=gate)
        if high:
            r.engine._altered[102] = Altered.HIGH
        turn = (reply_to_human(102) if reply
                else Turn(kind=TurnKind.OPEN, speaker=102, channel="guild"))
        t0 = clock.now()
        task = asyncio.create_task(r.engine.speak(turn))
        await r.advance(held, step=held)
        gate.set()
        await r.advance(45)
        assert task.result() == [line], r.utterances()[-1]["dropped"]
        (at, _), = r.sent.timed_says()
        # the turn (and its deadline) starts at the clock's first step, `held` in
        assert at - t0 <= held + (30.0 if reply else 20.0)
    run(main())


def test_the_deadline_starts_after_the_turns_delay(store, clock, tmp_path):
    """Tests lens M2: a turn is given its deadline once its delay is over; counted from before
    it, a turn delayed 25 s would expire before it started."""
    async def main():
        r = make_rig(store, clock, tmp_path, reply="quiet night, anyone up for a heroic")
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=101, channel="guild",
                                    delay=25), seconds=60)
    run(main())


FEASTS = ("brought three stacks of fish feasts again tonight",
          "nobody ever eats them but i keep on hoping",
          "maybe tonight is the night someone finally does")


def feast_bit(lines: int) -> Bit:
    return Bit(id="feasts", instruction="bring up the fish feasts nobody eats",
               min_days_between=3.0, voice_bit=False, contexts=("any",), lines=lines, dates=())


@pytest.mark.parametrize("lines", [2, 3])
def test_a_slow_typists_bit_sends_every_line(store, clock, tmp_path, lines):
    """Correctness I1 and its ruling: the deadline guards how stale the trigger is. It is
    checked after the model answers and before the first line, not before lines 2+, which
    the turn's own typing (4.5 characters a second here) would otherwise push past it."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, chars_per_sec=4.5, typo_rate=0.0, emoticon_rate=0.0)
        r = make_rig(store, clock, tmp_path, reply="\n".join(FEASTS[:lines]))
        turn = Turn(kind=TurnKind.BIT, speaker=102, channel="guild", bit=feast_bit(lines),
                    max_lines=lines)
        assert await spoken(r, turn, seconds=60) == list(FEASTS[:lines])
        times = [t for t, _ in r.sent.timed_says()]
        assert times[-1] - times[0] > 10  # line 2 went well after a 20 s deadline's start
        assert store.last_bit_use(102, "feasts") is not None
    run(main())


def test_a_bit_counts_as_used_once_its_first_line_is_sent(store, clock, tmp_path):
    """A turn cancelled between its lines (a pull, his logout) still used the bit: its first
    line is in chat, so it must not fire again tonight."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, chars_per_sec=4.5, typo_rate=0.0, emoticon_rate=0.0)
        r = make_rig(store, clock, tmp_path, reply="\n".join(FEASTS[:2]))
        task = asyncio.create_task(r.engine.speak(Turn(
            kind=TurnKind.BIT, speaker=102, channel="guild", bit=feast_bit(2), max_lines=2)))
        for _ in range(40):
            await r.advance(0.5, step=0.5)
            if r.sent.says():
                break
        assert [s.text for s in r.sent.says()] == [FEASTS[0]] and not task.done()
        task.cancel()
        await r.advance(5)
        assert task.cancelled() and len(r.sent.says()) == 1
        assert store.last_bit_use(102, "feasts") is not None
        # positive control: a bit whose first line never went out is not used
        quiet = make_rig(store, clock, tmp_path, reply="(nothing)")
        other = Bit(id="quiet", instruction="say nothing much", min_days_between=3.0,
                    voice_bit=False, contexts=("any",), lines=1, dates=())
        await r.advance(60)
        assert await spoken(quiet, Turn(kind=TurnKind.BIT, speaker=103, channel="guild",
                                        bit=other, max_lines=1)) == []
        assert store.last_bit_use(103, "quiet") is None
    run(main())


def test_the_log_says_why_a_line_got_no_answer_and_how_long_the_model_took(store, clock,
                                                                          tmp_path, caplog):
    """Liveops M5: a line nobody could answer left no trace, and say_sent and say_dropped
    named neither the line they answered nor the model's time."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered(), online=())
        with caplog.at_level(logging.INFO, logger="leeroy_mind"):
            row = await r.human("anyone around?")
            await r.advance(5)
        assert f"reply_skipped: line={row.id} reason=no_candidates" in caplog.text
        caplog.clear()
        answers = iter(["sure, one sec"])
        r = make_rig(store, clock, tmp_path, script=lambda model, messages: next(answers,
                                                                              "(nothing)"))
        with caplog.at_level(logging.INFO, logger="leeroy_mind"):
            row = await r.human("arkon you around?")
            await r.advance(40)
            assert await spoken(r, Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild",
                                        addressee=HUMAN, trigger_line=row.id)) == []
        sent = next(x for x in caplog.messages if x.startswith("say_sent:"))
        dropped = next(x for x in caplog.messages if x.startswith("say_dropped:"))
        for msg in (sent, dropped):
            assert f"trigger={row.id}" in msg and "llm_s=0.0" in msg
    run(main())


def test_live_calls_use_the_live_preset(store, clock, tmp_path):
    """Tests lens I3: every live call turns thinking off (the live model answers nothing
    otherwise: the rig's FakeLLM does the same) and sends LIVE's options (another num_ctx
    makes Ollama reload the model) to the configured live model, also after a reload."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        assert (await r.llm.chat(r.cfg.models.live, [], options={})).text == ""  # like the model
        assert await spoken(r, reply_to_human(101))
        live = json.loads(json.dumps(dict(LIVE.options)))
        calls = r.chat_calls()[1:]
        assert calls and all(c["think"] is False and c["options"] == live
                             and c["model"] == r.cfg.models.live for c in calls)
        r.engine.apply_config(replace(r.cfg, models=replace(r.cfg.models, live="next-live:1b")))
        await r.advance(15)
        assert await spoken(r, reply_to_human(102))
        assert r.chat_calls()[-1]["model"] == "next-live:1b"
        assert r.chat_calls()[-1]["think"] is False
    run(main())


def test_end_session_keeps_a_line_already_with_the_bridge(store, clock, tmp_path):
    gate, sent = asyncio.Event(), []

    async def slow_send(cmd):
        sent.append(cmd)
        await gate.wait()
        return Ack(ts=0, id=cmd.id, ok=True, error="")

    async def main():
        r = make_rig(store, clock, tmp_path, reply="on my way", send=slow_send)
        await r.session()
        await r.human("arkon you around?")
        for _ in range(30):
            await r.advance(1)
            if sent:
                break
        assert sent and r.engine.pending_turns()
        rows = chat_rows(store)
        ending = asyncio.create_task(r.engine.end_session())
        await asyncio.sleep(0)
        assert not ending.done()
        gate.set()
        await ending
        assert chat_rows(store) == rows + 1  # recorded although its turn was cancelled
        assert r.engine.pending_turns() == [] and len(sent) == 1
    run(main())


def test_apply_config_takes_effect(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, chattiness=10)
        asides = r.engine.asides
        r.engine.apply_config(replace(r.cfg, talk=TalkCfg(0, r.cfg.talk.raid_rule),
                                      human=HumanCfg("Zed", REAL, "she")))
        assert r.engine.asides is asides  # renamed in place: its hourly count carries on
        msgs, _ = await r.engine.compose(reply_to_human(101))
        text = "\n".join(m["content"] for m in msgs)
        assert "Answer Zed's last line" in text and "what she actually said" in text
        for _ in range(300):  # 20 minutes: chattiness 0 opens nothing
            await r.advance(4)
            await r.engine.tick(clock.now())
        assert r.sent.says() == []
    run(main())


def test_apply_config_moves_the_memory_depth(store, clock, tmp_path):
    """Spec minor 3: the owner's memory depth knob took a restart."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        assert r.engine.recall.tau_days == r.cfg.memory.depth_days
        r.engine.apply_config(replace(r.cfg, memory=replace(r.cfg.memory, depth_days=12.5)))
        assert r.engine.recall.tau_days == 12.5
    run(main())


def test_a_dead_member_never_speaks_on_say(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        await r.event(Group(ts=0, leader_guid=HUMAN, raid=False, members=(HUMAN, 101, 102)))
        await r.event(Death(ts=0, guid=101, map=0, killer_entry=0))
        await r.human("arkon you alive?", channel="say")
        await r.advance(40)
        assert all(s.guid != 101 for s in r.sent.says())
        await r.event(Resurrect(ts=0, guid=101))
        await r.advance(15)
        await r.human("arkon you good now?", channel="say")  # positive control
        await r.advance(40)
        assert any(s.guid == 101 and s.channel == "say" for s in r.sent.says())
        said = len(r.sent.says())
        await r.advance(60)
        await r.human("arkon watch the adds?", channel="say")  # planned while he is alive
        await r.event(Death(ts=0, guid=101, map=0, killer_entry=0))  # dies before he answers
        await r.advance(40)
        assert all(s.guid != 101 for s in r.sent.says()[said:])
        assert any(u["guid"] == 101 and u["dropped"] == "dead" for u in r.utterances())
    run(main())


# -- the human's real name ---------------------------------------------------------------------

def test_non_close_output_never_carries_the_real_name(store, clock, tmp_path):
    """The lowercase output path: Arkon is not close and types in lower case, so the real name
    must be redacted from the model's text before styling lowercases it (the redaction only
    knows a capitalised name)."""
    async def main():
        seed_test_cast(store)
        store.put_relationship(Relationship(a=101, b=HUMAN, affinity=10, familiarity=20))
        store.put_relationship(Relationship(a=102, b=HUMAN, affinity=60, familiarity=85))
        r = make_rig(store, clock, tmp_path, reply=f"{REAL} you legend, nice one")
        far = " ".join(await spoken(r, reply_to_human(101))).casefold()
        assert far and REAL.casefold() not in far and NICK.casefold() in far
        stored = store.conn.execute("SELECT text FROM chat_line WHERE from_guid = 101").fetchall()
        assert stored and all(REAL.casefold() not in x["text"].casefold() for x in stored)
        close = " ".join(await spoken(r, reply_to_human(102))).casefold()
        assert REAL.casefold() in close  # a close member may use it
    run(main())


def test_only_the_inner_circle_uses_his_real_name(store, clock, tmp_path):
    """Final fix wave ruling: a close friend (familiarity 60, under INNER_FAMILIARITY) is never
    told his real name and her output is redacted like anyone's; an inner-circle member
    (familiarity 85) is told it now and then, one member at a time guild-wide."""
    async def main():
        seed_test_cast(store)
        store.put_relationship(Relationship(a=101, b=HUMAN, affinity=60, trust=40,
                                            familiarity=60, interactions=30))
        store.put_relationship(Relationship(a=102, b=HUMAN, affinity=60, trust=40,
                                            familiarity=85, interactions=30))
        r = make_rig(store, clock, tmp_path, seed=3,
                     script=numbered(REAL + " you legend, round {n}"))
        offered = {101: [], 102: []}
        for i in range(80):
            g = (101, 102)[i % 2]
            said = " ".join(await spoken(r, reply_to_human(g), seconds=15, step=5))
            if f"real name, {REAL}" in r.prompts()[-1]:
                offered[g].append(clock.now())
            if g == 101:
                assert said and REAL.casefold() not in said.casefold()
        assert offered[101] == [] and len(offered[102]) >= 2
        assert all(b - a >= 600 for a, b in zip(offered[102], offered[102][1:]))
        friend = text_of((await r.engine.compose(reply_to_human(101)))[0])
        assert REAL not in friend  # her prompt is redacted like any non-close member's
    run(main())


def test_real_name_line_goes_to_one_close_member_at_a_time(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        for g in (101, 102):
            store.put_relationship(Relationship(a=g, b=HUMAN, affinity=60, familiarity=85))
        r = make_rig(store, clock, tmp_path, seed=2)
        offered = []
        for i in range(80):
            await spoken(r, reply_to_human((101, 102)[i % 2]), seconds=15, step=5)
            if f"real name, {REAL}" in r.prompts()[-1]:
                offered.append(clock.now())
        assert len(offered) >= 2
        assert all(b - a >= 600 for a, b in zip(offered, offered[1:]))
    run(main())


def test_his_real_name_reaches_an_inner_prompt_only_on_the_turn_offered_it(store, clock,
                                                                          tmp_path):
    """Privacy minor 10: the guild-wide gap paced only the offer. Once one inner-circle member
    (or he) said the name, every inner member's next chat window showed it, and a model
    mirrors names. It reaches an inner member's prompt only on the turn offered it."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, abbreviations={}, typo_rate=0.0, emoticon_rate=0.0)
        for g in (102, 103):
            store.put_relationship(Relationship(a=g, b=HUMAN, affinity=60, trust=40,
                                                familiarity=85, interactions=30))
        r = make_rig(store, clock, tmp_path, script=numbered(REAL + " you legend, round {n}"))
        for _ in range(60):  # until one of her turns is offered the name, and she says it
            assert await spoken(r, reply_to_human(102), seconds=25, step=5)
            if f"real name, {REAL}" in r.prompts()[-1]:
                break
        else:
            raise AssertionError("never offered")
        his_line(r, store, f"haha yes {REAL} is my real name")
        for g in (103, 102):  # the gap is running: neither is offered it now
            prompt = text_of((await r.engine.compose(reply_to_human(g)))[0])
            assert REAL not in prompt and "Zan you legend" in prompt
            assert "haha yes Zan is my real name" in prompt
        await r.advance(600, step=60)
        for _ in range(60):
            assert await spoken(r, reply_to_human(103), seconds=25, step=5)
            if f"real name, {REAL}" in r.prompts()[-1]:
                break
        else:
            raise AssertionError("never offered")
        assert f"Belra: {REAL} you legend" in r.prompts()[-1]  # on the turn offered it
    run(main())


def test_a_close_members_use_of_his_real_name_never_reaches_the_rest(store, clock, tmp_path):
    """Belra is close and types in lower case; Arkon is not close. Her line keeps the name
    capitalised in the log, so his prompt and his output never carry its Title or UPPER form."""
    def script(model, messages):
        if "You play Belra" in messages[0]["content"]:
            return f"{REAL} you legend"
        return f"lol {REAL} and {REAL.upper()} for real"  # what Arkon would echo if he saw it

    async def main():
        seed_test_cast(store)
        store.put_relationship(Relationship(a=102, b=HUMAN, affinity=60, familiarity=85))
        store.put_relationship(Relationship(a=101, b=HUMAN, affinity=10, familiarity=20))
        r = make_rig(store, clock, tmp_path, script=script)
        assert await spoken(r, reply_to_human(102))
        (row,) = store.conn.execute("SELECT text FROM chat_line WHERE from_guid = 102")
        assert REAL in row["text"] and REAL in r.world.history[-1].row.text  # not lowered
        out = await spoken(r, Turn(kind=TurnKind.CONTINUE, speaker=101, channel="guild",
                                   addressee=102))
        prompt = r.prompts()[-1]
        assert "You play Arkon" in prompt and "Belra: " in prompt  # he did see her line
        assert REAL not in prompt and REAL.upper() not in prompt
        said = " ".join(out)  # he types in lower case: look for the name in any case
        assert out and REAL.casefold() not in said.casefold()
    run(main())


@pytest.mark.parametrize("name, spelt", [("o'neil", "O'Neil"), ("mary jane", "Mary Jane"),
                                         ("Bill", "Bill")])
def test_his_name_is_kept_in_a_form_the_redaction_catches(store, clock, tmp_path, name, spelt):
    """Task 16 re-review: a configured "o'neil" or "mary jane" was kept as "O'neil" and
    "Mary jane", forms the redaction (as configured, Title, UPPER) does not catch; and an
    inner member's ordinary lower-case word was turned into the name. On the turn offered
    his name it is kept in its Title form however the model typed it; her other words stay."""
    def script(model, messages):
        if "real name" in messages[1]["content"]:
            return f"hey {name.lower()} you legend, who pays the bill"
        return "who pays the bill tonight"

    async def main():
        seed_test_cast(store)
        save_member(store, 102, typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0)
        store.put_relationship(Relationship(a=102, b=HUMAN, affinity=60, trust=40,
                                            familiarity=85, interactions=30))
        r = make_rig(store, clock, tmp_path, script=script, real_name=name)
        for _ in range(60):
            assert await spoken(r, reply_to_human(102), seconds=25, step=5)
            if "real name" in r.prompts()[-1]:
                break
        else:
            raise AssertionError("never offered")
        said = [x["text"] for x in store.conn.execute(
            "SELECT text FROM chat_line WHERE from_guid = 102 ORDER BY id")]
        assert said[-1].startswith(f"hey {spelt} u legend")
        assert all(x.startswith("who pays the bill") for x in said[:-1])  # never the name
        prompt = text_of((await r.engine.compose(reply_to_human(101)))[0])
        assert f"hey {NICK} u legend" in prompt and name.casefold() not in prompt.casefold()
    run(main())


def test_the_ordinary_word_a_real_name_also_is_survives(store, clock, tmp_path):
    """The name is "Bill". Lower case is the word: it reaches a non-close member's prompt as
    the human typed it, and his own "bill" is never turned into the name."""
    def script(model, messages):
        if "You play Belra" in messages[0]["content"]:
            return "Bill you legend"
        return "who is paying the bill tonight"

    async def main():
        seed_test_cast(store)
        store.put_relationship(Relationship(a=102, b=HUMAN, affinity=60, familiarity=85))
        r = make_rig(store, clock, tmp_path, script=script, real_name="Bill")
        assert await spoken(r, reply_to_human(102))
        heard = Chat(ts=0, channel="guild", from_guid=HUMAN, from_name="Testmage", to_guid=0,
                     text="got the repair bill again", map=0, zone=10)  # logged, not answered
        r.world.add_line(store.chat_lines([store.insert_chat_line(
            heard, session_id=None, origin="human")])[0], clock.now())
        out = await spoken(r, Turn(kind=TurnKind.CONTINUE, speaker=101, channel="guild",
                                   addressee=102))
        prompt = r.prompts()[-1]
        assert "You play Arkon" in prompt and "got the repair bill again" in prompt
        assert "Bill" not in prompt and "BILL" not in prompt
        assert "the bill tonight" in " ".join(out)
    run(main())


# -- his whispers ------------------------------------------------------------------------------

QUITTING = f"{NICK} told me he is quitting his job on Friday."


def is_extraction(messages: list[dict]) -> bool:
    return "matching this schema" in messages[-1]["content"]


def arkon_remembers(text: str, cited: list[int]) -> str:
    """The extraction's reply: one memory for Arkon resting on the lines `cited`."""
    return json.dumps({"memories": [{
        "speaker": "Arkon", "text": text, "importance": 6, "valence": 0.2, "about": [NICK],
        "evidence": [f"L{i}" for i in cited]}], "overheard": None})


def extractor_for(r, store, clock) -> Extractor:
    """The app's extractor over the rig's store and FakeLLM, with no bystanders."""
    writer = MemoryWriter(store, Embedder(r.engine.scheduler, r.cfg.models.embedding), clock,
                          human=lambda: r.engine.human)
    return Extractor(store, r.engine.scheduler, writer, r.cfg.models.live,
                     human_guid=lambda: HUMAN, bystanders=lambda t, s: [],
                     human=lambda: r.engine.human)


def whisper_to(speaker: int, to: int) -> Turn:
    return Turn(kind=TurnKind.WHISPER, speaker=speaker, channel="whisper", to_guid=to,
                addressee=to)


def text_of(messages: list[dict]) -> str:
    """Every message of a composed prompt as one text."""
    return "\n".join(m["content"] for m in messages)


def test_what_he_whispers_her_stays_out_of_her_guild_prompt(store, clock, tmp_path):
    """The interim whisper rule (until P1c's private scope). He whispers Arkon, the thread is
    extracted into a memory citing only those whispers, and Arkon's next guild prompt carries
    none of it while his next whisper to him can; compose() keeps the same rule. The lines are
    logged as the app logs them, outside this engine's chat window (a later evening), so only
    recall could bring them back."""
    cited: list[int] = []
    talk = numbered()

    def script(model, messages):
        if is_extraction(messages):
            return arkon_remembers(QUITTING, cited)
        return talk(model, messages)

    async def main():
        r = make_rig(store, clock, tmp_path, script=script)
        threads = ThreadTracker(store)
        his = Chat(ts=0, channel="whisper", from_guid=HUMAN, from_name="Testmage", to_guid=101,
                   text="between us, im quitting my job on friday", map=0, zone=10)
        cited.append(store.insert_chat_line(his, session_id=None, origin="human",
                                            audience=(101,)))
        threads.add(cited[-1], his, clock.now())
        hers = Chat(ts=0, channel="whisper", from_guid=101, from_name="Arkon", to_guid=HUMAN,
                    text="omg ok, safe with me", map=0, zone=10)
        cited.append(store.insert_chat_line(hers, session_id=None, origin="mind",
                                            audience=(101,)))
        threads.on_line_sent(cited[-1], hers)
        (thread,) = threads.flush_all()
        assert (await extractor_for(r, store, clock).extract(thread)).written == 1
        (kept,) = store.memories_with_actors(101, [HUMAN])
        assert kept.evidence == tuple(f"chat:{i}" for i in cited)

        guild, whisper = reply_to_human(101), whisper_to(101, HUMAN)
        assert await spoken(r, guild)
        assert "You play Arkon" in r.prompts()[-1] and "quitting" not in r.prompts()[-1]
        await r.advance(60)  # his own interval before another unprompted turn
        assert await spoken(r, whisper)
        assert "quitting his job on Friday" in r.prompts()[-1]
        assert (await r.engine.compose(guild))[1] == []
        assert (await r.engine.compose(whisper))[1] == [kept.id]
    run(main())


def test_his_whisper_and_her_reply_never_reach_her_guild_prompt(store, clock, tmp_path):
    """In one session, as the app runs it: he whispers Arkon, Arkon whispers back and the
    thread is extracted. Her next guild prompt carries none of it by any route (the chat
    window, her own last lines, recall); her next whisper to him carries all of it."""
    cited: list[int] = []
    talk = numbered("sounds good, line {n}")
    replied: list[str] = []

    def script(model, messages):
        if is_extraction(messages):
            return arkon_remembers(QUITTING, cited)
        if not replied:  # her answer to his whisper is the first thing she says
            replied.append("mine too, keep it quiet")
            return replied[0]
        return talk(model, messages)

    async def main():
        seed_test_cast(store)
        save_member(store, 101, typo_rate=0.0, split_rate=0.0)  # her words arrive as written
        r = make_rig(store, clock, tmp_path, script=script)
        threads = ThreadTracker(store)
        r.engine.on_line_sent = threads.on_line_sent  # how the app threads the mind's lines
        his = await r.human("between us, im quitting my job on friday", channel="whisper",
                            to_guid=101)
        threads.add(his.id, Chat(ts=0, channel="whisper", from_guid=HUMAN, from_name="Testmage",
                                 to_guid=101, text=his.text, map=0, zone=his.zone), his.ts)
        await r.advance(40)
        says = r.sent.says()
        assert [s.channel for s in says] == ["whisper"] and "keep it quiet" in says[0].text
        (thread,) = threads.flush_all()
        cited.extend(thread.line_ids)
        assert (await extractor_for(r, store, clock).extract(thread)).written == 1

        assert await spoken(r, reply_to_human(101))
        guild = text_of(r.chat_calls()[-1]["messages"])
        assert "You play Arkon" in guild
        assert "quitting" not in guild and "keep it quiet" not in guild
        await r.advance(60)  # her own interval before another unprompted turn
        assert await spoken(r, whisper_to(101, HUMAN))
        private = text_of(r.chat_calls()[-1]["messages"])
        assert f"(whisper to Arkon): {his.text}" in private  # his line
        assert "(whisper to Zan): mine too, keep it quiet" in private  # hers
        assert "Your last lines (don't reuse their wording):\nmine too, keep it quiet" in private
        assert QUITTING in private
    run(main())


def test_a_whisper_between_two_members_reaches_no_one_elses_turn(store, clock, tmp_path):
    """Belra whispers Arkon, he whispers back, and each keeps a memory of it. None of it
    reaches either one's prompt on guild chat or in a whisper to anyone else (the human, or
    Coros); his whisper back to her carries it all."""
    def script(model, messages):
        if "You play Belra" in messages[0]["content"]:
            return "psst im going horde next week"
        return "wait what why horde"

    async def main():
        seed_test_cast(store)
        for g in (101, 102):
            save_member(store, g, typo_rate=0.0, split_rate=0.0)
        r = make_rig(store, clock, tmp_path, script=script)
        assert await spoken(r, whisper_to(102, 101))
        assert await spoken(r, whisper_to(101, 102))
        lines = tuple(f"chat:{i}" for (i,) in store.conn.execute("SELECT id FROM chat_line"))
        t = int(clock.now())
        mine = {g: store.insert_memory(make_mem(
            owner=g, kind="conversation", text=text, actors=(other, HUMAN), source="extract",
            evidence=lines, t_event=t, t_created=t, t_accessed=t))
            for g, other, text in ((101, 102, f"Belra is going horde, not a word to {NICK}."),
                                   (102, 101, f"I told Arkon I go horde, not a word to {NICK}."))}
        for g in (101, 102):
            for turn in (reply_to_human(g), whisper_to(g, HUMAN), whisper_to(g, 103)):
                messages, ids = await r.engine.compose(turn)
                assert "horde" not in text_of(messages) and ids == [], (g, turn.to_guid)
        messages, ids = await r.engine.compose(whisper_to(101, 102))
        private = text_of(messages)
        assert "psst im going horde next week" in private and "wait what why horde" in private
        assert ids == [mine[101]]
    run(main())


def test_a_whisper_never_picks_a_public_turns_memories(store, clock, tmp_path):
    """A guild turn whose trigger is a whisper the guild never saw (a burst can follow up the
    speaker's last line): the whisper's words don't search her memories. A whisper back to
    him may."""
    async def main():
        r = make_rig(store, clock, tmp_path)
        line = store.insert_chat_line(Chat(
            ts=0, channel="whisper", from_guid=HUMAN, from_name="Testmage", to_guid=101,
            text="sindragosa tonight, just us?", map=0, zone=10), session_id=None, origin="human")
        t = int(clock.now())
        mid = store.insert_memory(make_mem(owner=101, text="We wiped on Sindragosa twice.",
                                           t_event=t, t_created=t, t_accessed=t))
        guild = Turn(kind=TurnKind.CONTINUE, speaker=101, channel="guild", addressee=102,
                     trigger_line=line)
        assert (await r.engine.compose(guild))[1] == []
        back = Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="whisper", to_guid=HUMAN,
                    addressee=HUMAN, trigger_line=line)
        assert (await r.engine.compose(back))[1] == [mid]
    run(main())


# -- flirting ----------------------------------------------------------------------------------

def test_a_flirt_at_a_member_is_dropped(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold")
        r = make_rig(store, clock, tmp_path, reply="you're trouble, zan")
        at_member = Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=101)
        assert await spoken(r, at_member) == [] and r.chat_calls() == []
        assert r.utterances()[-1]["dropped"] == "flirt_target"
        at_him = Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN)
        assert await spoken(r, at_him)  # positive control
    run(main())


def test_a_flirt_comes_only_from_a_woman_who_wants_him(store, clock, tmp_path):
    """Privacy minor 7: the FLIRT guard checked only the target, so a turn from a TurnSource or
    speak() could have a man, or a fond woman, flirt with him."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="fond")
        save_member(store, 103, player_gender="female", stance="drawn")
        r = make_rig(store, clock, tmp_path, script=numbered("hey you, round {n}"))
        for g in (101, 102):  # a man (the test cast is male), then a fond woman
            flirt = Turn(kind=TurnKind.FLIRT, speaker=g, channel="guild", addressee=HUMAN)
            assert await spoken(r, flirt, seconds=15) == []
            assert r.utterances()[-1]["dropped"] == "flirt_stance" and r.chat_calls() == []
        assert await spoken(r, Turn(kind=TurnKind.FLIRT, speaker=103, channel="guild",
                                    addressee=HUMAN))  # she wants him: positive control
    run(main())


def _plain_woman(store, guid: int = 104, **over) -> None:
    save_member(store, guid, **{**dict(player_gender="female", stance="lust", style="bold",
                                      typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0,
                                      abbreviations={}), **over})


def test_romance_at_him_that_names_a_member_goes_out(store, clock, tmp_path):
    """Fix round 1 (review I1): the widened screen dropped romance aimed at him. A whisper to
    him naming a rival, a flirt with no name in it and a rival's cut-in all go out."""
    async def main():
        seed_test_cast(store)
        _plain_woman(store)
        rival = "i want you so bad, don't tell belra"
        r = make_rig(store, clock, tmp_path, script=lambda m, msgs: rival)
        his = his_line(r, store, "you around later?", channel="whisper", to_guid=104)
        assert await spoken(r, whisper_back(104, his.id)) == [rival]
        for line in ("miss me?", "back off belra, he's mine ;)"):
            r = make_rig(store, clock, tmp_path, script=lambda m, msgs, line=line: line)
            await r.advance(70)
            flirt = Turn(kind=TurnKind.FLIRT, speaker=104, channel="guild", addressee=HUMAN)
            assert await spoken(r, flirt) == [line], line
    run(main())


def test_a_turn_to_him_that_turns_to_a_member_is_told_it_is_for_him(store, clock, tmp_path):
    """Fix round 1 (review I1): every such retry carried "Keep it friendly", asking a flirt at
    him to stop flirting. A line on his turn that opens on a member with desire is asked
    again with a hint that the line is for him."""
    async def main():
        seed_test_cast(store)
        _plain_woman(store)
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["arkon i want you so bad", "i want you so bad, zan"]))
        assert await spoken(r, reply_to_human(104)) == ["i want you so bad, zan"]
        retry = r.prompts()[-1]
        assert "Keep it friendly" not in retry and "Say it to Zan" in retry
    run(main())


def test_banter_with_a_member_gets_no_wink_and_no_romance_emote(store, clock, tmp_path):
    """Fix round 1 (review I2): the typing added ";)" to a line the screen had passed, and an
    emote command sent a cuddle at a member. A line to or naming a member gets no wink or
    heart (a smile still); kiss, wink, cuddle and blush go out untargeted, or at him."""
    async def main():
        seed_test_cast(store)
        _plain_woman(store, emoticons=[";)", "<3"], emoticon_rate=1.0)
        save_member(store, 103, emoticons=[":)"], emoticon_rate=1.0, typo_rate=0.0,
                    split_rate=0.0, abbreviations={})
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["ty for the heals dunna", "ty for the heals arkon", "/cuddle dunna lol"]))
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=104, channel="guild")) == [
            "ty for the heals dunna"]
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=103, channel="guild")) == [
            "ty for the heals arkon :)"]
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=104, channel="guild")) == ["lol"]
        (cuddle,) = r.sent.emotes()
        assert (cuddle.emote, cuddle.target_guid) == ("cuddle", 0)
    run(main())


def test_an_emote_command_is_sent_only_where_it_can_be(store, clock, tmp_path):
    """Fix round 1 (review minor 8): a command mid-sentence left a hole ("i'd you right now
    zan"), a line that was only an emote sent nothing, and an emote in a whisper went out in
    public. A command inside a sentence is its word; a line that is only one sends it; a
    whisper sends none and keeps its commands as words (fix round 2, re-review NB6: "/hug you
    later zan" went out as "you later zan")."""
    async def main():
        seed_test_cast(store)
        _plain_woman(store)
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["i'd /kiss you right now zan", "/wave", "/hug you later zan"]))
        assert await spoken(r, reply_to_human(104)) == ["i'd kiss you right now zan"]
        assert r.sent.emotes() == []
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=104, channel="guild")) == []
        (wave,) = r.sent.emotes()
        assert wave.emote == "wave"
        his = his_line(r, store, "night", channel="whisper", to_guid=104)
        await r.advance(70)
        assert await spoken(r, whisper_back(104, his.id)) == ["hug you later zan"]
        assert len(r.sent.emotes()) == 1  # no hug in public for a whisper
    run(main())


RR_ENGINE = [  # (turn kind, line, goes out): the re-review's cases through the engine
    ("flirt", "belra, back off ;) he's mine", True),
    ("flirt", "belra, nice try ;) zan's coming home with me", True),
    ("reply", "belra's cute but i want you so bad", True),
    ("reply", "i want you so bad arkon", False),
    ("reply", "you're so hot arkon", False),
    ("open", "oh arkon you're so hot", False),
    ("open", "god arkon i want you so bad", False),
    ("open", "lol belra's at it again ;)", True),
]


@pytest.mark.parametrize("kind,line,out", RR_ENGINE)
def test_the_member_screen_through_the_engine(store, clock, tmp_path, kind, line, out):
    """Fix round 2 (the re-review's rule): romance at him that mentions a rival goes out,
    desire spoken to a member is asked again and dropped, gossip about a member passes."""
    async def main():
        seed_test_cast(store)
        _plain_woman(store)
        r = make_rig(store, clock, tmp_path, script=lambda m, msgs: line)
        await r.advance(70)
        if kind == "flirt":
            turn = Turn(kind=TurnKind.FLIRT, speaker=104, channel="guild", addressee=HUMAN)
        elif kind == "reply":
            his = his_line(r, store, "you around tonight?")
            turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=104, channel="guild",
                        addressee=HUMAN, trigger_line=his.id)
        else:
            turn = Turn(kind=TurnKind.OPEN, speaker=104, channel="guild")
        assert (await spoken(r, turn) == [line]) is out, r.utterances()[-1]["dropped"]
    run(main())


def test_an_earlier_answer_never_backs_a_bystanders_span(store, clock, tmp_path):
    """Fix round 2 (re-review NB4): the other answers to his line backed a bystander's
    invented span, the rehearsal's own "has been raging for 2 years"."""
    async def main():
        seed_test_cast(store)
        for g in (101, 103):
            save_member(store, g, typo_rate=0.0, split_rate=0.0, emoticon_rate=0.0,
                        abbreviations={}, words_per_line=[3, 20])
        span = "that epic gem debt has been going on for 2 years"
        answer = "nope, belra still owes me for that epic gem lol"
        r = make_rig(store, clock, tmp_path,
                     script=lambda m, msgs: answer if "You play Arkon" in msgs[0]["content"]
                     else span)
        his = his_line(r, store, "arkon did belra ever pay you back for that epic gem?")
        first = Turn(kind=TurnKind.REPLY_HUMAN, speaker=101, channel="guild", addressee=HUMAN,
                     trigger_line=his.id)
        task = asyncio.create_task(r.engine._speak(first, TurnMeta("t1", answers_human=True)))
        await r.advance(40)
        assert task.result() == [answer]
        bystander = Turn(kind=TurnKind.REPLY_HUMAN, speaker=103, channel="guild",
                         addressee=HUMAN, trigger_line=his.id)
        task = asyncio.create_task(r.engine._speak(bystander, TurnMeta("t2", answers_human=True)))
        await r.advance(40)
        assert task.result() == [] and r.utterances()[-1]["dropped"] == "tells"
    run(main())


def test_a_line_naming_him_by_his_character_name_may_still_get_a_wink(store, clock, tmp_path):
    """Fix round 2 (re-review NB7): the wink gate counted his own character name as a
    member's."""
    async def main():
        seed_test_cast(store)
        _plain_woman(store, emoticons=[";)"], emoticon_rate=1.0)
        r = make_rig(store, clock, tmp_path, script=lambda m, msgs: "nice pull testmage")
        # the live cast lists him too (the rig's leaves him out)
        r.world.members[HUMAN] = Member(guid=HUMAN, name="Testmage", race=1, class_=8, gender=0,
                                        level=80, rank=0, online=True, map=0, zone=10,
                                        spec_tab=-1)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=104, channel="guild")) == [
            "nice pull testmage ;)"]
    run(main())


def test_no_mark_is_added_to_her_romance_or_to_a_wipe(store, clock, tmp_path):
    """Rehearsal 2 (judge C 4): the send step bolted "lmao" onto "dream of me ;)" and "aww"
    onto a wipe line. A line that carried her romance text, or a wipe reaction, gets no mark;
    a plain line still may."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", emoticons=[":P"],
                    emoticon_rate=1.0, typo_rate=0.0, split_rate=0.0)
        save_member(store, 103, emoticons=[":P"], emoticon_rate=1.0, typo_rate=0.0,
                    split_rate=0.0)
        r = make_rig(store, clock, tmp_path, script=numbered("you look sharp zan, take {n}"))
        flirt = Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN)
        assert await spoken(r, flirt) == ["u look sharp zan, take 1"]
        await r.advance(70)
        wipe = Turn(kind=TurnKind.WIPE_REACT, speaker=103, channel="guild")
        assert await spoken(r, wipe) == ["u look sharp zan, take 2"]
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=103, channel="guild")) == [
            "u look sharp zan, take 3 :P"]
    run(main())


def test_a_flirt_for_him_that_lands_on_a_member_is_dropped_and_sets_off_nothing(store, clock,
                                                                              tmp_path):
    """Rehearsal 2 (judge B 5, judge C 7): a FLIRT turn at him went to another member ("happy
    birthday dunna! if i was there i'd make you a cake"), and the jealousy aside read it as
    a flirt. Such a line is asked again, then dropped, and sets off no aside; turns to him are
    screened too, for a line that opens on a member ("arkon i want you so bad" in a reply to
    him), and the retry says the line is his, never "keep it friendly" (fix round 1)."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", style="bold",
                    typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0)
        for g in (101, 103, 104, 105):
            save_member(store, g, jealousy_mode="teaser")
        cake = "happy birthday dunna! if i was there i'd make you a cake :)"
        r = make_rig(store, clock, tmp_path, jealousy=10, script=script_from([cake, cake]))
        flirt = Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN)
        assert await spoken(r, flirt) == [] and r.utterances()[-1]["dropped"] == "tells"
        assert "Say it to Zan" in r.prompts()[-1] and "Keep it friendly" not in r.prompts()[-1]
        await r.advance(60)
        assert not any("flirting with Zan" in p for p in r.prompts())  # no aside
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["arkon i want you so bad", "arkon i want you so bad"]))
        assert await spoken(r, reply_to_human(102)) == []
    run(main())


def test_a_flirt_aimed_at_a_member_never_reaches_chat(store, clock, tmp_path):
    """Prompts I10: an output screen for the content invariant. Romance aimed at a member
    (named after the romance word, or "you" on a turn to a member) is asked again, then
    dropped; romance aimed at him, or talk about someone flirting with him, is chat."""
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", typo_rate=0.0,
                    emoticon_rate=0.0, split_rate=0.0)
        save_member(store, 103, typo_rate=0.0, emoticon_rate=0.0, split_rate=0.0)
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["lol arkon i've had a crush on you for ages", "kissing Coros at the bank lol"]))
        to_arkon = Turn(kind=TurnKind.CONTINUE, speaker=102, channel="guild", addressee=101)
        assert await spoken(r, to_arkon) == []
        assert r.utterances()[-1]["dropped"] == "tells" and len(r.chat_calls()) == 2
        r = make_rig(store, clock, tmp_path, script=script_from(
            ["had a crush on you since wrath lol", "Belra is flirting with Zan again lol"]))
        await r.advance(70)
        assert await spoken(r, reply_to_human(102)) == ["had a crush on u since wrath lol"]
        await r.advance(70)
        aside = Turn(kind=TurnKind.ASIDE, speaker=103, channel="guild",
                     instruction="Belra is flirting with Zan in chat.")  # as asides are planned
        assert await spoken(r, aside) == ["belra is flirting with zan again lol"]
    run(main())


# -- the library's facts (fix wave part E) -----------------------------------------------------

FACT_CHUNKS = [
    chunk("Lancer Grounds", "The Lancer Grounds are east of the Frost Peak in Icecrown, where "
          "the Lancer's Trial dailies start.", kind="zone"),
    chunk("The Lancer's Trial", "Quest The Lancer's Trial (level 80, Tournament, daily). Starts "
          "with Marshal Brightlance.", kind="quest", source="world")]
FACTS_MARK = "Facts you know (use them if they help"
DAILIES = "anyone know where the lancer's trial dailies start?"


def test_an_unrelated_line_never_takes_the_facts_from_his_first_answer(store, clock, tmp_path):
    """Fix round 1 (review I3): any member line after his game question counted as an answer,
    so a burst line landing before the first reply took its facts away and told it "already
    answered". Only lines sent as answers to his line count."""
    from leeroy_mind.talk.questions import game_question

    async def main():
        seed_test_cast(store)
        r = make_rig(store, clock, tmp_path, script=numbered(), library=FakeLibrary(FACT_CHUNKS))
        await r.session()
        line = his_line(r, store, DAILIES)
        r.engine.facts.start(line.id, game_question(DAILIES))
        task = asyncio.create_task(r.engine.speak(Turn(kind=TurnKind.OPEN, speaker=105,
                                                       channel="guild")))
        await r.advance(20)
        assert task.result()  # a burst line after his question, not an answer to it
        meta = TurnMeta("t", answers_human=True, facts_line=line.id)
        turn = Turn(kind=TurnKind.REPLY_HUMAN, speaker=102, channel="guild", addressee=HUMAN,
                    trigger_line=line.id)
        user = (await r.engine._compose(turn, meta, live=False)).messages[1]["content"]
        assert FACTS_MARK in user and "already answered" not in user
    run(main())


def test_the_first_reply_to_his_game_question_gets_the_facts_and_nobody_else(store, clock,
                                                                            tmp_path):
    """Fix wave G1 (judge A 3): every replier got the same facts, so the second answer
    recited the first. The first answer gets them; the later ones are told it's answered."""
    async def main():
        lib = FakeLibrary(FACT_CHUNKS)
        r = make_rig(store, clock, tmp_path, script=numbered(), library=lib)
        r.engine.bits.p_fire = 0.0  # a burst opens on its own topic, not a bit
        await r.session()
        await r.human(DAILIES)
        await r.advance(45, step=2)
        replies = r.prompts()
        assert len(replies) >= 2 and len(r.sent.says()) == len(replies)
        first, *later = replies
        assert FACTS_MARK in first and "point him to someone who'd know" in first
        assert "The Lancer Grounds are east of the Frost Peak in Icecrown" in first
        assert "Starts with Marshal Brightlance." in first
        for p in later:
            assert FACTS_MARK not in p and "Marshal Brightlance" not in p
            assert "already answered that. Add your own take, or answer (nothing)." in p
        assert len(lib.searches) == 1  # one lookup for the line, whoever answers it
        assert lib.searches[0]["priority"] is Priority.REPLY_HUMAN
        assert lib.searches[0]["llm"] is r.engine.scheduler
        seen = len(replies)
        await r.advance(61)
        await r.human("how was your day?")  # a question, not a game one
        await r.advance(45, step=2)
        # nor any of the guild's other talk (review minor 8): a burst, his login's greetings,
        # the gz for his achievement and the raid lead's wipe call
        await r.advance(61)
        r.engine._spawn(r.engine._run_burst(BurstPlan(2, 2)))
        await r.advance(60, step=2)
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(90, step=2)
        await r.event(Achievement(ts=0, guid=HUMAN, achievement_id=7, name="Lancer of Note"))
        await r.advance(30, step=2)
        await r.event(Group(ts=0, leader_guid=102, raid=True, members=(HUMAN, *TEST_CAST)))

        def boss(state):
            return Boss(ts=0, map=631, instance_id=5, boss_id=0, boss_name="", state=state,
                        members=(HUMAN, *TEST_CAST))
        await r.event(boss("engaged"))
        await r.advance(20)
        await r.event(boss("wiped"))
        await r.advance(70)
        later = r.prompts()[seen:]
        assert any("Respond to what he actually said" in p for p in later)  # his next line
        assert any("make sense on its own" in p for p in later)  # the burst
        assert any("just logged on" in p for p in later)  # the greetings
        assert any("just got Lancer of Note" in p for p in later)  # the gz
        assert any("You're leading the raid" in p for p in later)  # the lead's call
        assert not any(FACTS_MARK in p for p in later)
        assert len(lib.searches) == 1
    run(main())


def test_a_reply_with_facts_pins_his_line_for_the_trim(store, clock, tmp_path, monkeypatch):
    """Review minor 3: with facts, his line is the floor of the chat the trim keeps."""
    import leeroy_mind.talk.pipeline as pipeline
    seen = []
    real_trim = pipeline.trim_to_budget

    def spy(parts, **kw):
        seen.append(parts)
        return real_trim(parts, **kw)
    monkeypatch.setattr(pipeline, "trim_to_budget", spy)

    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered(), library=FakeLibrary(FACT_CHUNKS))
        await r.human(DAILIES)
        await r.advance(45, step=2)
        await r.human("how was your day?")
        await r.advance(45, step=2)
    run(main())
    with_facts = [p for p in seen if p.facts]
    assert with_facts and all(p.chat[p.pinned].endswith(DAILIES) for p in with_facts)
    assert all(p.pinned is None for p in seen if not p.facts)


def test_his_whispered_game_question_gets_the_facts_in_the_reply(store, clock, tmp_path):
    async def main():
        answered = 0
        for seed in range(3):
            lib = FakeLibrary(FACT_CHUNKS)
            r = make_rig(store, clock, tmp_path, seed=seed, script=numbered(), library=lib)
            await r.human("where are the lancer grounds, icecrown?", channel="whisper",
                          to_guid=103)
            await r.advance(45, step=2)
            says = r.sent.says()
            assert all(s.guid == 103 and s.channel == "whisper" for s in says)
            assert all(FACTS_MARK in p for p in r.prompts()) and len(lib.searches) == 1
            answered += bool(says)
            await r.engine.end_session()
        assert answered
    run(main())


def test_his_lore_question_is_answered_by_the_guild_historian(store, clock, tmp_path):
    """The topic-fit replier comes from persona data, never a name: for lore and strategy the
    member whose officer title makes them the guild's historian, then the lore minds."""
    async def main():
        seed_test_cast(store)
        save_member(store, 104, guild_role="officer", officer_title="Guild Historian")
        save_member(store, 102, thinks_as="lore")
        whys = []
        for seed in range(6):
            lib = FakeLibrary([chunk("Prince Arvel", "Prince Arvel was the heir of Lordane.")])
            r = make_rig(store, clock, tmp_path, seed=seed, script=numbered(), library=lib)
            await r.human("who was prince arvel before he took the frost blade, any lore?")
            await r.advance(45, step=2)
            assert 104 in {s.guid for s in r.sent.says()}, seed
            whys += [u["why"] for u in store.conn.execute(
                "SELECT why FROM utterance WHERE guid = 104 AND kind = 'reply_human'")]
            store.conn.execute("DELETE FROM utterance")
            await r.engine.end_session()
        assert "reply:topic_fit" in whys  # put in when the draw left the historian out
        save_member(store, 104, guild_role="officer", officer_title="Bank Keeper")
        script = numbered()  # one for every rig: a fresh mind reads her last lines back
        for seed in range(6):  # no historian: one who thinks in lore
            r = make_rig(store, clock, tmp_path, seed=seed, script=script,
                         library=FakeLibrary([chunk("Prince Arvel", "He was the heir.")]))
            await r.human("who was prince arvel before he took the frost blade, any lore?")
            await r.advance(45, step=2)
            assert 102 in {s.guid for s in r.sent.says()}, seed
            await r.engine.end_session()
    run(main())


def test_his_quest_question_gets_a_quest_helper_with_the_facts(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 105, player_gender="female")
        talk = numbered()  # one count across the rigs: her own lines stay in the store
        for seed in range(4):
            r = make_rig(store, clock, tmp_path, seed=seed, script=talk,
                         library=FakeLibrary(FACT_CHUNKS))
            await r.human(DAILIES)
            await r.advance(45, step=2)
            assert 105 in {s.guid for s in r.sent.says()}, seed
            hers = [p for p in r.prompts() if "You play Ebbo" in p]
            # the quest helper who knows it answers first, with the facts (fix wave G1)
            assert hers and FACTS_MARK in hers[0] and "You play Ebbo" in r.prompts()[0]
            await r.engine.end_session()
    run(main())


def test_his_game_question_put_to_one_member_is_answered_by_that_member_alone(store, clock,
                                                                             tmp_path):
    """Fix wave G5: the topic-fit replier joins a question to the guild (the test above), not
    one he put to a member by name. The member he named answers it, with the facts."""
    async def main():
        seed_test_cast(store)
        save_member(store, 105, player_gender="female")  # the quest helper that would be added
        talk = numbered()
        for seed in range(6):
            lib = FakeLibrary(FACT_CHUNKS)
            r = make_rig(store, clock, tmp_path, seed=seed, script=talk, library=lib)
            await r.human("arkon, where do the lancer's trial dailies start?")
            await r.advance(45, step=2)
            assert [s.guid for s in r.sent.says()] == [101], seed
            assert FACTS_MARK in r.prompts()[0] and len(lib.searches) == 1
            await r.engine.end_session()
    run(main())


def test_the_facts_are_for_his_faction_by_his_race_on_the_roster(store, clock, tmp_path):
    """Fix round 2 (controller): the guild is Horde, so a quest the library tags Alliance is no
    fact for it. The faction comes from his race on the stored roster, never from the config;
    an unknown race keeps both sides."""
    ally = chunk("The Lancer's Oath", "Quest The Lancer's Oath (level 80, Tournament, daily, "
                 "Alliance). Starts with Marshal Brightlance.", kind="quest", source="world")
    horde = chunk("The Lancer's Vow", "Quest The Lancer's Vow (level 80, Tournament, daily, "
                  "Horde). Starts with Warden Ashtusk.", kind="quest", source="world")
    givers = {"Marshal Brightlance", "Warden Ashtusk"}

    async def main():
        for race, kept in ((5, {"Warden Ashtusk"}), (1, {"Marshal Brightlance"}), (0, givers)):
            r = make_rig(store, clock, tmp_path, script=numbered(),
                         library=FakeLibrary([ally, horde]))
            store.conn.execute("DELETE FROM cast_member WHERE guid = ?", (HUMAN,))
            if race:
                store.upsert_cast([Member(guid=HUMAN, name="Testmage", race=race, class_=8,
                                          gender=0, level=80, rank=0, online=True, map=0,
                                          zone=10, spec_tab=-1)], 1000.0)
            r.engine.set_human(HUMAN)
            await r.human("anyone know where the lancer dailies start?")
            await r.advance(45, step=2)
            told = [p for p in r.prompts() if FACTS_MARK in p]
            assert told, race
            assert all({g for g in givers if g in p} == kept for p in told), race
            await r.engine.end_session()
    run(main())


def test_without_a_library_his_game_question_is_answered_as_before(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered())
        await r.human(DAILIES)
        await r.advance(45, step=2)
        assert r.prompts() and not any(FACTS_MARK in p for p in r.prompts())
        assert r.engine.facts is None
    run(main())


# -- fix wave G3: what rehearsal 3 still found --------------------------------------------------

class _HighRoll(random.Random):
    """Every roll lands at 0.95, over WHISPER_REPLY_P's 0.9."""

    def random(self):
        return 0.95


def test_her_answer_to_his_whisper_goes_out_right_after_a_guild_line_of_hers(store, clock,
                                                                            tmp_path):
    """Rehearsal 3, check 4: a lust woman's reply to his whisper was dropped by the per-person
    reply guard (REPLY_S), because a guild bit of hers had gone out 4 s before."""
    async def main():
        _women(store)
        r = make_rig(store, clock, tmp_path, script=numbered())
        task = asyncio.create_task(r.engine.speak(Turn(kind=TurnKind.OPEN, speaker=102,
                                                       channel="guild")))
        while not r.sent.says():
            await r.advance(0.5, 0.5)
        await r.human("come find me by the docks after raid?", channel="whisper",
                      to_guid=102)
        await r.advance(40)
        assert task.done()
        assert [s.channel for s in r.sent.says() if s.guid == 102][:2] == ["guild", "whisper"]
        assert not [u for u in r.utterances() if u["dropped"] == "guards"]
        # in guild chat the guard keeps its job: her answer to his guild line waits 10 s
        r.engine.guards.record("t-guild", 102, r.clock.now(), TurnKind.OPEN)
        await r.human("belra you there?")
        await r.advance(30)
        assert [u["dropped"] for u in r.utterances() if u["guid"] == 102][-1] == "guards"
    run(main())


def test_a_woman_with_an_attitude_toward_him_answers_every_whisper_of_his(store, clock,
                                                                          tmp_path):
    """Rehearsal 3, check 4: his third whisper to a drawn woman drew no reply, the one in ten
    WHISPER_REPLY_P leaves out. A woman fond of him, drawn to him or wanting him answers every
    whisper; a man keeps the 0.9."""
    async def main():
        _women(store)
        r = make_rig(store, clock, tmp_path, script=numbered())
        r.engine.rng = _HighRoll(1)
        for g in (101, 102, 103, 104):
            await r.human("you around?", channel="whisper", to_guid=g)
        await r.advance(40)
        assert {s.guid for s in r.sent.says() if s.channel == "whisper"} == {102, 103, 104}
    run(main())


def test_a_silent_answer_to_his_whisper_is_asked_once_more(store, clock, tmp_path):
    """Seen live (coordinator, fix wave G3): an answer to his whisper came back "(nothing)" and
    was dropped as silent. It is asked once more, with a hint to answer him in a line in her
    own voice (a line or two at the explicit band: fix round 1, review M5), and a second
    silence is accepted. Silence anywhere else stays a valid answer."""
    hint = "Answer Zan's whisper in a line, in your own voice."
    two = "Answer Zan's whisper in a line or two, in your own voice."

    async def main():
        _women(store)
        r = make_rig(store, clock, tmp_path, script=script_from(["(nothing)", "come here then"]))
        line = his_line(r, store, "you around?", channel="whisper", to_guid=102).id
        assert await spoken(r, whisper_back(102, line)) == ["come here then"]
        assert len(r.chat_calls()) == 2 and two in r.prompts()[1] and two not in r.prompts()[0]
        r = make_rig(store, clock, tmp_path, script=script_from(["(nothing)", "/w Zan"]))
        line = his_line(r, store, "still there?", channel="whisper", to_guid=103).id
        assert await spoken(r, whisper_back(103, line)) == []
        assert r.utterances()[-1]["dropped"] == "silent" and len(r.chat_calls()) == 2
        assert hint in r.prompts()[1]  # the drawn woman is not at the explicit band
        r = make_rig(store, clock, tmp_path, script=script_from(["(nothing)", "hey"]))
        g = his_line(r, store, "anyone around?").id
        assert await spoken(r, Turn(kind=TurnKind.REPLY_HUMAN, speaker=104, channel="guild",
                                    addressee=HUMAN, trigger_line=g)) == []
        assert await spoken(r, Turn(kind=TurnKind.WHISPER, speaker=105, channel="whisper",
                                    to_guid=HUMAN, addressee=HUMAN)) == ["hey"]
        assert [u["dropped"] for u in r.utterances()][-2:] == ["silent", None]
        assert all(hint not in p for p in r.prompts())
    run(main())


def test_the_engine_keeps_the_models_words_for_each_line_it_said(store, clock, tmp_path):
    """Rehearsal 3, check 8: extraction's copy rule needs the words before the typing (a typo
    broke its six-word run), which only the engine has. A line it never said has none."""
    async def main():
        seed_test_cast(store)
        save_member(store, 101, typo_rate=0.0, split_rate=0.0, abbreviations={"you": "u"},
                    emoticon_rate=0.0)
        words = "you keep a whole bag slot for your alts these days"
        r = make_rig(store, clock, tmp_path, reply=words)
        sent = await spoken(r, Turn(kind=TurnKind.OPEN, speaker=101, channel="guild"))
        (line_id, text), = store.conn.execute(
            "SELECT id, text FROM chat_line WHERE from_guid = 101").fetchall()
        assert [text] == sent == ["u keep a whole bag slot for your alts these days"]
        assert r.engine.said_words(line_id) == words
        assert r.engine.said_words(line_id + 1) is None
    run(main())


def test_a_command_or_a_handle_at_him_never_reaches_chat(store, clock, tmp_path):
    """Rehearsal 3, finding 5: an unprompted whisper to him went out as "/w <his character
    name> dude that kill was so ...". A leading whisper or reply command, @handle or "Name:"
    at him is stripped on every channel, and a line that is only a command is dropped."""
    async def main():
        _women(store)
        for g, stance in ((102, "lust"), (103, "lust"), (104, "fond")):
            save_member(store, g, player_gender="female", stance=stance, typo_rate=0.0,
                        split_rate=0.0, abbreviations={}, emoticon_rate=0.0)
        r = make_rig(store, clock, tmp_path, script=script_from([
            "/w Testmage that kill was something", "@Zan come sit with me then",
            "Zan: you're late", "/w Zan", "/invite arkon"]))
        whisper = Turn(kind=TurnKind.WHISPER, speaker=102, channel="whisper", to_guid=HUMAN,
                       addressee=HUMAN)
        assert await spoken(r, whisper) == ["that kill was something"]
        flirt = Turn(kind=TurnKind.FLIRT, speaker=103, channel="guild", addressee=HUMAN)
        assert await spoken(r, flirt, seconds=70) == ["come sit with me then"]
        line = his_line(r, store, "where were you").id
        assert await spoken(r, Turn(kind=TurnKind.REPLY_HUMAN, speaker=104, channel="guild",
                                    addressee=HUMAN, trigger_line=line)) == ["you're late"]
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=101, channel="guild")) == []
        await r.advance(70)
        assert await spoken(r, Turn(kind=TurnKind.OPEN, speaker=105, channel="guild")) == []
        assert [u["dropped"] for u in r.utterances()][-2:] == ["silent", "command"]
    run(main())


def test_an_opener_never_answers_a_line_of_his(store, clock, tmp_path):
    """Rehearsal 3, check 2: a member's opener answered his question to another member 80 s
    before, though its topic was her own. An opener's chat leaves his lines out and says
    none of it is hers to answer; the other members' lines stay."""
    async def main():
        seed_test_cast(store)
        r = make_rig(store, clock, tmp_path)
        his_line(r, store, "arkon what are you up to tonight?")
        await r.advance(20)
        member_line(r, store, 101, "just back from the market, you?")
        msgs, _ = await r.engine.compose(Turn(kind=TurnKind.OPEN, speaker=103, channel="guild"))
        user = msgs[1]["content"]
        assert "what are you up to tonight" not in user
        assert "Arkon: just back from the market, you?" in user
        assert "nothing in chat is addressed to you" in user
        line = his_line(r, store, "coros you around?").id  # an answer to him still sees it
        msgs, _ = await r.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN, speaker=103,
                                              channel="guild", addressee=HUMAN,
                                              trigger_line=line))
        assert "Zan: coros you around?" in msgs[1]["content"]
    run(main())


# His questions about the guild's past that name nobody (rehearsal 3, check 3). The guild matter
# is invented (bank duty split into even and odd weeks; a potion feud), shaped like the real one.
KEEPER_MARK = "You remember this one: answer from that memory, in your own words."
THEIRS_MARK = "remembers it: it's their story, so react to it and don't add facts."
NOBODY_MARK = "Nobody here remembers that for sure"


def _history(store, clock, owner: int, text: str, *, days: int = 400) -> int:
    """A memory of the guild's past in the owner's seeded history."""
    t = int(clock.now()) - days * 86400
    return store.insert_memory(make_mem(owner=owner, text=text, actors=(HUMAN, owner),
                                        t_event=t, t_created=t, t_accessed=t, source="owner",
                                        evidence=("owner:G3",)))


def test_his_question_about_the_guilds_past_is_answered_first_by_one_who_remembers(
        store, clock, tmp_path):
    """Rehearsal 3, check 3: "who gets the ... again?" and "how long has the ... been going?"
    named nobody, and five members who held no memory of it answered (one falsely: "it's my
    turn"); the two who held it were never drawn. One who remembers answers first, from that
    memory (pinned to her prompt though it rests); the others react to her story and add no
    fact."""
    async def main():
        seed_test_cast(store)
        keep = _history(store, clock, 104, "In the winter of 2008 Zan split bank duty between "
                        "Belra and me: the even weeks are mine, the odd weeks hers.")
        for n in range(6):  # memories that outrank it while it rests
            _history(store, clock, 104, f"I restocked the guild bank tab number {n} for Zan.",
                     days=10 + n)
        store.touch_memories([keep], clock.now())
        r = make_rig(store, clock, tmp_path, script=numbered())
        await r.human("who gets the even weeks for bank duty again?")
        await r.advance(60, step=2)
        prompts = r.prompts()
        assert len(prompts) >= 2 and "You play Dunna" in prompts[0]
        assert KEEPER_MARK in prompts[0] and "the even weeks are mine" in prompts[0]
        assert all(THEIRS_MARK in p and "Dunna remembers it" in p and KEEPER_MARK not in p
                   for p in prompts[1:])
        assert "Add something they didn't" not in "".join(prompts[1:])
        assert not any(NOBODY_MARK in p for p in prompts)
    run(main())


def test_with_nobody_here_who_remembers_it_nobody_states_a_fact(store, clock, tmp_path):
    """Rehearsal 3, check 3: with no one online who holds it, the replies may joke or not
    know ("no idea, ask dunna"), and say who would know. With nobody holding it at all, the
    question is answered as before, the history tell its net (fix round 1, review C1)."""
    async def main():
        seed_test_cast(store)
        _history(store, clock, 104, "Belra and I have had a potion feud since the cauldron "
                 "fire in 2008.")
        r = make_rig(store, clock, tmp_path, script=numbered(), online=(101, 102, 103, 105))
        await r.human("how long has the potion feud been going?")
        await r.advance(60, step=2)
        prompts = r.prompts()
        assert prompts and all(NOBODY_MARK + ", but Dunna would know" in p for p in prompts)
        assert not any(KEEPER_MARK in p or THEIRS_MARK in p for p in prompts)
        await r.engine.end_session()
        r = make_rig(store, clock, tmp_path, script=numbered())
        await r.human("since when do we have a bank keeper?")
        await r.advance(60, step=2)
        prompts = r.prompts()
        assert prompts and not any(m in p for p in prompts
                                   for m in (KEEPER_MARK, THEIRS_MARK, NOBODY_MARK))
    run(main())


def _everyday(store, clock, n: int = 12) -> None:
    """Old memories in the everyday words of raid nights, as the owner's history has hundreds:
    those words are common across the guild's memories (fix round 1, review C1)."""
    for i in range(n):
        _history(store, clock, 101 + i % 5, f"Raid night {i}: guild chat asked if anyone needed "
                 "anything from the bank, the tank wanted a good plan for the pull.",
                 days=30 + i)


@pytest.mark.parametrize("line", [
    "what time is raid night?", "have a good night", "brb 10m, anyone need anything?",
    "who's tanking again?", "what's the plan again?", "how long was that last pull?",
    "what did i miss in guild chat?", "is it raid night?"])
def test_his_everyday_questions_are_answered_as_before(store, clock, tmp_path, line):
    """Fix round 1 (review C1): everyday questions of his that share everyday words with old
    memories put a member first "from that memory" and told the rest it was her story, and a
    bare "...again?" told everyone nobody remembers it. Only a rare shared word counts."""
    async def main():
        seed_test_cast(store)
        _everyday(store, clock)
        _history(store, clock, 104, "In the winter of 2008 Zan split bank duty between Belra "
                 "and me: the even weeks are mine, the odd weeks hers.")
        r = make_rig(store, clock, tmp_path, script=numbered())
        await r.human(line)
        await r.advance(60, step=2)
        prompts = r.prompts()
        assert prompts and not any(m in p for p in prompts
                                   for m in (KEEPER_MARK, THEIRS_MARK, NOBODY_MARK)), line
    run(main())


@pytest.mark.parametrize("line, keeper", [
    ("who gets the even weeks for bank duty again?", "Dunna"),
    ("what was the bank pact again?", "Ebbo"), ("when was the bank pact?", "Ebbo")])
def test_his_questions_about_the_guilds_past_still_find_their_keepers(store, clock, tmp_path,
                                                                      line, keeper):
    """Fix round 1 (review C1): among the everyday memories only the true holder counts, by a
    rare shared word; the members who share only "bank" are none."""
    async def main():
        seed_test_cast(store)
        _everyday(store, clock)
        _history(store, clock, 104, "In the winter of 2008 Zan split bank duty between Belra "
                 "and me: the even weeks are mine, the odd weeks hers.")
        _history(store, clock, 105, "Zan wrote the bank pact in 2008 so Arkon and I would stop "
                 "arguing over the gold.")
        r = make_rig(store, clock, tmp_path, script=numbered())
        await r.human(line)
        await r.advance(60, step=2)
        prompts = r.prompts()
        assert len(prompts) >= 2 and f"You play {keeper}" in prompts[0], line
        assert KEEPER_MARK in prompts[0]
        assert all(f"{keeper} remembers it: it's their story" in p for p in prompts[1:]), line
    run(main())


def test_a_question_naming_a_member_a_whisper_or_a_plan_is_answered_as_before(store, clock,
                                                                             tmp_path):
    async def main():
        seed_test_cast(store)
        _history(store, clock, 104, "In the winter of 2008 Zan split bank duty between Belra "
                 "and me: the even weeks are mine, the odd weeks hers.")
        _history(store, clock, 103, "We farmed the twilight drake every week in 2008, all of "
                 "us, until it dropped.")
        r = make_rig(store, clock, tmp_path, script=numbered())
        for text, channel, to in (("coros who gets the even weeks for bank duty again?",
                                   "guild", 0),
                                  ("who gets the even weeks for bank duty again?", "whisper",
                                   102),
                                  ("anyone wanna farm the twilight drake tonight?", "guild", 0)):
            await r.human(text, channel=channel, to_guid=to)
            await r.advance(60, step=2)
        prompts = r.prompts()
        assert prompts and not any(m in p for p in prompts
                                   for m in (KEEPER_MARK, THEIRS_MARK, NOBODY_MARK))
    run(main())


def test_flirt_chance_by_attitude():
    def woman(stance):
        return make_persona(102, "Belra", player_gender="female", stance=stance)
    assert flirt_chance(woman("none"), 1.6) == 0 and flirt_chance(woman("fond"), 1.6) == 0
    assert flirt_chance(woman("drawn"), 1.0) == pytest.approx(0.3)
    assert flirt_chance(woman("lust"), 1.0) == pytest.approx(0.45)
    assert flirt_chance(woman("drawn"), 1.6) == pytest.approx(0.48)
    assert flirt_chance(woman("lust"), 2.5) == pytest.approx(0.9)  # capped
    assert flirt_chance(make_persona(101, "Arkon", stance="lust"), 1.0) == 0  # men are friends


def test_her_own_dial_ten_reaches_no_turn_that_is_not_to_him(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust", dial=10)
        r = make_rig(store, clock, tmp_path, dial=3)
        for turn in (Turn(kind=TurnKind.CONTINUE, speaker=102, channel="guild", addressee=101),
                     Turn(kind=TurnKind.OPEN, speaker=102, channel="guild"),
                     Turn(kind=TurnKind.CONTINUE, speaker=102, channel="whisper", to_guid=101,
                          addressee=101)):
            msgs, _ = await r.engine.compose(turn)
            assert ROMANCE_MARK not in msgs[1]["content"] and OVERLAY not in msgs[1]["content"]
    run(main())


def test_his_own_ten_is_not_hers(store, clock, tmp_path):
    async def main():
        seed_test_cast(store)
        save_member(store, 102, player_gender="female", stance="lust")
        r = make_rig(store, clock, tmp_path, dial=10)
        plain = his_line(r, store, "anyone seen my hearthstone").id
        r.engine._romance_at[102] = r.clock.now()
        r.engine._compose_rng.seed("share")
        got = 0
        for _ in range(300):
            msgs, _ = await r.engine.compose(Turn(kind=TurnKind.REPLY_HUMAN, speaker=102,
                                                  channel="guild", addressee=HUMAN,
                                                  trigger_line=plain))
            got += ROMANCE_MARK in msgs[1]["content"]
        assert 0.28 <= got / 300 <= 0.47, got / 300  # her 0.75, halved by the cooling-off
    run(main())
