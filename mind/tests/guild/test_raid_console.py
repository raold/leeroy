"""The raid night's console actions (leeroy_mind.endpoint's raid ops): each enters RaidNight
where the owner's chat or one of its timers would, with the same guards, and is refused, with
a reason in true words, whenever guild life itself would not act. Fake ports only."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import random

import pytest

from leeroy_mind.guild import raid_night as raid_night_module
from leeroy_mind.guild.config import GuildConfig
from leeroy_mind.guild.pick import Lead, Pick
from leeroy_mind.guild.protocol import ClearLockout, FormRaid, RaidControl, SetLootMaster, Summon
from leeroy_mind.guild.raid_night import CONSOLE_ACTIONS, ConsoleRefused, RaidNight, State
from leeroy_mind.guild.service import install
from leeroy_mind.guild.store import new_raid, record_attendance
from leeroy_mind.protocol import Login, Logout
from leeroy_mind.testing.fake_clock import FakeClock

from .fakes import FakeArmory, FakePorts, job, member, persona
from .test_raid_night import _run_async, advance, chat, controls, form, inside, running
from .test_service import app, armory  # noqa: F401  (app: test_service's fixture)


def build(ports, *, summon_plan=None):
    """test_raid_night's roster and fakes, with an optional arrival plan."""
    tanks = {101, 103, 105, 106, 107}
    healers = {102, *range(111, 118)}
    titles = {101: "Raid Lead", 102: "Healing", 104: "Raid Lead, DKP", 107: "Tank",
              108: "Raid Lead, Historian", 109: "Raid Lead, Theorycraft", 120: "Warlock"}
    for guid in range(101, 131):
        cls, spec = (1, 2) if guid in tanks else (5, 0) if guid in healers else (9, 1) if guid == 120 else (8, 1)
        ports.members[guid] = member(guid, f"M{guid}", cls, spec)
        ports.personas[guid] = persona(guid, f"M{guid}", guild_role="officer" if guid in titles else "raider",
                                       officer_title=titles.get(guid, ""))
    prior = new_raid(ports.conn, started=ports.clock.now() - 86400, raid_key="naxx", size=25,
                     heroic=False, offline=True, lead_guid=101, tank_guid=101, loot_guid=104)
    ports.conn.execute("UPDATE guild_raid SET ended=started+100 WHERE raid_id=?", (prior,))
    record_attendance(ports.conn, prior, [], [130])

    async def wait_job(cmd_id, timeout_s):
        command = next(cmd for cmd in reversed(ports.sent) if cmd.id == cmd_id)
        kind = "form_raid" if isinstance(command, FormRaid) else "summon"
        guids = command.members if isinstance(command, FormRaid) else command.guids
        return job(kind, "done", id=cmd_id, guids=guids,
                   instance_id=77 if isinstance(command, Summon) and command.where == "inside" else 0)
    ports.wait_job = wait_job
    kw = {"summon_plan": summon_plan} if summon_plan else {}
    armory = FakeArmory({}, {})
    return ports, armory, RaidNight(ports, GuildConfig(), armory, random.Random(7), **kw)


@pytest.fixture
def setup(fake):
    return build(fake)


@pytest.fixture
def late(fake):
    return build(fake, summon_plan=lambda members: (sorted(members)[:-2],
                                                   [(sorted(members)[-2], 100), (sorted(members)[-1], 140)]))


class Timed:
    """A listener with a clock-driven countdown, as RaidCalls has."""
    def __init__(self):
        self.heard = []

    async def on_ready(self, run):
        self.heard.append("ready")

    async def on_go(self, run):
        self.heard.append("go")


def shape(cmd):
    """A command without its random id."""
    return replace(cmd, id="")


async def logged_in_and_called(setup):
    """His login, then the call already out (his chat pick needs no intent words then)."""
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    night.state, night._call_deadline = State.CALLED, ports.clock.now() + 600


# -- not while he is away ------------------------------------------------------------------------

