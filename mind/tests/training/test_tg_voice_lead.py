import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.training.cards import Card
from leeroy_mind.training.protocol import BossCast
from leeroy_mind.training.voice_lead import CALLOUTS, HUMAN_ASSIGNMENTS, VoiceLead, briefing, voicecheck
from leeroy_mind.clock import night_of

from .test_tg_gate import _run_async, boss, run_for


def rig(fake):
    run = run_for(fake, offline=False)
    fake.online = True
    fake.say = AsyncMock(return_value=True)
    fake.on_comms = lambda guid: True
    ground = SimpleNamespace(listener=SimpleNamespace(run=run),
        gate=SimpleNamespace(recording=lambda: True), book=lambda: SimpleNamespace(name=lambda id: "Defile"))
    return VoiceLead(fake, ground), run


def cast(fake, start, *, spell=1, target=1, pull=None, **changes):
    return replace(BossCast(int(fake.clock.now()*1000), pull or f"{start}-100-12",
                            631, 100, 36597, 999, spell, target, "start", 1000), **changes)


def test_briefing_includes_facts_assignment_and_sentence_bound():
    c = Card(None, "631:12:0", 1, "champion", "stock", (), call="spread for defile")
    assert briefing(c, "spread was too tight", "valkyrs", boss="Lich King", human="Examplecaller") == (
        "Lich King next. plan: spread for defile. last time: spread was too tight. Examplecaller, valkyrs.")
    text = briefing(replace(c, call="word "*100), "long "*100, "adds", boss="Boss", human="Caller")
    assert len(text) <= 255 and text.endswith(".")
    assert HUMAN_ASSIGNMENTS[(631, 12)]["ranged"] == "valkyrs"
    assert any(c.spell == "Defile" for c in CALLOUTS[(631, 12)])


@_run_async
async def test_cast_is_queued_without_inline_speech_and_targets_called_name(fake):
    voice, run = rig(fake)
    engage = boss(fake, 12)
    await voice.on_boss(engage)
    await fake.clock.advance(.01, step=.01)
    await voice.on_cast(cast(fake, engage.ts + 1))
    fake.say.assert_not_awaited()
    await voice.tick()
    fake.say.assert_awaited_once_with(101, "raid", "defile on Testy, move", kind=TurnKind.URGENT)


@pytest.mark.parametrize("bad", ["offline", "human_lead", "wrong_instance", "old_pull", "gate", "unknown_target", "ended"])
@_run_async
async def test_callout_rejects_wrong_or_unknown_fight(fake, bad):
    voice, run = rig(fake)
    ev = boss(fake, 12)
    await voice.on_boss(ev)
    event = cast(fake, ev.ts)
    if bad == "offline":
        voice.ground.listener.run = replace(run, human_in_raid=False)
    elif bad == "human_lead":
        voice.ground.listener.run = replace(run, lead_is_human=True)
    elif bad == "wrong_instance":
        event = replace(event, instance_id=101)
    elif bad == "old_pull":
        event = replace(event, pull=f"{ev.ts-1}-100-12")
    elif bad == "gate":
        voice.ground.gate.recording = lambda: False
    elif bad == "unknown_target":
        event = replace(event, target=0)
    else:
        await voice.on_boss(replace(ev, state="wiped"))
    await voice.on_cast(event)
    await voice.tick()
    fake.say.assert_not_awaited()


@_run_async
async def test_per_spell_and_minute_limits_survive_new_pulls(fake):
    voice, run = rig(fake)
    start = boss(fake, 12)
    await voice.on_boss(start)
    for _ in range(6):
        await voice.on_cast(cast(fake, start.ts))
        await voice.tick()
        await fake.clock.advance(9, step=9)
    assert fake.say.await_count == 4
    await voice.on_boss(boss(fake, 12, "wiped"))
    start = boss(fake, 12)
    await voice.on_boss(start)
    await voice.on_cast(cast(fake, start.ts))
    await voice.tick()
    assert fake.say.await_count == 4


