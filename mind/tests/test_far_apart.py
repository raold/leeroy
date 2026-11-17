"""Far-apart members stay far apart (evening DM audit, 2026-11-16: a member who has never met him
planned a date with him at an event of hers, from a stored note to settle something in person):
seeing him in person is a soft tell of its own ("in person", never the summons'
hard rules) on her lines to him, her note and intentions that see him in person stay out of her
prompt until her arc has met him (#325's FAR_AWAY_MET), and a later expansion named is a soft
tell ("era"). Each on a guild line, his whisper and his DM. Invented members and lines only."""
import asyncio

import pytest

from conftest import HUMAN, make_mem, seed_test_cast
from engine_rig import engine_clock, make_rig, save_member
from leeroy_mind.storage.rows import Relationship, pack_vec
from leeroy_mind.talk.filters import find_tells
from leeroy_mind.talk.moves import in_person, is_summon
from leeroy_mind.talk.pipeline import (DROP_ON_RETRY, ERA_HINT, IN_PERSON_HINT, OWED_HARD_TELLS,
                                       TurnMeta, _far_memories)
from leeroy_mind.talk.prompt import far_note, relationship_card
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.testing.fake_llm import fake_embed

MEET = "come to the gig on saturday, you can sit next to me"
NOTE = "Thinks his jokes land. Owes him a rematch she wants to settle in person"


@pytest.mark.parametrize("text", [
    MEET, "ur showing up with snacks", "i need u there for the gig",
    "come on over saturday", "i'll sort it out in person next week", "i want to take you out",
    "i want you in the flesh", "can't wait to see u irl", "you better show up at the pub",
])
def test_seeing_him_in_person(text):
    assert in_person(text)


@pytest.mark.parametrize("text", [
    "we've never met in person lol", "come to the raid tonight", "you better show up to raid",
    "the loot didn't show up in my mail", "see you in dalaran", "join the group for the gig",
    "i'd never take you out of the raid team",
])
def test_not_in_person(text):
    assert not in_person(text)


def test_a_soft_tell_of_its_own_never_the_summons():
    assert not is_summon(MEET)
    assert [(t.reason, t.fatal, t.soft) for t in find_tells(MEET, far=True)] == [
        ("in person", False, True)]
    assert find_tells(MEET) == [] and find_tells(MEET, summons_ok=False) == []
    assert "in person" not in OWED_HARD_TELLS and "in person" not in DROP_ON_RETRY


def test_a_later_expansion_named_is_a_soft_tell():
    assert [t.reason for t in find_tells("we argued about the Molten Front all night")] == ["era"]
    assert [t.reason for t in find_tells("Three Hammers business, not mine")] == ["era"]
    assert find_tells("grab a mop, the floor is covered") == []
    assert "era" not in OWED_HARD_TELLS and "era" not in DROP_ON_RETRY


# -- her note and intentions ------------------------------------------------------------------------

def test_her_note_without_the_sentence_that_sees_him():
    assert far_note(NOTE) == "Thinks his jokes land."
    assert far_note("Thinks his jokes land") == "Thinks his jokes land"
    assert far_note("We have never met in person. He makes her laugh") == (
        "We have never met in person. He makes her laugh")
    r = Relationship(a=102, b=HUMAN, affinity=50.0, familiarity=70.0, notes=NOTE)
    far = relationship_card("Zan", r, is_human=True, far=True)
    near = relationship_card("Zan", r, is_human=True)
    assert "settle in person" not in far and "jokes land" in far and "settle in person" in near


def test_her_intentions_that_see_him_stay_out():
    plan = make_mem(text="Going to the gig on saturday to see if his jokes work in person", kind="plan")
    said = make_mem(text="I'll take you out for a drink one day", kind="reflection")
    told = make_mem(text="He said he never met anyone in person from the guild", kind="conversation")
    gig = make_mem(text="Going to the gig on saturday", kind="plan")
    assert _far_memories([plan, said, told, gig]) == [told, gig]


# -- the pipeline: his whisper, his DM and a guild line to him ----------------------------------------