@_run_async
async def test_every_action_is_refused_while_he_is_not_in_game(setup):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    await night.on_logout(Logout(0, 1, True))
    before = (night.state, list(ports.sent), list(ports.proposed))
    pick = night.console_pick("icc", 25, False, None)
    for action in CONSOLE_ACTIONS:
        assert night.console_refusal(action).startswith("you're not in game")
        with pytest.raises(ConsoleRefused, match="not in game"):
            await night.console_act(action, pick if action == "form" else None)
    assert (night.state, ports.sent, ports.proposed) == before
    status = night.console_status()
    assert set(status["actions"]) == set(CONSOLE_ACTIONS)
    assert all(why.startswith("you're not in game") for why in status["actions"].values())


@_run_async
async def test_an_online_flag_without_his_login_is_not_enough(setup):
    ports, _, night = setup
    night._present = False  # the bridge says online, but this controller saw no login
    assert night.console_refusal("call").startswith("you're not in game")


# -- call ----------------------------------------------------------------------------------------

@_run_async
async def test_call_brings_the_calls_own_timer_forward(setup):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    await night.tick()
    assert night.state is State.WAITING_CALL and ports.proposed == []
    assert night.console_status()["call_in"] > 0
    assert await night.console_act("call") == "the raid call goes out in a moment"
    assert night.state is State.WAITING_CALL  # the tick makes it, as at its own time
    await night.tick()
    assert night.state is State.CALLED and len(ports.proposed) == 1
    assert ports.proposed[0][:2] == (101, "guild") and "Testy" in ports.proposed[0][2]
    assert night.console_refusal("call") == "no call to make: the raid call is out, waiting for your pick"
    with pytest.raises(ConsoleRefused):
        await night.console_act("call")
    await night.tick()
    assert len(ports.proposed) == 1


@_run_async
async def test_call_is_refused_before_his_login_and_on_a_quiet_or_ended_night(setup):
    ports, _, night = setup
    assert night.console_refusal("call").startswith("no call to make: no raid night is open")
    await night.on_login(Login(0, 1, True))
    night._end("called")
    assert night.console_refusal("call") == "no call to make: tonight's raid night has ended"


# -- form ----------------------------------------------------------------------------------------

def _twin(tmp_path):
    clock = FakeClock(datetime(2026, 10, 29, 20, 0, tzinfo=timezone.utc))
    return build(FakePorts(tmp_path, clock))


@_run_async
async def test_form_sends_exactly_what_his_chat_pick_sends(tmp_path):
    (tmp_path / "chat").mkdir()
    (tmp_path / "console").mkdir()
    by_chat, by_console = _twin(tmp_path / "chat"), _twin(tmp_path / "console")
    try:
        for ports, _, night in (by_chat, by_console):
            night.state = State.CALLED
            night._call_deadline = ports.clock.now() + 600
        await by_chat[2].on_chat(chat(by_chat[0], "icc 25"))
        message = await by_console[2].console_act("form", by_console[2].console_pick("icc", 25, False, None))
        assert message.startswith("forming Icecrown Citadel 25")
        assert by_chat[2]._pick == by_console[2]._pick == Pick("icc", 25, False, None)
        for _, _, night in (by_chat, by_console):
            await night.tick()
        sent = [[shape(c) for c in ports.sent] for ports, _, _ in (by_chat, by_console)]
        assert [type(c) for c in sent[0]] == [ClearLockout, RaidControl, FormRaid, SetLootMaster, Summon]
        assert sent[0] == sent[1]
        assert by_chat[0].proposed == by_console[0].proposed
        assert by_chat[2].state is by_console[2].state is State.ASSEMBLING
        assert by_chat[2].run.plan == by_console[2].run.plan
    finally:
        by_chat[0].close()
        by_console[0].close()


@_run_async
async def test_form_keeps_a_lead_he_named_in_chat_and_resolves_only_officers(setup):
    ports, _, night = setup
    assert night.console_pick("icc", None, False, "me").lead == Lead(True)
    assert night.console_pick("icc", None, False, "rotate").lead == Lead(False, None, True)
    assert night.console_pick("icc", None, False, 108).lead == Lead(False, 108)
    for guid in (110, 999, 1):  # a raider, a stranger, himself
        with pytest.raises(ConsoleRefused, match="only an officer"):
            night.console_pick("icc", None, False, guid)
    await night.on_login(Login(0, 1, True))
    night._lead = Lead(False, 108)  # as "let M108 lead" would leave it
    await night.console_act("form", night.console_pick("icc", 10, True, None))
    assert night._pick == Pick("icc", 10, True, Lead(False, 108))


