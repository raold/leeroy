"""His "you lead" carried out in a real MindApp (not started, a scripted wire for the bridge): his
line read, the member who leads chosen, one lead_do (or raid_control inside an instance), and her
answer's instruction telling what really happened."""
import asyncio
from types import SimpleNamespace

import pytest

from conftest import run
from selfies.fakes import OWNER, mind_app
from leeroy_mind.guild.protocol import Job, RaidControl
from leeroy_mind.lead import service as svc
from leeroy_mind.lead.protocol import LeadDo
from leeroy_mind.lead.service import native_code
from leeroy_mind.protocol import Ack, Chat, Group, Hello, Member
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

ORG = (1, 1637)        # Orgrimmar: map, zone
NAXX = (533, 3456)     # Naxxramas: map, zone
NAMES = {101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna", 105: "Ebbo"}
TANK = 103             # a protection warrior


def job(cmd, *, kind="lead_do", state="done", reason="leading", cmd_id=None):
    return Job(ts=0, id=cmd_id or cmd.id, kind=kind, state=state, reason=reason, stage="",
               guids=(), skipped=(), kills=0, wipes=0, instance_id=0, guid=getattr(cmd, "guid", 0),
               level=0, avg_ilvl=0)


class Wire:
    """The bridge as the service sees it: native's done job first, then the ack. answers: a
    function of the command giving (ok, error)."""

    def __init__(self, app):
        self.app, self.sent, self.connected = app, [], True
        self.answer = lambda cmd: (True, "")

    async def send(self, cmd, *, preserve_id=False):
        assert preserve_id
        self.sent.append(cmd)
        ok, error = self.answer(cmd)
        if ok and isinstance(cmd, LeadDo) and cmd.action == "start":
            self.app.leading.on_job(job(cmd))
        return Ack(0, cmd.id, ok, error)


@pytest.fixture
def app(tmp_path):
    app = mind_app(tmp_path, toml='[human]\npronouns = "he"\n')
    world = app.world
    world.human_guid, world.human_online, world.human_zone = OWNER, True, ORG[1]
    world.members = {g: Member(guid=g, name=n, race=2, class_=1, gender=1, level=80, rank=3,
                               online=True, map=ORG[0], zone=ORG[1],
                               spec_tab=2 if g == TANK else 0)
                     for g, n in NAMES.items()}
    world.online = set(NAMES)
    world.group = Group(ts=0, leader_guid=OWNER, raid=False, members=(OWNER, 101, 102, 103))
    app.hello = Hello(ts=0, server="mod-leeroy", version="0.1.0", guild_id=0, human_guid=OWNER,
                      features=("invites", "lead"), started_ms=5)
    app.leading._hello = 5
    real = app.bridge
    app.bridge = Wire(app)
    yield app
    app.bridge = real
    run(app.stop())


def chat(text, *, channel="party", to=0, at=ORG):
    return Chat(ts=0, channel=channel, from_guid=OWNER, from_name="Ownerly",
                to_guid=to if channel == "whisper" else 0, text=text, map=at[0], zone=at[1])


async def settle(app, seconds=0.0):
    for _ in range(3):
        await asyncio.sleep(0)
    if seconds:
        await app.clock.advance(seconds, 1.0)
    tasks = [t for t in app.leading._tasks if not t.done()]
    if tasks:
        await asyncio.wait(tasks, timeout=5)


def told(app, line, guid, channel="party"):
    turn = Turn(TurnKind.REPLY_HUMAN, guid, channel, trigger_line=line)
    return app.leading.instruction(turn, TurnMeta("t", why="reply:lead"), "")


def test_you_lead_in_party_gives_the_tank_the_lead(app):
    async def main():
        assert app.leading.claims(chat("you lead"), 501) == "lead"
        await settle(app)
        [cmd] = app.bridge.sent
        assert isinstance(cmd, LeadDo)
        assert (cmd.action, cmd.guid, cmd.activity, cmd.spot) == ("start", TANK, "quest", "")
        [rt] = app.leading.reply_turns(SimpleNamespace(id=501), "party")
        assert (rt.speaker, rt.kind, rt.reply_to, rt.to_guid) == (TANK, TurnKind.REPLY_HUMAN, "party", 0)
        assert app.leading.ready(501, TANK).is_set()
        said = told(app, 501, TANK)
        assert "you're leading his group out questing" in said and "following you" in said
        assert app.leading.active is not None and app.leading.active.guid == TANK
    run(main())


def test_the_member_he_names_or_whispers_leads(app):
    async def main():
        app.leading.claims(chat("Belra, take the lead"), 502)
        await settle(app)
        assert app.bridge.sent[-1].guid == 102
        app.leading.claims(chat("lead the way", channel="whisper", to=101), 503)
        await settle(app)
        assert app.bridge.sent[-1].guid == 101
        [rt] = app.leading.reply_turns(SimpleNamespace(id=503), "whisper")
        assert (rt.speaker, rt.to_guid) == (101, OWNER)
    run(main())


def test_a_named_member_outside_his_group_did_not_hear_it(app):
    async def main():
        assert app.leading.claims(chat("Dunna, take the lead"), 504) is None
        await settle(app)
        assert not app.bridge.sent
    run(main())


def test_a_whisper_to_a_member_outside_his_group_is_refused_in_her_words(app):
    async def main():
        assert app.leading.claims(chat("you lead", channel="whisper", to=104), 505) == "lead"
        await settle(app)
        assert not app.bridge.sent
        said = told(app, 505, 104, "whisper")
        assert "not in his group" in said and "don't say you're leading" in said
    run(main())


def test_grinding_and_travel(app):
    async def main():
        app.leading.claims(chat("go grind"), 506)
        await settle(app)
        assert app.bridge.sent[-1].activity == "grind"
        app.leading.claims(chat("lead us to the inn"), 507)
        await settle(app)
        cmd = app.bridge.sent[-1]
        assert (cmd.activity, cmd.spot) == ("travel", "org_inn")
        assert "to the inn in Orgrimmar" in told(app, 507, TANK)
    run(main())


def test_a_place_none_of_ours_moves_nobody_and_she_asks(app):
    async def main():
        assert app.leading.claims(chat("lead us to the cave"), 508) == "lead"
        await settle(app)
        assert not app.bridge.sent
        assert "Ask him where exactly" in told(app, 508, TANK)
    run(main())


def test_a_spot_on_another_continent_is_too_far_to_walk(app):
    async def main():
        app.leading.claims(chat("take us to the bank in dalaran"), 509)
        await settle(app)
        assert not app.bridge.sent
        assert "too far to walk" in told(app, 509, TANK)
    run(main())


def test_a_native_refusal_is_her_reason(app):
    async def main():
        app.bridge.answer = lambda cmd: (False, "combat")
        app.leading.claims(chat("you lead"), 510)
        await settle(app)
        said = told(app, 510, TANK)
        assert "middle of a fight" in said and "don't say you're leading" in said
        assert app.leading.active is None
    run(main())


def test_an_older_worldserver_is_never_asked(app):
    async def main():
        app.hello = Hello(ts=0, server="mod-leeroy", version="0.1.0", guild_id=0,
                          human_guid=OWNER, features=("invites",))
        app.leading.claims(chat("you lead"), 511)
        await settle(app)
        assert not app.bridge.sent
        assert "can't take the lead right now" in told(app, 511, TANK)
    run(main())


def test_another_time_is_not_the_leads(app):
    async def main():
        assert app.leading.claims(chat("Belra, you lead tonight"), 512) is None
        await settle(app)
        assert not app.bridge.sent
    run(main())


def test_the_member_he_names_also_calls_his_raid(app):
    calls = []
    app.raidlead = SimpleNamespace(designate=lambda guid: calls.append(guid) or True,
                                   lead=lambda: None)

    async def main():
        app.leading.claims(chat("Belra, you lead"), 531)
        await settle(app)
        assert calls == [102]
        app.leading.claims(chat("you lead"), 532)   # nobody named: the raid lead is unchanged
        await settle(app)
        assert calls == [102]
    run(main())


def test_inside_an_instance_dungeon_clear_takes_the_tank_and_she_calls_it(app):
    calls = []
    app.raidlead = SimpleNamespace(designate=lambda guid: calls.append(guid) or True,
                                   lead=lambda: None)

    def answer(cmd):
        if isinstance(cmd, RaidControl) and cmd.leader_guid != 101:
            return False, "leader_mismatch:101"   # dungeon-clear elects its own tank
        return True, ""

    async def main():
        app.bridge.answer = answer
        app.leading.claims(chat("Belra, you lead", at=NAXX), 513)
        await settle(app)
        sent = [c for c in app.bridge.sent if isinstance(c, RaidControl)]
        assert [(c.action, c.member_guid, c.leader_guid) for c in sent] == [
            ("dc_on", OWNER, TANK), ("dc_on", OWNER, 101)]
        assert calls == [102]
        said = told(app, 513, 102)
        assert "Arkon is leading his group from boss to boss" in said
        assert app.leading.dc is not None and app.leading.dc.tank == 101
        # his end: dungeon-clear off
        assert app.leading.claims(chat("i'll lead", at=NAXX), 514) == "lead"
        await settle(app)
        assert app.bridge.sent[-1].action == "dc_off" and app.leading.dc is None
        assert "taken the lead back" in told(app, 514, 102)
    run(main())


def test_no_tank_in_an_instance_is_said_plainly(app):
    async def main():
        app.bridge.answer = lambda cmd: (False, "leader_mismatch:0")
        app.leading.claims(chat("you lead", at=NAXX), 515)
        await settle(app)
        assert "no tank" in told(app, 515, TANK)
    run(main())


def test_his_end_only_while_a_lead_is_in_force(app):
    async def main():
        assert app.leading.claims(chat("stop"), 516) is None
        assert app.leading.claims(chat("i'll lead"), 517) is None   # the raid lead's, then
        app.leading.claims(chat("you lead"), 518)
        await settle(app)
        assert app.leading.claims(chat("stop leading"), 519) == "lead"
        await settle(app)
        cmd = app.bridge.sent[-1]
        assert (cmd.action, cmd.guid) == ("end", TANK)
        assert app.leading.active is None
        assert "taken the lead back" in told(app, 519, TANK)
    run(main())


def test_an_end_the_game_never_answered_is_not_said_as_done(app):
    async def main():
        app.leading.claims(chat("you lead"), 533)
        await settle(app)
        app.bridge.connected = False
        app.leading.claims(chat("stop leading"), 534)
        await settle(app)
        said = told(app, 534, TANK)
        assert "not sure yet" in said and "don't say you're following him" in said
        assert app.leading.active is not None   # still hers until native says it ended
    run(main())


def test_his_stop_before_the_start_is_confirmed_still_stops_it(app):
    gate = asyncio.Event()

    class Slow(Wire):
        async def send(self, cmd, *, preserve_id=False):
            if isinstance(cmd, LeadDo) and cmd.action == "start":
                await gate.wait()   # the start's ack is slow
            return await super().send(cmd, preserve_id=preserve_id)

    async def main():
        app.bridge = Slow(app)
        app.leading.claims(chat("you lead"), 542)
        await asyncio.sleep(0)
        assert app.leading.claims(chat("stop leading"), 543) == "lead"
        await asyncio.sleep(0)
        gate.set()
        await settle(app)
        assert [c.action for c in app.bridge.sent] == ["start", "end"]
        assert app.leading.active is None
        assert "taken the lead back" in told(app, 543, TANK)
    run(main())


def test_his_ill_lead_also_takes_the_raids_calling_back(app):
    calls = []
    app.raidlead = SimpleNamespace(designate=lambda guid: True, lead=lambda: None,
                                   take_back=lambda: calls.append("his") or True)

    async def main():
        app.leading.claims(chat("Belra, you lead"), 544)
        await settle(app)
        app.leading.claims(chat("stop leading"), 545)   # not his own lead: the calling stays
        await settle(app)
        assert calls == []
        app.leading.claims(chat("Belra, you lead"), 546)
        await settle(app)
        app.leading.claims(chat("i'll lead"), 547)
        await settle(app)
        assert calls == ["his"]
    run(main())


def test_follow_me_ends_it_and_still_is_playerbots(app):
    async def main():
        app.leading.claims(chat("you lead"), 520)
        await settle(app)
        assert app.leading.claims(chat("follow me"), 521) == "lead"
        await settle(app)
        assert app.bridge.sent[-1].action == "end"
    run(main())


def test_a_whisper_end_to_someone_else_is_talk(app):
    async def main():
        app.leading.claims(chat("you lead"), 522)
        await settle(app)
        assert app.leading.claims(chat("stop", channel="whisper", to=101), 523) is None
    run(main())


def test_native_ending_it_gets_one_short_line(app, monkeypatch):
    spoken = []

    async def speak(turn, *, why="speak", **kw):
        spoken.append((turn, why))
        return ["ok"]
    monkeypatch.setattr(app.engine, "speak", speak)

    async def main():
        app.leading.claims(chat("lead us to the inn"), 524)
        await settle(app)
        told(app, 524, TANK)   # her answer has been written: the end gets its own line
        active = app.leading.active
        app.leading.on_job(job(SimpleNamespace(id=active.id, guid=TANK), kind="lead",
                               state="ended", reason="arrived"))
        await settle(app)
        [(turn, why)] = spoken
        assert why == "source:lead" and turn.speaker == TANK and turn.channel == "party"
        assert "the inn in Orgrimmar" in turn.instruction and "stopped leading" in turn.instruction
        assert app.leading.active is None
        # a quiet end says nothing
        app.leading.claims(chat("you lead"), 525)
        await settle(app)
        told(app, 525, TANK)
        app.leading.on_job(job(SimpleNamespace(id=app.leading.active.id, guid=TANK), kind="lead",
                               state="ended", reason="group"))
        await settle(app)
        assert len(spoken) == 1
    run(main())


def test_going_into_an_instance_hands_it_to_dungeon_clear(app, monkeypatch):
    spoken = []

    async def speak(turn, *, why="speak", **kw):
        spoken.append(turn)
        return ["ok"]
    monkeypatch.setattr(app.engine, "speak", speak)

    async def main():
        app.leading.claims(chat("you lead"), 526)
        await settle(app)
        active = app.leading.active
        app.leading.on_job(job(SimpleNamespace(id=active.id, guid=TANK), kind="lead",
                               state="ended", reason="instance"))
        await settle(app, svc.HANDOVER_S + 1)
        cmd = app.bridge.sent[-1]
        assert isinstance(cmd, RaidControl) and (cmd.action, cmd.leader_guid) == ("dc_on", TANK)
        assert app.leading.dc is not None
        [turn] = spoken
        assert "boss to boss" in turn.instruction
    run(main())


def test_a_hello_clears_a_lead_an_earlier_mind_left(app):
    async def main():
        await app.leading.on_hello(app.hello)
        await settle(app)
        [cmd] = app.bridge.sent
        assert (cmd.action, cmd.guid) == ("end", 0)
        # a lead of ours in force survives a reconnect to the same worldserver, not a new one
        app.leading.claims(chat("you lead"), 527)
        await settle(app)
        await app.leading.on_hello(app.hello)
        assert app.leading.active is not None
        await app.leading.on_hello(Hello(ts=0, server="mod-leeroy", version="0.1.0", guild_id=0,
                                         human_guid=OWNER, features=("lead",), started_ms=9))
        assert app.leading.active is None
    run(main())


def test_switched_off_or_quiet_his_line_is_talk(app):
    async def main():
        app.cfg = app.cfg.__class__(**{**app.cfg.__dict__,
                                       "talk": app.cfg.talk.__class__(**{**app.cfg.talk.__dict__,
                                                                         "lead": False})})
        assert app.leading.claims(chat("you lead"), 528) is None
    run(main())


def test_a_raid_the_guild_leads_is_not_hers_to_lead(app):
    async def main():
        app.leading._raid_led = lambda: True
        app.leading.claims(chat("you lead"), 529)
        await settle(app)
        assert not app.bridge.sent
        assert "already being led" in told(app, 529, TANK)
    run(main())


def test_another_member_answering_is_told_he_asked_someone_else(app):
    async def main():
        app.leading.claims(chat("you lead"), 530)
        await settle(app)
        said = told(app, 530, 101)
        assert "asked Coros to take the lead, not you" in said
    run(main())


def test_native_codes():
    assert native_code("unknown type: lead_do") == "unsupported"
    assert native_code("bad field: spot") == "unsupported"
    assert native_code("busy:training_owner") == "busy:training_owner"
    assert native_code("a long sentence") == "refused"


def test_a_wrath_dungeon_the_boss_tables_leave_out_is_dungeon_clears(app):
    async def main():
        app.leading.claims(chat("you lead", at=(575, 1196)), 535)   # Utgarde Pinnacle
        await settle(app)
        [cmd] = app.bridge.sent
        assert isinstance(cmd, RaidControl) and cmd.action == "dc_on"
    run(main())


def test_his_end_cancels_a_handover_still_waiting(app, monkeypatch):
    async def speak(turn, *, why="speak", **kw):
        return ["ok"]
    monkeypatch.setattr(app.engine, "speak", speak)

    async def main():
        app.leading.claims(chat("you lead"), 536)
        await settle(app)
        active = app.leading.active
        app.leading.on_job(job(SimpleNamespace(id=active.id, guid=TANK), kind="lead",
                               state="ended", reason="instance"))
        await asyncio.sleep(0)
        assert app.leading.claims(chat("i'll lead", at=NAXX), 537) == "lead"
        await settle(app, svc.HANDOVER_S + 1)
        assert not [c for c in app.bridge.sent if isinstance(c, RaidControl)]
        assert app.leading.dc is None and app.leading.handing is None
        assert "taken the lead back" in told(app, 537, TANK)
    run(main())


def test_dungeon_clear_stays_his_to_stop_until_it_stopped(app):
    async def main():
        app.leading.claims(chat("you lead", at=NAXX), 538)
        await settle(app)
        assert app.leading.dc is not None
        app.bridge.connected = False
        app.leading.claims(chat("stop leading", at=NAXX), 539)
        await settle(app)
        assert app.leading.dc is not None and "not sure yet" in told(app, 539, TANK)
        app.bridge.connected = True
        app.leading.claims(chat("stop leading", at=NAXX), 540)
        await settle(app)
        assert app.bridge.sent[-1].action == "dc_off" and app.leading.dc is None
    run(main())


def test_an_end_native_sent_before_the_starts_ack_is_kept(app, monkeypatch):
    spoken = []

    async def speak(turn, *, why="speak", **kw):
        spoken.append(turn)
        return ["ok"]
    monkeypatch.setattr(app.engine, "speak", speak)

    def answer(cmd):
        # native's done job, then (in the same batch) her death, then the ack
        app.leading.on_job(job(cmd))
        app.leading.on_job(job(cmd, kind="lead", state="ended", reason="dead"))
        return True, ""

    class Early(Wire):
        async def send(self, cmd, *, preserve_id=False):
            self.sent.append(cmd)
            ok, error = answer(cmd) if isinstance(cmd, LeadDo) else (True, "")
            return Ack(0, cmd.id, ok, error)

    async def main():
        app.bridge = Early(app)
        app.leading.claims(chat("you lead"), 541)
        await settle(app)
        assert app.leading.active is None
        assert not spoken   # her answer, not yet told, says it: no second line
        said = told(app, 541, TANK)
        assert "it's over already: you died" in said and "don't say you're leading" in said
        # once told, a later end gets its own line
        app.bridge = Wire(app)
        app.leading.claims(chat("you lead"), 548)
        await settle(app)
        told(app, 548, TANK)
        app.leading.on_job(job(SimpleNamespace(id=app.leading.active.id, guid=TANK), kind="lead",
                               state="ended", reason="dead"))
        await settle(app)
        [turn] = spoken
        assert "died while leading" in turn.instruction
    run(main())


def test_an_offline_tank_is_never_dungeon_clears_target(app):
    app.world.online.discard(TANK)

    async def main():
        app.leading.claims(chat("Belra, you lead", at=NAXX), 549)
        await settle(app)
        [cmd] = [c for c in app.bridge.sent if isinstance(c, RaidControl)]
        assert cmd.leader_guid == 102   # no tank online: hers, and native names its own
    run(main())


def test_his_end_while_dungeon_clear_is_being_asked_stops_it_again(app, monkeypatch):
    async def speak(turn, *, why="speak", **kw):
        return ["ok"]
    monkeypatch.setattr(app.engine, "speak", speak)
    gate = asyncio.Event()

    class Slow(Wire):
        async def send(self, cmd, *, preserve_id=False):
            if isinstance(cmd, RaidControl) and cmd.action == "dc_on":
                self.asked.append(cmd.action)
                await gate.wait()
            return await super().send(cmd, preserve_id=preserve_id)

    async def main():
        Slow.asked = []
        app.leading.claims(chat("you lead"), 550)
        await settle(app)
        app.bridge = Slow(app)
        active = app.leading.active
        app.leading.on_job(job(SimpleNamespace(id=active.id, guid=TANK), kind="lead",
                               state="ended", reason="instance"))
        await app.clock.advance(svc.HANDOVER_S + 1, 1.0)
        for _ in range(5):
            await asyncio.sleep(0)
        assert Slow.asked == ["dc_on"] and app.leading.handing is not None
        assert app.leading.claims(chat("stop leading", at=NAXX), 551) == "lead"
        gate.set()
        await settle(app)
        assert [c.action for c in app.bridge.sent if isinstance(c, RaidControl)] == ["dc_on", "dc_off"]
        assert app.leading.dc is None
    run(main())
