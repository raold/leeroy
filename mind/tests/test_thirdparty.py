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
from leeroy_mind.memory.thirdparty import for_him
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.testing.fake_clock import FakeClock
from test_sofar import his_dm, section

HE = ("he", "him", "his")


@pytest.fixture
def clock():
    return FakeClock(datetime(2026, 10, 29, 20, tzinfo=ZoneInfo("America/New_York")), yields=64)


def mem(text, actors=(), scope_with=0, i=1):
    return MemoryRow(id=i, owner=102, kind="event", text=text, t_event=0, t_created=0,
                     t_accessed=0, access_n=0, actors=tuple(actors), place=None, boss_id=None,
                     raid_id=None, importance=5, valence=0.0, source="offscreen", evidence=(),
                     scope_with=scope_with)


def shown(memories, forms=HE):
    out = for_him(memories, owner=102, hg=1, forms=forms, his_names=["Zan", "Quillon"],
                  names={1: "Zan", 102: "Belra", 103: "Mirko"})
    return [m.text for m in out]


def test_a_memory_about_somebody_else_names_them_or_stays_out():
    assert shown([mem("I deleted the app that tracked his gym hours")]) == []  # nobody named
    assert shown([mem("My ex kept his key for a month")]) == ["My ex kept his key for a month"]
    assert shown([mem("I finally blocked his number. My ex can find someone else to call")]) == [
        "I finally blocked my ex's number. My ex can find someone else to call"]
    assert shown([mem("He wiped the raid again and blamed lag", actors=(102, 103))]) == [
        "Mirko wiped the raid again and blamed lag"]
    assert shown([mem("I laughed at his joke about the tank", actors=(103,))]) == [
        "I laughed at Mirko's joke about the tank"]


def test_memories_about_him_are_shown_as_written():
    about = [mem("He told me about his new job", actors=(1, 102)),
             mem("He said he would be late on raid night", scope_with=1),
             mem("Zan said his addons broke after the patch"),
             mem("I want to raid more this week")]  # no pronoun at all
    assert shown(about) == [m.text for m in about]


def test_they_forms_and_her_forms():
    m = [mem("I deleted the app that tracked his gym hours")]
    assert shown(m, forms=("they", "them", "their")) == [m[0].text]  # no evidence either way
    assert shown([mem("I sent her the recipe")], forms=("she", "her", "her")) == []
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


def test_her_dm_and_whisper_prompts_leave_the_unnamed_he_out(store, clock, tmp_path):
    """Through the pipeline: recall returns both memories; the private prompt shows the one
    about him and not the one whose "his" is nobody's, and a guild turn still shows both."""
    rig = rig_for(store, clock, tmp_path, script=numbered("mm {n}, go on then"))
    other = mem("I deleted the app that tracked his gym hours", i=11)
    him = mem("He told me the gym near his flat closed", actors=(1, 102), i=12)

    async def recall(*a, **kw):
        return [other, him]
    rig.engine.recall.recall = recall
    talk = talk_for(rig)
    mid = his_dm(rig, "did you go to the gym today")
    answer(rig, talk, mid)
    remembered = section(rig.prompts()[-1], "You remember:")
    assert "gym near his flat" in remembered and "tracked his gym hours" not in remembered

    async def main():
        rig.world.human_online = True
        await rig.session()
        await rig.advance(120)
        before = len(rig.prompts())
        await rig.human("did you go to the gym today", channel="whisper", to_guid=102)
        await rig.advance(60)
        whisper = section(rig.prompts()[before], "You remember:")
        assert "gym near his flat" in whisper and "tracked his gym hours" not in whisper
        before = len(rig.prompts())
        await rig.human("anyone go to the gym today", channel="guild")
        await rig.advance(60)
        assert any("tracked his gym hours" in section(p, "You remember:")
                   for p in rig.prompts()[before:])  # every other turn shows it as before
    asyncio.run(main())
    rig.close()
