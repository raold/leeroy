"""The raid lead's service on an in-process app: designation (console, chat, default),
persistence and its end with the group, native's switch, orders carried out and acknowledged only
after the game's job, holds for damage dealers only and always lifted, plain refusals, fences, the
model fallback and its order with talk, and the callouts."""

import asyncio
import itertools
import json
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.guild.protocol import BotDo, RaidControl
from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.protocol import Boss, Chat, Death, Group, Hello
from leeroy_mind.raidlead import callouts as C
from leeroy_mind.raidlead.protocol import RaidLead
from leeroy_mind.raidlead.service import KEY, RaidLeadService, attach
from leeroy_mind.talk.turns import TurnKind

from .rig import CAST, HUMAN, LIVE, done, drain, make_app, ready

DAMAGE = {103, 104}  # the mage and the rogue; 101 and 105 tank, 102 heals


@pytest.fixture
def app(store, clock, tmp_path):
    return make_app(store, clock, tmp_path)


def said_by(app):
    return [(t.speaker, t.kind, t.instruction) for t in app.guild.source.turns]


def orders(app):
    return [(c.action, c.param, set(c.guids)) for c in app.guild.ports.sent]


def line(text, *, channel="raid", from_guid=HUMAN):
    return Chat(ts=0, channel=channel, from_guid=from_guid, from_name="Rowan", to_guid=0,
                text=text, map=631, zone=4812)


LINE_IDS = itertools.count(1000)


async def say(service, text, *, line_id=None, settle=True, **kw):
    """His line as the app hands it over: claimed (or not), then its chat event. Each call is a
    line of its own unless line_id says otherwise."""
    line_id = next(LINE_IDS) if line_id is None else line_id
    ev = line(text, **kw)
    claimed = service.claims(ev, line_id)
    await service.event(ev)
    if settle:
        await drain(service)
    return claimed


def led(app, guid, nights=1):
    for _ in range(nights):
        app.store.conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,"
                               "tank_guid,loot_guid,ended) VALUES (1,'icc',10,0,0,?,?,?,2)",
                               (guid, guid, guid))
    app.store.conn.commit()


def raid_lead_actions(app):
    return [c.action for c in app.bridge.sent if isinstance(c, RaidLead)]


# -- designation ---------------------------------------------------------------------------------

def test_default_lead_is_the_officer_with_most_nights_led_and_no_flag_is_set(app):
    async def main():
        led(app, 103, 2)
        led(app, 101, 1)
        service = await ready(app)
        await drain(service)
        assert (service.lead(), service.source) == (103, "default")
        assert app.raid.lead() == 103
        # never a raid assistant flag: only the switch's sync (or probe) and marks reach native
        assert set(raid_lead_actions(app)) <= {"sync", "probe", "mark"}
        assert said_by(app) == []  # the default is silent
    run(main())