@_run_async
async def test_queue_drops_on_wipe_or_connection_change(fake):
    for why in ("wipe", "connection", "closing"):
        voice, run = rig(fake)
        event = boss(fake, 12)
        await voice.on_boss(event)
        await voice.on_cast(cast(fake, event.ts))
        if why == "wipe":
            await voice.on_boss(replace(event, state="wiped"))
        elif why == "connection":
            fake.connection_epoch = getattr(fake, "connection_epoch", 0) + 1
        else:
            run.session._closing = True
        await voice.tick()
        fake.say.assert_not_awaited()


@_run_async
async def test_refused_guard_is_not_retried(fake):
    voice, run = rig(fake)
    fake.say.return_value = False
    event = boss(fake, 12)
    await voice.on_boss(event)
    await voice.on_cast(cast(fake, event.ts))
    await voice.tick()
    await voice.on_cast(cast(fake, event.ts))
    await voice.tick()
    assert fake.say.await_count == 1


@_run_async
async def test_close_cancels_owned_speech(fake):
    voice, run = rig(fake)
    entered = asyncio.Event()
    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    fake.say.side_effect = blocked
    event = boss(fake, 12)
    await voice.on_boss(event)
    await voice.on_cast(cast(fake, event.ts))
    task = asyncio.create_task(voice.tick())
    await entered.wait()
    await voice.close()
    await task
    assert voice._speech is None


def test_pre_call_uses_current_human_role_and_native_comms(fake):
    voice, run = rig(fake)
    human = SimpleNamespace(online=True, map=631, class_=8, spec_tab=2)
    voice.ground.app = SimpleNamespace(world=SimpleNamespace(members={1: human}))
    line = voice.pre_call(631, 12, call="spread for defile")
    assert line.startswith("the Lich King next. plan: spread for defile.")
    assert "Testy, valkyrs." in line
    fake.on_comms = lambda guid: False
    assert voice.pre_call(631, 12, call="spread for defile") == "spread for defile. Testy: valkyrs."
    voice.ground.listener.run = replace(run, lead_is_human=True)
    assert voice.pre_call(631, 12) is None


def test_pre_call_has_no_unproved_assignment_without_human_role(fake):
    voice, run = rig(fake)
    line = voice.pre_call(631, 12, call="spread for defile")
    assert "valkyrs" not in line
    fake.on_comms = lambda guid: False
    assert voice.pre_call(631, 12, call="spread for defile") is None


def test_voicecheck_requires_delivered_utterance_for_pre_call(fake):
    voice, run = rig(fake)
    line = voice.pre_call(631, 12, call="spread for defile")
    night = night_of(fake.clock.local())
    report = voicecheck(fake.conn, night)
    assert report["briefings"] == 0 and report["expected_briefings"] == 1
    with fake.conn:
        fake.conn.execute("INSERT INTO utterance(ts,guid,channel,kind,text) VALUES (?,?,?,?,?)",
                          (int(fake.clock.now()),101,"raid","callout",line))
    report = voicecheck(fake.conn, night)
    assert report["briefings"] == 1 and report["acceptance"] == "unverified"
    assert report["non_lead_raid_lines_in_combat"] is None