@pytest.fixture
def clock():
    return engine_clock()


def _rig(store, clock, tmp_path, *, met=False, note=NOTE):
    seed_test_cast(store)
    save_member(store, 102, player_gender="female", typo_rate=0.0, split_rate=0.0,
                emoticon_rate=0.0, abbreviations={})
    store.put_relationship(Relationship(a=102, b=HUMAN, affinity=50.0, familiarity=70.0,
                                        attraction=60.0, notes=note,
                                        romance_stage="weekend" if met else "whispers"))
    return make_rig(store, clock, tmp_path)


TURNS = {
    "whisper": lambda: Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=HUMAN, addressee=HUMAN),
    "dm": lambda: Turn(TurnKind.REPLY_HUMAN, 102, "dm:102", to_guid=HUMAN, addressee=HUMAN),
    "guild": lambda: Turn(TurnKind.REPLY_HUMAN, 102, "guild", addressee=HUMAN),
}


def _speak(r, t, meta=None):
    meta = meta or TurnMeta(r.engine._new_id(), why="test", answers_human=True)

    async def go():
        task = asyncio.create_task(r.engine._speak(t, meta))
        await r.advance(100)
        return task.result()
    return r.sync(go())


@pytest.mark.parametrize("channel", list(TURNS))
def test_a_far_member_is_asked_again_without_seeing_him(store, clock, tmp_path, channel):
    r = _rig(store, clock, tmp_path)
    try:
        r.script(MEET, "saturday works, tell me how it sounds after")
        sent = _speak(r, TURNS[channel]())
        assert sent and "sit next to me" not in " ".join(sent)
        assert IN_PERSON_HINT.format(they="he", them="him", their="his") in r.last_user()
    finally:
        r.close()


@pytest.mark.parametrize("channel", list(TURNS))
def test_a_met_arc_may_see_him(store, clock, tmp_path, channel):
    r = _rig(store, clock, tmp_path, met=True)
    try:
        r.script(MEET, "unused")
        sent = _speak(r, TURNS[channel]())
        assert sent and len(r.chat_calls()) == 1
    finally:
        r.close()


@pytest.mark.parametrize("channel", ["whisper", "dm"])
def test_an_owed_answer_keeps_a_retry_that_still_sees_him(store, clock, tmp_path, channel):
    """Soft, never the summons: the retry stands, no fallback generation."""
    r = _rig(store, clock, tmp_path)
    try:
        r.script(MEET, "ha fine, i need u there saturday")
        meta = TurnMeta("owed", why="reply:named", answers_human=True,
                        whisper_reply=channel == "whisper")
        sent = _speak(r, TURNS[channel](), meta)
        assert sent and "need u there" in " ".join(sent) and len(r.chat_calls()) == 2
    finally:
        r.close()


@pytest.mark.parametrize("channel", list(TURNS))
@pytest.mark.parametrize("met", [False, True])
def test_her_note_and_plan_reach_her_prompt_only_once_met(store, clock, tmp_path, channel, met):
    r = _rig(store, clock, tmp_path, met=met)
    t = int(clock.now())
    text = "Going to the gig on saturday to see if his jokes work in person"
    store.insert_memory(make_mem(text=text, owner=102, kind="plan", t_event=t - 3600,
                                 t_created=t, t_accessed=t, actors=(HUMAN,),
                                 embedding=pack_vec(fake_embed(text))))
    try:
        messages, _ = r.sync(r.engine.compose(TURNS[channel]()))
        prompt = "\n".join(m["content"] for m in messages)
        assert ("settle in person" in prompt) is met
        assert ("his jokes work in person" in prompt) is met
    finally:
        r.close()


def test_the_era_retry_is_told_2009(store, clock, tmp_path):
    r = _rig(store, clock, tmp_path)
    try:
        r.script("we argued about the Molten Front, nothing more", "we argued about loot, nothing more")
        sent = _speak(r, TURNS["whisper"]())
        assert sent and "Molten" not in " ".join(sent) and ERA_HINT in r.last_user()
    finally:
        r.close()