def test_default_breaks_ties_by_attendance_then_lowest_guid(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert service.lead() == 101
    run(main())


def test_a_group_without_officers_has_no_default_lead(app):
    async def main():
        service = await ready(app, members=(102, 104, 105))
        await drain(service)
        assert service.lead() is None and app.raid._designated is None
    run(main())


def test_he_names_the_lead_in_chat_and_she_acknowledges(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert await say(service, "Coros, you're leading tonight") == "order"
        assert (service.lead(), service.source) == (103, "chat")
        [(speaker, kind, text)] = said_by(app)
        assert speaker == 103 and kind is TurnKind.REPLY_HUMAN and "asked you to lead" in text
        stored = json.loads(app.store.get_meta(KEY))
        assert stored["lead"] == 103 and stored["source"] == "chat"
    run(main())


def test_calling_it_a_night_never_changes_the_lead(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert not await say(service, "i'm calling it")
        assert service.lead() == 101 and not service.his
    run(main())


def test_he_takes_it_back_and_the_calls_go_back_to_him_at_once(app):
    """P2-5: in a raid he drives whose lead is the member he named, "I'll lead" makes him the
    talk's lead and the raid calls' speaker again."""
    from leeroy_mind.guild.ports import LivePorts

    async def main():
        service = await ready(app)
        await drain(service)
        run_ = SimpleNamespace(lead_is_human=True, plan=SimpleNamespace(lead_guid=101, members=list(CAST)))
        app.guild.raid_night.run = run_
        app.raid.set_lead(101)
        app.raidlead = service
        ports = LivePorts(app)
        assert ports.speaking_lead(run_) == 101
        assert await say(service, "I'll lead") == "order"
        assert service.lead() is None and service.his
        assert app.raid._designated is None and app.raid.lead() == HUMAN
        assert ports.speaking_lead(run_) == HUMAN  # RaidCalls see him: nobody calls
        await service.console_action({"action": "set", "guid": 104})
        assert ports.speaking_lead(run_) == 104 and app.raid.lead() == 104
        await service.console_action({"action": "set", "guid": HUMAN})
        assert ports.speaking_lead(run_) == HUMAN and app.raid.lead() == HUMAN
    run(main())


def test_console_sets_and_clears(app):
    async def main():
        service = await ready(app)
        await drain(service)
        status = service.console_status()
        assert [c["guid"] for c in status["candidates"]] == sorted(CAST)
        assert "assistant" not in status and status["native"] is True
        reply = await service.console_action({"action": "set", "guid": 104})
        assert reply["ok"] and service.lead() == 104 and service.source == "console"
        assert said_by(app)[-1][0] == 104
        bad = await service.console_action({"action": "set", "guid": 999})
        assert not bad["ok"] and service.lead() == 104
        reply = await service.console_action({"action": "set", "guid": HUMAN})
        assert reply["ok"] and service.lead() is None and service.his
        reply = await service.console_action({"action": "clear"})
        assert reply["ok"] and not service.his
        await drain(service)
        assert service.lead() == 101
    run(main())


def test_designation_persists_and_ends_with_the_group(app):
    async def main():
        service = await ready(app)
        await service.console_action({"action": "set", "guid": 104})
        again = RaidLeadService(app)
        assert (again.lead_guid, again.source, again.members) == (104, "console", frozenset(CAST))
        await service.event(Group(0, HUMAN, True, (HUMAN, 101, 102, 103, 105)))
        assert service.lead() is None and json.loads(app.store.get_meta(KEY))["lead"] is None
        await drain(service)
        assert service.lead() == 101
        await service.event(Group(0, 0, False, ()))
        assert service.lead() is None and service.members == frozenset()
        assert app.raid._designated is None
    run(main())


def test_orders_queued_for_a_group_that_broke_are_dropped(app):
    """P3-2: an order still queued when his group breaks never reaches the next group."""
    async def main():
        service = await ready(app)
        await drain(service)
        ev = line("everyone stack on me")
        service.claims(ev, 7)
        await service.event(ev)  # queued, not yet run
        await service.event(Group(0, 0, False, ()))
        await service.event(Group(0, HUMAN, True, (HUMAN, *CAST)))
        await drain(service)
        assert app.guild.ports.sent == []
    run(main())


def test_a_bad_stored_state_is_no_lead(app):
    app.store.set_meta(KEY, '{"lead": 999, "source": "chat", "members": [101], "his": true, "held": {"x": 1}}')
    service = RaidLeadService(app)
    assert service.lead_guid is None and service.members == frozenset({101}) and service.held == {}
    app.store.set_meta(KEY, "not json")
    assert RaidLeadService(app).lead_guid is None


def test_his_guild_group_counts_as_his_group(app):
    from leeroy_mind.guild.protocol import GuildGroup

    async def main():
        service = RaidLeadService(app)
        await service.connection(True)
        await service.event(Hello(0, "mod-leeroy", "1", 21, HUMAN, ("raidlead",)))
        await service.event(GuildGroup(0, "built", 25, HUMAN, True, 571, 0, 0, (), (101, 102)))
        assert service.members == frozenset({101, 102}) and service.raid
    run(main())


def test_hello_asks_for_his_group_and_the_switch(app):
    """P3-10: the sync at each hello has native publish his group, so the group is known at once."""
    async def main():
        service = RaidLeadService(app)
        await service.connection(True)
        await service.event(Hello(0, "mod-leeroy", "1", 21, HUMAN, ("raidlead",)))
        assert service.native is None and service.lead() is None
        await service._tick()
        assert raid_lead_actions(app) == ["sync"] and service.native is True
        await app.clock.advance(61)
        await service._tick()
        assert raid_lead_actions(app) == ["sync", "probe"]
    run(main())


# -- native's switch -----------------------------------------------------------------------------

def test_native_off_stops_everything(app):
    """P2-4: Leeroy.RaidLead.Enable = 0 answers raidlead_off: no lead, no talk lead, no calls."""
    async def main():
        service = RaidLeadService(app)
        await service.connection(True)
        await service.event(Hello(0, "mod-leeroy", "1", 21, HUMAN, ("raidlead",)))
        await service.event(Group(0, HUMAN, True, (HUMAN, *CAST)))
        app.bridge.acks.append((False, "raidlead_off"))
        await drain(service)
        assert service.native is False and service.blocked() == "native_off"
        assert service.lead() is None and app.raid._designated is None
        assert not await say(service, "everyone stack on me")
        assert app.guild.ports.sent == []
        await app.clock.advance(61)
        await drain(service)  # .reload config turned it on: the next probe sees it
        assert service.native is True and service.lead() == 101
    run(main())


def test_a_raidlead_off_refusal_turns_it_off(app):
    async def main():
        service = await ready(app)
        await drain(service)
        app.guild.ports.acks.append((False, "raidlead_off"))
        await say(service, "lust now")
        assert service.native is False and service.lead() is None
        assert said_by(app) == [] and app.guild.ports.said == []
    run(main())


def test_a_worldserver_without_the_feature_is_off(app):
    async def main():
        service = await ready(app)
        await service.event(Hello(0, "mod-leeroy", "1", 21, HUMAN, ("playstyle",)))
        await drain(service)
        assert service.lead() is None and service.blocked() == "unsupported"
    run(main())


# -- fences --------------------------------------------------------------------------------------

@pytest.mark.parametrize("fence", ["offline", "not_leader", "offline_raid", "mind_led", "forming",
                                   "member_line", "whisper", "private_room", "discord", "dm",
                                   "group_dm"])
def test_fences_keep_lines_as_talk(app, fence):
    async def main():
        service = await ready(app)
        await drain(service)
        kw = {}
        if fence == "offline":
            app.world.human_online = False
        elif fence == "not_leader":
            await service.event(Group(0, 101, True, (HUMAN, *CAST)))
        elif fence == "offline_raid":
            app.guild.offline.active = True
        elif fence == "mind_led":
            app.guild.raid_night.run = SimpleNamespace(lead_is_human=False)
        elif fence == "forming":
            app.guild.raid_night.state = SimpleNamespace(value="forming")
        elif fence == "member_line":
            kw = {"from_guid": 101}
        elif fence == "whisper":
            kw = {"channel": "whisper"}
        elif fence == "private_room":
            kw = {"channel": "voice:private1"}
        elif fence == "discord":
            kw = {"channel": "discord:general"}
        elif fence == "dm":
            kw = {"channel": "dm:101"}
        elif fence == "group_dm":
            kw = {"channel": "gdm:4"}
        before = list(app.guild.ports.sent)
        assert not await say(service, "everyone stack on me", **kw)
        assert app.guild.ports.sent == before
    run(main())


def test_claims_fail_open(app, monkeypatch):
    """P3-4: a failure inside claims is talk, never a lost line."""
    async def main():
        service = await ready(app)
        await drain(service)
        monkeypatch.setattr("leeroy_mind.raidlead.service.parse", lambda *a: 1 / 0)
        assert service.claims(line("everyone stack on me"), 7) is False
    run(main())


def test_a_mind_led_raid_takes_the_talk_lead_back(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert app.raid.lead() == 101
        app.guild.raid_night.run = SimpleNamespace(lead_is_human=False)
        await service._tick()
        assert app.raid._designated is None
    run(main())


def test_push_to_talk_in_a_shared_room_counts(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert await say(service, "Everyone, spread out.", channel="voice:raid1") == "order"
        assert orders(app) == [("order", "spread", set(CAST))]
        assert not await say(service, "Hold on.", channel="voice:raid1")
    run(main())


# -- orders --------------------------------------------------------------------------------------

def test_an_order_runs_on_his_group_and_is_acknowledged_after_the_job(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert await say(service, "everyone stack on me", line_id=42) == "order"
        [cmd] = app.guild.ports.sent
        assert isinstance(cmd, BotDo) and (cmd.action, cmd.param, cmd.target_guid) == ("order", "stack", 0)
        assert set(cmd.guids) == set(CAST)
        [(speaker, kind, text)] = said_by(app)
        assert speaker == 101 and kind is TurnKind.REPLY_HUMAN
        assert "stack on Rowan" in text and "Done, confirmed by the game" in text
        assert app.guild.source.turns[0].trigger_line == 42
    run(main())


def test_named_member_order_goes_to_her_and_she_answers(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "Dunna, run out")
        assert orders(app) == [("order", "run_out", {104})]
        assert said_by(app)[0][0] == 104
    run(main())


@pytest.mark.parametrize("text, guids", [
    ("tanks swap", {101, 105}), ("tank, taunt", {101, 105}), ("healers, spread out", {102}),
    ("melee back off", {104}), ("ranged: spread", {103}), ("dps hold", DAMAGE),
    ("everyone wait for my pull", DAMAGE), ("wait for my pull", DAMAGE), ("everyone back off", set(CAST)),
])
def test_addressees(app, text, guids):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, text)
        assert set(app.guild.ports.sent[0].guids) == guids
    run(main())


def test_a_tank_or_healer_never_holds(app):
    """P2-1: a hold is for damage dealers only; a named healer or the tanks are refused in words."""
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "Belra, hold")
        await say(service, "tanks, stop attacking")
        assert app.guild.ports.sent == []
        assert [s[2] for s in app.guild.ports.said] == ["can't do that one, tanks and healers don't hold"] * 2
    run(main())


def test_stack_on_a_named_member(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "stack on belra")
        assert app.guild.ports.sent[0].target_guid == 102
        assert "stack on Belra" in said_by(app)[0][2]
    run(main())


def test_partial_order_says_only_what_was_done(app):
    async def main():
        service = await ready(app)
        await drain(service)
        app.guild.ports.jobs.append(lambda cmd: done(cmd, skip=[(104, "dead")]))
        await say(service, "everyone follow me")
        text = said_by(app)[0][2]
        assert "Dunna" in text.split("Not for")[1] and "they're dead" in text
        assert "Dunna" not in text.split("Not for")[0]
    run(main())


def test_no_terminal_means_no_word(app):
    async def main():
        service = await ready(app)
        await drain(service)
        app.guild.ports.jobs.append(None)
        await say(service, "everyone follow me")
        assert said_by(app) == [] and app.guild.ports.said == []
    run(main())


def test_refused_order_is_a_plain_cant_do(app):
    async def main():
        service = await ready(app)
        await drain(service)
        app.guild.ports.jobs.append(lambda cmd: done(cmd, skip=[(g, "dead") for g in cmd.guids]))
        await say(service, "everyone follow me")
        assert said_by(app) == []
        [(speaker, channel, text, kind)] = app.guild.ports.said
        assert speaker == 101 and channel == "raid" and text == "can't do that one, they're dead"
    run(main())


def test_refused_at_the_ack_is_plain(app):
    async def main():
        service = await ready(app)
        await drain(service)
        app.guild.ports.acks.append((False, "busy"))
        await say(service, "everyone follow me")
        assert app.guild.ports.said[-1][2] == "can't do that one, they're busy" and said_by(app) == []
    run(main())


def test_lust_is_the_raid_leads_own_order_on_the_shaman(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "lust now")
        [cmd] = app.guild.ports.sent
        assert (cmd.action, cmd.param, cmd.guids) == ("order", "lust", (102,))
        assert "Bloodlust now" in said_by(app)[0][2]
    run(main())


def test_lust_never_falls_back_from_an_absent_addressee(app):
    """P3-1: an addressee he named who is not here is refused by name; a rogue cannot lust."""
    async def main():
        service = await ready(app)
        await drain(service)
        app.world.online.discard(102)
        await say(service, "belra, bl")
        await say(service, "Dunna, lust now")
        assert app.guild.ports.sent == []
        assert [s[2] for s in app.guild.ports.said] == [
            "can't do that one, Belra isn't here", C.refusal("no_shaman")]
    run(main())


def test_lust_without_a_shaman_is_refused(app):
    async def main():
        service = await ready(app, members=(101, 103, 104))
        await drain(service)
        await say(service, "lust now")
        assert app.guild.ports.sent == []
        assert app.guild.ports.said[-1][2] == C.refusal("no_shaman")
    run(main())


def test_kill_skull_marks_his_target_for_her_without_a_flag(app):
    async def main():
        service = await ready(app)
        await drain(service)
        app.bridge.sent.clear()
        await say(service, "kill skull")
        [mark] = app.bridge.sent
        assert (mark.action, mark.guid, mark.icon) == ("mark", 101, "skull")
        assert app.guild.ports.sent[0].param == "kill_skull"
        app.guild.ports.sent.clear()
        app.bridge.acks.append((False, "no_target"))
        await say(service, "kill skull")
        assert app.guild.ports.sent == []
        assert app.guild.ports.said[-1][2] == "can't do that one, you've got nothing hostile targeted"
    run(main())


def test_a_wipe_releases_nobody_and_adds_no_line(app):
    """P3-7: inside an instance the dead wait for a rez (GroupRez) unless he orders the release,
    and the wipe's one line is the talk's debrief."""
    async def main():
        service = await ready(app)
        await drain(service)
        await service.event(Death(0, 104, 631, 0))
        await service.event(Boss(0, 631, 77, 0, "", "wiped", (HUMAN, 101, 104)))
        await drain(service)
        assert app.guild.ports.sent == [] and said_by(app) == []
        await say(service, "release and run back")
        assert orders(app) == [("order", "release", set(CAST))]
    run(main())


# -- holds: lifted on his pull, by a later order, after 30 s; persisted; retried ------------------

def test_a_hold_is_lifted_when_his_group_engages(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "wait for my pull")
        assert set(service.held) == DAMAGE
        assert set(json.loads(app.store.get_meta(KEY))["held"]) == {str(g) for g in DAMAGE}
        app.guild.ports.sent.clear()
        await service.event(Boss(0, 631, 77, 0, "", "engaged", (HUMAN, 101)))
        await drain(service)
        assert orders(app) == [("order", "lift", DAMAGE)] and service.held == {}
    run(main())


def test_a_hold_is_lifted_after_thirty_seconds_and_a_failed_lift_is_retried(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "dps, hold")
        app.guild.ports.sent.clear()
        await app.clock.advance(31)
        app.guild.ports.jobs.append(lambda cmd: done(cmd, skip=[(104, "busy:training_state")]))
        await drain(service)
        assert orders(app) == [("order", "lift", DAMAGE)] and set(service.held) == {104}
        await app.clock.advance(6)
        await drain(service)
        assert orders(app)[-1] == ("order", "lift", {104}) and service.held == {}
    run(main())


def test_a_later_stay_or_stack_clears_the_hold(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "wait for my pull")
        await say(service, "stack on me")
        assert service.held == {}
        app.guild.ports.sent.clear()
        await service.event(Boss(0, 631, 77, 0, "", "engaged", (HUMAN, 101)))
        await drain(service)
        assert app.guild.ports.sent == []  # nothing drags them off the new spot
    run(main())


def test_holds_persist_and_are_lifted_at_the_next_hello(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "wait for my pull")
        again = RaidLeadService(app)
        assert set(again.held) == DAMAGE
        await again.connection(True)
        app.guild.ports.sent.clear()
        await again.event(Hello(0, "mod-leeroy", "1", 21, HUMAN, ("raidlead",)))
        await drain(again)
        assert orders(app)[0] == ("order", "lift", DAMAGE) and again.held == {}
    run(main())


# -- the model fallback --------------------------------------------------------------------------

def model_app(store, clock, tmp_path, answer, *, gate=None):
    from leeroy_mind.testing.fake_llm import FakeLLM
    llm = FakeLLM(lambda model, messages: json.dumps(answer), gate=gate)
    return make_app(store, clock, tmp_path, llm=llm)


def test_model_maps_an_unclear_order_onto_the_list(store, clock, tmp_path):
    app = model_app(store, clock, tmp_path, {"order": "stay", "who": "everyone", "member": "",
                                             "confidence": 0.9})

    async def main():
        service = await ready(app)
        await drain(service)
        assert await say(service, "everyone get behind the boss", line_id=12) == "held"
        [call] = app.llm.calls
        assert call["model"] == LIVE and call["think"] is False
        assert call["format"]["properties"]["order"]["enum"][-1] == "none"
        assert orders(app) == [("order", "stay", set(CAST))]
        assert app.signals == [(12, True)]
    run(main())


def test_model_keeps_his_addressee(store, clock, tmp_path):
    app = model_app(store, clock, tmp_path, {"order": "spread", "who": "everyone", "member": "",
                                             "confidence": 0.95})

    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "healers go stand apart")
        assert app.guild.ports.sent[0].guids == (102,)
    run(main())


def test_low_confidence_asks_one_question(store, clock, tmp_path):
    app = model_app(store, clock, tmp_path, {"order": "stay", "who": "everyone", "member": "",
                                             "confidence": 0.3})

    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "everyone get behind the boss")
        assert app.guild.ports.sent == []
        [(speaker, kind, text)] = said_by(app)
        assert speaker == 101 and "one short question" in text
    run(main())


def test_a_sure_none_goes_back_to_talk_and_signals_see_talk(store, clock, tmp_path):
    app = model_app(store, clock, tmp_path, {"order": "none", "who": "member", "member": "belra",
                                             "confidence": 0.9})

    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "Belra, go get some food", line_id=9)
        assert app.guild.ports.sent == [] and said_by(app) == [] and app.released == [9]
        assert app.signals == [(9, False)]  # never logged as a command
    run(main())


def test_a_held_line_goes_to_talk_before_his_next_line(store, clock, tmp_path):
    """P3-3: his next line never waits behind the model's reading of an earlier one, and they
    reach talk in the order he wrote them; the late reading changes nothing."""
    gate = asyncio.Event()
    app = model_app(store, clock, tmp_path, {"order": "stay", "who": "everyone", "member": "",
                                             "confidence": 0.9}, gate=gate)

    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "everyone get behind the boss", line_id=20, settle=False)
        await asyncio.sleep(0)
        assert service._pending and app.released == []
        await service.before_his_line()  # what the app does before his next line
        assert app.released == [20] and not service._pending
        gate.set()
        await drain(service)
        assert app.guild.ports.sent == [] and app.released == [20]
    run(main())


