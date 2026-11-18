"""Whose "he" a recalled memory means in a private turn with him (memory/thirdparty.py; self-play
audit 2026-11-17: a memory of her ex read as the owner, the only "he" in the prompt). Invented
memories only."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import HUMAN
from discord.test_dm_prompt import answer, rig_for, talk_for
from engine_rig import numbered
from leeroy_mind.memory.thirdparty import for_him, household_man
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.testing.fake_clock import FakeClock
from test_sofar import his_dm, section

HE = ("he", "him", "his")


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)


def mem(text, actors=(), scope_with=0, i=1, source="offscreen", kind="event"):
    return MemoryRow(id=i, owner=102, kind=kind, text=text, t_event=0, t_created=0,
                     t_accessed=0, access_n=0, actors=tuple(actors), place=None, boss_id=None,
                     raid_id=None, importance=5, valence=0.0, source=source, evidence=(),
                     scope_with=scope_with)


def her(household="", life_thread=""):
    return SimpleNamespace(real_life=SimpleNamespace(household=household,
                                                     life_thread=life_thread))


def shown(memories, forms=HE, persona=None):
    out = for_him(memories, owner=102, hg=1, forms=forms, his_names=["Zan", "Quillon"],
                  names={1: "Zan", 102: "Belra", 103: "Mirko"}, persona=persona)
    assert len(out) == len(memories)  # never left out (review of #368, P2-3)
    return [m.text for m in out]


def test_another_man_is_named_only_on_positive_evidence():
    gym = "I deleted the app that tracked his gym hours"
    # her own day, and her household has a man in it: he is the one her day is about
    assert shown([mem(gym)], persona=her("lives with her two kids; her ex-husband has them "
                                         "at weekends")) == [
        "I deleted the app that tracked my ex-husband's gym hours"]
    assert shown([mem(gym)], persona=her("married, husband works nights")) == [
        "I deleted the app that tracked my husband's gym hours"]
    assert household_man(her("lives alone with a cat")) == ""
    assert shown([mem(gym)], persona=her("lives alone with a cat")) == [gym]  # no evidence
    assert shown([mem(gym)]) == [gym]
    # the text itself names the role, before or after the pronoun
    assert shown([mem("My ex kept his key for a month")]) == ["My ex kept his key for a month"]
    assert shown([mem("I finally blocked his number. My ex can find someone else to call")]) == [
        "I finally blocked my ex's number. My ex can find someone else to call"]
    # another member is its actor
    assert shown([mem("He wiped the raid again and blamed lag", actors=(102, 103))]) == [
        "Mirko wiped the raid again and blamed lag"]
    assert shown([mem("I laughed at his joke about the tank", actors=(103,))]) == [
        "I laughed at Mirko's joke about the tank"]


def test_reflections_plans_and_game_memories_are_never_touched_without_evidence():
    """Review of #368, P2-3: her night's reflections say "he" about him with no actor stored;
    an achievement's title can hold "Him"; none of these is a third party."""
    married = her("married, husband works nights, one son")
    keep = [mem("He went quiet when I asked about the weekend and I keep turning it over",
                source="reflection", kind="reflection"),
            mem("Ask him whether he wants the off-tank spot before thursday", source="plan",
                kind="plan"),
            mem("Earned the achievement: Not One Of Us Fears Him", source="game"),
            mem("He told me about his new job", actors=(1, 102)),
            mem("He said he would be late on raid night", scope_with=1, source="reflection"),
            mem("Zan said his addons broke after the patch"),
            mem("I want to raid more this week")]
    assert shown(keep, persona=married) == [m.text for m in keep]


def test_they_forms_and_her_forms():
    m = [mem("I deleted the app that tracked his gym hours")]
    wife = her("lives with her wife and a dog")
    assert shown(m, forms=("they", "them", "their"), persona=wife) == [m[0].text]
    assert shown([mem("She forgot the recipe again")], forms=("she", "her", "her"),
                 persona=wife) == ["my wife forgot the recipe again"]
    assert shown([mem("I sent her the recipe")]) == ["I sent her the recipe"]  # he is "he"
    assert for_him(m, owner=102, hg=0, forms=HE, his_names=[], names={}) == m


def add_memory(rig, text, actors=()):
    from leeroy_mind.storage.rows import pack_vec  # noqa: F401
    now = int(rig.clock.now())
    row = replace_row(mem(text, actors=actors, i=None), now)
    return rig.store.insert_memory(row)


def replace_row(row, now):
    from dataclasses import replace
    return replace(row, t_event=now, t_created=now, t_accessed=now)


def test_her_dm_and_whisper_prompts_name_the_man_of_her_own_day(store, clock, tmp_path):
    """Through the pipeline: in her DM and her whisper with him the memory of her own day
    names her ex-husband, the memory about him is as written, and a guild turn shows the
    stored words."""
    from engine_rig import save_member
    rig = rig_for(store, clock, tmp_path, script=numbered("mm {n}, go on then"))
    save_member(store, 102, player_gender="female", region="US",
                household="lives with her two kids; her ex-husband has them at weekends")
    rig.engine.personas.invalidate(102)
    other = mem("I deleted the app that tracked his gym hours", i=11)
    him = mem("He told me the gym near his flat closed", actors=(1, 102), i=12)

    async def recall(*a, **kw):
        return [other, him]
    rig.engine.recall.recall = recall
    talk = talk_for(rig)
    mid = his_dm(rig, "did you go to the gym today")
    answer(rig, talk, mid)
    remembered = section(rig.prompts()[-1], "You remember:")
    assert "gym near his flat" in remembered
    assert "tracked my ex-husband's gym hours" in remembered and "his gym hours" not in remembered

    async def main():
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)
        before = len(rig.prompts())
        await rig.human("did you go to the gym today", channel="whisper", to_guid=102)
        await rig.advance(60)
        whisper = section(rig.prompts()[before], "You remember:")
        assert "gym near his flat" in whisper and "my ex-husband's gym hours" in whisper
        before = len(rig.prompts())
        await rig.human("anyone go to the gym today", channel="guild")
        await rig.advance(60)
        assert any("tracked his gym hours" in section(p, "You remember:")
                   for p in rig.prompts()[before:])  # every other turn shows it as stored
    asyncio.run(main())
    rig.close()