@_run_async
async def test_voicecheck_counts_recorded_combat_and_actual_lead_reactions(fake):
    voice, run = rig(fake)
    start = boss(fake, 12)
    await voice.on_boss(start)
    await fake.clock.advance(20, step=20)
    await voice.on_boss(boss(fake, 12, "wiped"))
    with fake.conn:
        fake.conn.executemany("INSERT INTO utterance(ts,guid,channel,kind,text,why,dropped) VALUES (?,?,?,?,?,?,?)", [
            (start.ts/1000+2, 102, "raid", "urgent", "move", "", None),
            (start.ts/1000+3, 101, "raid", "urgent", "move", "", None),
            (start.ts/1000+4, 102, "raid", "reply", "dropped", "", "guards"),
            (start.ts/1000+5, 102, "guild", "reply", "elsewhere", "", None),
            (start.ts/1000+31, 101, "raid", "callout", "next pull", "wipe:lead", None),
            (start.ts/1000+32, 102, "raid", "callout", "someone else", "wipe:lead", None),
        ])
    await fake.clock.advance(40, step=40)
    await voice.on_boss(boss(fake, 12))
    await fake.clock.advance(20, step=20)
    await voice.on_boss(boss(fake, 12, "killed"))
    with fake.conn:
        fake.conn.execute("INSERT INTO utterance(ts,guid,channel,kind,text,why) VALUES (?,?,?,?,?,?)",
                          (fake.clock.now()+2, 101, "raid", "kill", "got him", "kill:lead"))
    report = voicecheck(fake.conn, night_of(fake.clock.local()))
    assert report["non_lead_raid_lines_in_combat"] == 1
    assert report["debriefs"] == report["kills"] == 1
    assert report["combat_coverage"] == "complete_recorded_intervals"
    assert report["fact_delivery"] == report["audible_delivery"] == report["acceptance"] == "unverified"


@_run_async
async def test_voicecheck_does_not_claim_quiet_if_a_terminal_was_lost(fake):
    voice, run = rig(fake)
    await voice.on_boss(boss(fake, 12))
    await fake.clock.advance(10, step=10)
    await voice.on_boss(boss(fake, 12, "killed"))
    await fake.clock.advance(10, step=10)
    await voice.on_boss(boss(fake, 12))
    voice.invalidate(run)
    report = voicecheck(fake.conn, night_of(fake.clock.local()))
    assert report["combat_coverage"] == "partial"
    assert report["non_lead_raid_lines_in_combat"] is None


@_run_async
async def test_new_connection_terminal_does_not_close_previous_fight_audit(fake):
    voice, run = rig(fake)
    await voice.on_boss(boss(fake, 12))
    fake.connection_epoch = getattr(fake, "connection_epoch", 0) + 1
    await fake.clock.advance(10, step=10)
    await voice.on_boss(boss(fake, 12, "killed"))
    report = voicecheck(fake.conn, night_of(fake.clock.local()))
    assert report["kills"] is None and report["non_lead_raid_lines_in_combat"] is None


@_run_async
async def test_callout_passes_through_real_pipeline_six_turn_guard(fake, store, clock, tmp_path):
    from test_engine_events import raid_rig
    engine = await raid_rig(store, clock, tmp_path)
    voice, run = rig(fake)
    voice.ground.listener.run = replace(run, plan=replace(run.plan, lead_guid=102))
    fake.say = engine.engine.say_as
    event = boss(fake, 12)
    await engine.event(event)
    await voice.on_boss(event)
    for i in range(6):
        engine.engine.guards.record(f"existing-{i}", 100+i, clock.now(), TurnKind.REPLY_HUMAN)
    await voice.on_cast(cast(fake, event.ts))
    task = asyncio.create_task(voice.tick())
    await clock.advance(2, step=.1)
    await task
    assert engine.sent.says() == []
    dropped = [dict(row) for row in engine.utterances() if row["kind"] == "urgent"]
    assert len(dropped) == 1 and dropped[0]["dropped"] == "guards"


@pytest.mark.live
def test_callout_names_resolve_in_installed_dbc():
    import os
    from pathlib import Path
    from leeroy_mind.training.spells import load_spellbook
    path = os.environ.get("LEEROY_TEST_DBC_DIR")
    if path is None:
        pytest.skip("Set LEEROY_TEST_DBC_DIR for explicit read-only spell-name verification")
    book = load_spellbook(Path(path))
    assert not sorted({c.spell for calls in CALLOUTS.values() for c in calls if not book.ids(c.spell)})