@pytest.mark.parametrize("why", ["unloaded", "paused", "gate"])
def test_no_loaded_model_means_talk(store, clock, tmp_path, why):
    app = model_app(store, clock, tmp_path, {"order": "stay", "who": "everyone", "member": "",
                                             "confidence": 0.9})

    async def main():
        service = await ready(app)
        await drain(service)
        if why == "unloaded":
            app.arbiter.loaded = ["another-model"]
        elif why == "paused":
            app.arbiter.paused = True
        else:
            class Gate:
                def check(self):
                    raise GpuPaused("paused")
            app.scheduler.gpu_gate = Gate()
        await say(service, "everyone get behind the boss", line_id=11)
        assert app.llm.calls == [] and app.guild.ports.sent == [] and app.released == [11]
    run(main())


# -- callouts ------------------------------------------------------------------------------------

def test_ready_check_is_his_and_she_tells_the_raid(app):
    async def main():
        service = await ready(app)
        await drain(service)
        app.bridge.sent.clear()
        assert await say(service, "rc") == "order"
        [rc] = app.bridge.sent
        assert isinstance(rc, RaidControl) and (rc.member_guid, rc.action, rc.leader_guid) == (HUMAN, "ready_check", HUMAN)
        assert said_by(app)[0][0] == 101 and "ready check is up" in said_by(app)[0][2]
    run(main())