@_run_async
async def test_form_is_refused_while_a_raid_runs_or_closes(setup):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    assert night.console_refusal("form") is None  # waiting for the call: his chat may pick too
    await logged_in_and_called(setup)
    await form(setup)
    assert night.state is State.ASSEMBLING
    assert night.console_refusal("form") == "the raid is assembling: end it first"
    night._end("called")
    assert night.console_refusal("form") == "the last raid is still closing"
    await night.tick()
    assert night.console_refusal("form") is None


@_run_async
async def test_an_expired_call_lapses_first_as_his_own_line_would(setup):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    night.state, night._call_deadline = State.CALLED, ports.clock.now() - 1
    await night.console_act("form", night.console_pick("ulduar", 10, False, None))
    assert len(ports.proposed) == 1  # call_lapsed, as on_chat proposes it first
    assert night.state is State.FORMING and night._pick.raid_key == "ulduar"
    night.state, night._call_deadline = State.CALLED, ports.clock.now() - 1
    with pytest.raises(ConsoleRefused, match="nothing to end: tonight's raid night has ended"):
        await night.console_act("end")


# -- summon --------------------------------------------------------------------------------------

@_run_async
async def test_summon_brings_the_late_arrivals_timers_forward(late):
    ports, _, night = late
    await form(late)
    assert len(night._late) == 2 and night.console_refusal("summon") is None
    waiting = [guid for guid, _ in night._late]
    sent = len(ports.sent)
    assert await night.console_act("summon") == "summoning 2 late members now"
    await night.tick()
    summons = [c for c in ports.sent[sent:] if isinstance(c, Summon)]
    assert [c.guids for c in summons] == [(waiting[0],), (waiting[1],)]
    assert all(c.where == "entrance" for c in summons)  # he is not inside yet
    assert night.console_refusal("summon") == "nobody is still on the way: everyone has been summoned"


@_run_async
async def test_summon_waits_for_a_pull_and_needs_a_raid(late):
    ports, _, night = late
    await night.on_login(Login(0, 1, True))
    assert night.console_refusal("summon").startswith("no raid is under way")
    await logged_in_and_called(late)
    await form(late)
    night._assembly_pulls[3] = 1
    assert night.console_refusal("summon") == "a pull is under way: late members are summoned once it ends"


# -- start and stop --------------------------------------------------------------------------------

@_run_async
async def test_start_brings_the_ready_check_then_the_countdown_forward(setup):
    ports, _, night = setup
    timed = Timed()
    night.add_listener(timed)
    await form(setup)
    assert night.console_refusal("start") == "you're not in Icecrown Citadel yet: the ready check follows once you are"
    await inside(setup)
    assert night._ready_due is not None and not controls(ports, "ready_check")
    assert night.console_refusal("start") is None
    await night.console_act("start")
    await night.tick()
    assert controls(ports, "ready_check") and night._dc_due is not None and not timed.heard
    assert night.console_refusal("start") is None  # the countdown has not begun
    assert await night.console_act("start") == "the pull countdown starts now; dungeon-clear follows it"
    await night.tick()
    assert timed.heard == ["ready"] and not controls(ports, "dc_on")
    assert night.console_refusal("start") == "the pull countdown is under way"
    await advance(ports, 4)
    await night.tick()
    assert not controls(ports, "dc_on")  # never before its five seconds
    await advance(ports, 1)
    await night.tick()
    assert timed.heard == ["ready", "go"] and len(controls(ports, "dc_on")) == 1
    assert night.state is State.RUNNING
    assert night.console_refusal("start") == "dungeon-clear is already running"


@_run_async
async def test_start_and_stop_need_a_bot_lead(setup):
    ports, _, night = setup
    await form(setup, "icc 10, i'll lead")
    await inside(setup)
    assert night.state is State.RUNNING and night.run.lead_is_human
    for action in ("start", "stop"):
        assert night.console_refusal(action) == "you lead tonight: dungeon-clear runs only under a bot lead"