def test_pull_countdown(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert await say(service, "pull in 3")
        await app.clock.advance(4)
        assert [s[:3] for s in app.guild.ports.said] == [(101, "raid", "pulling in 3"), (101, "raid", "go")]
        await service.close()
    run(main())


def test_plan_question_answers_from_facts(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await service.event(Boss(0, 631, 77, 0, "", "engaged", (HUMAN, 101)))
        await drain(service)
        app.world.members[HUMAN] = app.world.members[101].__class__(
            guid=HUMAN, name="Rowan", race=1, class_=8, gender=0, level=80, rank=0, online=True,
            map=631, zone=4812, spec_tab=0)
        assert await say(service, "what's the plan?")
        text = said_by(app)[-1][2]
        assert "marrowgar: spread out" in text and "2 tanks, 1 healers" in text
    run(main())


def test_boss_brief_is_the_playbook_hook_and_boss_plan_the_standing_call():
    context = C.BossContext(631, 0, "Lord Marrowgar", 25, False)
    assert C.boss_brief(context).startswith("Lord Marrowgar, 25 normal: ")
    assert C.boss_brief(C.BossContext(631, 0, "Lord Marrowgar")) is None  # size not known
    assert C.boss_plan(context).startswith("marrowgar")
    assert C.boss_plan(C.BossContext(1, 99)) is None


# -- off ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "[raidlead]\nenabled = false", '[raidlead]\nenabled = "true"',
                                  "[raidlead]\nenabled = true\nextra = 1", "[raidlead\n"])
def test_off_or_invalid_attaches_nothing(store, clock, tmp_path, text):
    app = make_app(store, clock, tmp_path)
    (tmp_path / "mind.toml").write_text(text, encoding="utf-8")
    registered = []
    app.register_event_handler = lambda *a, **k: registered.append(a)
    app.register_connection_hook = app.register_background = app.register_shutdown_drain = (
        lambda *a, **k: registered.append(a))
    before = store.conn.total_changes
    assert attach(app) is None
    assert registered == [] and store.conn.total_changes == before


def test_on_attaches_its_handlers(store, clock, tmp_path):
    app = make_app(store, clock, tmp_path)
    (tmp_path / "mind.toml").write_text("[raidlead]\nenabled = true\n", encoding="utf-8")
    events = []
    app.register_event_handler = lambda kind, handler, **k: events.append(kind)
    app.register_connection_hook = app.register_background = app.register_shutdown_drain = lambda h: None
    assert isinstance(attach(app), RaidLeadService)
    assert {"chat", "group", "guild_group", "boss", "death", "hello"} <= set(events)


# -- the second review's P3s ---------------------------------------------------------------------

def test_lift_retries_back_off_and_stop_while_the_bridge_is_down(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "dps, hold")
        app.guild.ports.sent.clear()
        stuck = lambda cmd: done(cmd, skip=[(104, "busy:training_state")])  # noqa: E731
        for _ in range(8):
            app.guild.ports.jobs.append(stuck)
        await app.clock.advance(31)
        await drain(service)
        tries = [app.clock.now()]
        for _ in range(140):
            await app.clock.advance(1)
            before = len(app.guild.ports.sent)
            await drain(service)
            if len(app.guild.ports.sent) > before:
                tries.append(app.clock.now())
        gaps = [round(b - a) for a, b in zip(tries, tries[1:])]
        assert gaps[:4] == [5, 10, 20, 40]
        assert set(service.held) == {104}
        await service.connection(False)
        sent = len(app.guild.ports.sent)
        await app.clock.advance(300)
        await drain(service)
        assert len(app.guild.ports.sent) == sent  # nothing while the bridge is down
        app.guild.ports.jobs.clear()  # the member is free again
        await service.connection(True)
        await service.event(Hello(0, "mod-leeroy", "1", 21, HUMAN, ("raidlead",)))
        await drain(service)
        assert app.guild.ports.sent[-1].param == "lift" and service.held == {}
    run(main())


def test_one_line_is_carried_out_once(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "release and run back", line_id=31)
        await say(service, "release and run back", line_id=31)
        assert orders(app) == [("order", "release", set(CAST))]
    run(main())


def test_members_playerbots_already_released_count_as_done(app):
    """His typed "release ..." reaches playerbots too: one already a ghost did what he asked, so
    there is no second "already released" line."""
    async def main():
        service = await ready(app)
        await drain(service)
        app.guild.ports.jobs.append(lambda cmd: done(cmd, skip=[(g, "released") for g in cmd.guids]))
        await say(service, "release and run back")
        assert app.guild.ports.said == []
        [(speaker, kind, text)] = said_by(app)
        assert speaker == 101 and "Done, confirmed by the game" in text and "Not for" not in text
    run(main())


def test_before_his_line_fails_open(app, monkeypatch):
    async def main():
        service = await ready(app)
        await drain(service)
        service._pending.append(SimpleNamespace(done=False))
        async def broken(**kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(service, "_release_pending", broken)
        await service.before_his_line()  # no exception: his line goes on as usual
    run(main())