@_run_async
async def test_stop_and_start_switch_dungeon_clear_and_the_close_stops_it_once(setup):
    ports, _, night = setup
    await running(setup)
    assert night.console_refusal("stop") is None and night.console_status()["run"]["dc"] == "on"
    message = await night.console_act("stop")
    assert message == "dungeon-clear stopped: the raid goes on, and Start resumes it"
    stop = controls(ports, "dc_off")
    assert len(stop) == 1 and (stop[0].member_guid, stop[0].leader_guid) == (1, 101)
    assert night.state is State.RUNNING and night.console_status()["run"]["dc"] == "held"
    assert night.console_refusal("stop") == "dungeon-clear is already stopped"
    assert await night.console_act("start") == "dungeon-clear resumed"
    assert len(controls(ports, "dc_on")) == 2 and night.console_status()["run"]["dc"] == "on"
    await night.console_act("end")
    await night.tick()
    assert night.state is State.ENDED and len(controls(ports, "dc_off")) == 2  # its close


@_run_async
async def test_a_raid_ended_while_stopped_is_not_stopped_twice(setup):
    ports, _, night = setup
    await running(setup)
    await night.console_act("stop")
    await night.console_act("end")
    await night.tick()
    assert night.state is State.ENDED and len(controls(ports, "dc_off")) == 1


@pytest.mark.parametrize("failure", ["refused", "lost", "wrong_id"])
@_run_async
async def test_an_unconfirmed_stop_says_so_and_leaves_the_close_stop(setup, failure):
    ports, _, night = setup
    await running(setup)
    real = ports.send

    async def send(cmd):
        if isinstance(cmd, RaidControl) and cmd.action == "dc_off" and not controls(ports, "dc_off"):
            ports.sent.append(cmd)
            if failure == "lost":
                raise ConnectionError("gone")
            ack = await FakePorts.send(ports, cmd)
            ports.sent.pop()
            return (replace(ack, ok=False, error="dc_refused: <private>") if failure == "refused"
                    else replace(ack, id="other"))
        return await real(cmd)
    ports.send = send
    words = {"refused": r"the server refused it \(dc_refused:private\)",
             "lost": "did not confirm", "wrong_id": "did not confirm"}[failure]
    with pytest.raises(ConsoleRefused, match=words):
        await night.console_act("stop")
    assert night.console_status()["run"]["dc"] == "on"
    await night.console_act("end")
    await night.tick()
    assert len(controls(ports, "dc_off")) == 2  # the close still stops it


@_run_async
async def test_a_dungeon_clear_switch_waits_only_briefly_for_a_busy_tick(setup, monkeypatch):
    ports, _, night = setup
    await running(setup)
    monkeypatch.setattr(raid_night_module, "CONSOLE_LOCK_S", 0.05)
    await night._lock.acquire()
    try:
        with pytest.raises(ConsoleRefused, match="busy with the game"):
            await night.console_act("stop")
    finally:
        night._lock.release()
    assert not controls(ports, "dc_off")


# -- end -----------------------------------------------------------------------------------------

@_run_async
async def test_end_is_his_calling_it_and_before_a_pick_his_decline(setup):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    assert night.console_refusal("end") == "nothing to end: the raid call has not gone out yet"
    night.state, night._call_deadline = State.CALLED, ports.clock.now() + 600
    assert await night.console_act("end") == "no raid tonight: the guild has been told"
    assert night.state is State.ENDED and len(ports.proposed) == 1
    await logged_in_and_called(setup)
    await running(setup)
    await night.console_act("end")
    await night.tick()
    assert ports.conn.execute("SELECT outcome FROM guild_raid WHERE ended IS NOT NULL "
                              "ORDER BY raid_id DESC LIMIT 1").fetchone()[0] == "called"


# -- status --------------------------------------------------------------------------------------

@_run_async
async def test_status_shows_the_state_its_timers_and_each_refusal(setup):
    ports, _, night = setup
    await night.on_login(Login(0, 1, True))
    status = night.console_status()
    assert status["state"] == "waiting_call" and status["present"] and status["run"] is None
    assert status["actions"]["call"] is None and status["actions"]["form"] is None
    assert status["actions"]["stop"].startswith("no raid is under way")
    await logged_in_and_called(setup)
    await running(setup)
    run = night.console_status()["run"]
    assert (run["raid"], run["size"], run["lead"], run["tank"], run["dc"]) == ("icc", 25, 101, 101, "on")
    assert run["in_zone"] and set(run["inside"]) == set(run["members"]) and run["late"] == []
    assert night.console_status()["actions"] == {
        "call": "no call to make: the raid is under way", "form": "the raid is under way: end it first",
        "summon": "nobody is still on the way: everyone has been summoned",
        "start": "dungeon-clear is already running", "stop": None, "end": None}


@_run_async
async def test_an_unknown_action_is_refused(setup):
    ports, _, night = setup
    with pytest.raises(ConsoleRefused, match="unknown"):
        await night.console_act("disband")


def test_a_server_error_code_is_shown_only_as_identifier_text():
    assert raid_night_module._code("leader_mismatch:0") == "leader_mismatch:0"
    assert raid_night_module._code("bad <b>html</b> \n text") == "badbhtmlbtext"
    assert raid_night_module._code("") == "no reason given"
    assert len(raid_night_module._code("x" * 500)) == 60


# -- guild life's own guards around them -----------------------------------------------------------

FORM = {"action": "form", "raid": "icc", "size": 25, "heroic": False, "lead": None}


@_run_async
async def test_guild_life_refuses_raid_actions_whenever_it_would_not_run_them(app, monkeypatch):
    service = install(app, armory=armory())
    cases = [("the mind is not connected to the game", lambda: None)]

    def pending():
        service._connected, service._presence_pending = True, True
    cases.append(("your login or logout is still being handled: try again in a moment", pending))

    def no_armory():
        service._presence_pending = False
        service.armory.available = False
    cases.append(("the game's database (the guild armory) is unavailable, so guild life runs "
                  "no raid nights", no_armory))

    def offline():
        service.armory.available = True
        monkeypatch.setattr(type(service.offline), "active", property(lambda self: True))
    cases.append(("an offline raid is still running or closing", offline))

    def stopping():
        monkeypatch.undo()
        service._stopping = True
    cases.append(("the mind is stopping", stopping))
    for reason, arrange in cases:
        arrange()
        status = service.console_status()
        assert status["blocked"] == reason
        assert set(status["actions"].values()) == {reason}
        for args in (FORM, *({"action": a} for a in CONSOLE_ACTIONS if a != "form")):
            with pytest.raises(ConsoleRefused) as refused:
                await service.console_action(args)
            assert str(refused.value) == reason
    assert service.raid_night.state is State.IDLE and service.raid_night._pick is None
    app.bridge.send.assert_not_called()


@_run_async
async def test_guild_life_hands_his_pick_to_the_raid_night(app, fake):
    service = install(app, armory=armory())
    service._connected = True
    status = service.console_status()
    assert status["blocked"] is None and status["offline_raid"] is False
    assert status["actions"]["form"] is None
    with pytest.raises(ConsoleRefused, match="only an officer"):
        await service.console_action({**FORM, "lead": 999})
    message = await service.console_action(FORM)
    assert message.startswith("forming Icecrown Citadel 25")
    assert service.raid_night.state is State.FORMING
    assert service.raid_night._pick == Pick("icc", 25, False, None)


@_run_async
async def test_a_dungeon_clear_switch_asks_every_guard_again_once_it_has_the_lock(setup):
    ports, _, night = setup
    await running(setup)
    held_reason = [None]
    await night._lock.acquire()  # a tick talking to the game
    stopping = asyncio.create_task(night.console_act("stop", guard=lambda: held_reason[0]))
    await asyncio.sleep(0.01)
    held_reason[0] = "the mind is not connected to the game"  # the bridge dropped meanwhile
    night._lock.release()
    with pytest.raises(ConsoleRefused, match="not connected"):
        await stopping
    assert not controls(ports, "dc_off") and night.console_status()["run"]["dc"] == "on"
    await night.console_act("stop")
    await night._lock.acquire()
    starting = asyncio.create_task(night.console_act("start", guard=lambda: "the mind is stopping"))
    night._lock.release()
    with pytest.raises(ConsoleRefused, match="stopping"):
        await starting
    assert len(controls(ports, "dc_on")) == 1 and night.console_status()["run"]["dc"] == "held"


@_run_async
async def test_a_start_that_is_already_due_says_so(setup):
    ports, _, night = setup
    await form(setup)
    await inside(setup)
    night._ready_due = ports.clock.now() - 1  # the tick is about to send it
    assert await night.console_act("start") == (
        "the ready check is already due: it goes out in a moment")
