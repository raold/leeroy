"""The raid lead's service on an in-process app: designation (console, chat, default),
persistence and its end with the group, orders carried out and acknowledged only after the game's
job, plain refusals, fences, the model fallback and the callouts."""

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


@pytest.fixture
def app(store, clock, tmp_path):
    return make_app(store, clock, tmp_path)


def said_by(app):
    return [(t.speaker, t.kind, t.instruction) for t in app.guild.source.turns]


def line(text, *, channel="raid", from_guid=HUMAN):
    return Chat(ts=0, channel=channel, from_guid=from_guid, from_name="Rowan", to_guid=0,
                text=text, map=631, zone=4812)


async def say(service, text, *, line_id=7, **kw):
    """His line as the app hands it over: claimed (or not), then its chat event."""
    ev = line(text, **kw)
    claimed = service.claims(ev, line_id)
    await service.event(ev)
    await drain(service)
    return claimed


def led(app, guid, nights=1):
    for _ in range(nights):
        app.store.conn.execute("INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,"
                               "tank_guid,loot_guid,ended) VALUES (1,'icc',10,0,0,?,?,?,2)",
                               (guid, guid, guid))
    app.store.conn.commit()


# -- designation ---------------------------------------------------------------------------------

def test_default_lead_is_the_officer_with_most_nights_led(app):
    async def main():
        led(app, 103, 2)
        led(app, 101, 1)
        service = await ready(app)
        await drain(service)
        assert (service.lead(), service.source) == (103, "default")
        assert app.raid.lead() == 103
        assist = [c for c in app.bridge.sent if isinstance(c, RaidLead)]
        assert [(c.action, c.guid) for c in assist] == [("assist", 103)]
        assert service.assisted == 103
        assert said_by(app) == []  # the default is silent
    run(main())


def test_default_breaks_ties_by_attendance_then_lowest_guid(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert service.lead() == 101  # no nights on record: the lowest guid of the officers
    run(main())


def test_a_party_has_no_assistant_flag(app):
    async def main():
        service = await ready(app, raid=False)
        await drain(service)
        assert service.lead() == 101 and app.bridge.sent == []
        assert service.console_status()["assistant"] is False
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
        assert await say(service, "Coros, you're leading tonight")
        assert (service.lead(), service.source) == (103, "chat")
        assist = [(c.action, c.guid) for c in app.bridge.sent if isinstance(c, RaidLead)]
        assert assist == [("assist", 101), ("unassist", 101), ("assist", 103)]
        [(speaker, kind, text)] = said_by(app)
        assert speaker == 103 and kind is TurnKind.REPLY_HUMAN and "asked you to lead" in text
        stored = json.loads(app.store.get_meta(KEY))
        assert stored["lead"] == 103 and stored["source"] == "chat" and stored["assisted"] == 103
    run(main())


def test_he_can_take_it_himself(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert await say(service, "I'll lead")
        await drain(service)
        assert service.lead() is None and service.his
        assert app.raid._designated is None
    run(main())


def test_console_sets_and_clears(app):
    async def main():
        service = await ready(app)
        await drain(service)
        status = service.console_status()
        assert [c["guid"] for c in status["candidates"]] == sorted(CAST)
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
        assert service.lead() == 101  # the guild's pick again
    run(main())


def test_designation_persists_and_ends_with_the_group(app):
    async def main():
        service = await ready(app)
        await service.console_action({"action": "set", "guid": 104})
        again = RaidLeadService(app)
        assert (again.lead(), again.source, again.members) == (104, "console", frozenset(CAST))
        # she leaves his group: her designation ends
        await service.event(Group(0, HUMAN, True, (HUMAN, 101, 102, 103, 105)))
        assert service.lead() is None and json.loads(app.store.get_meta(KEY))["lead"] is None
        await drain(service)
        assert service.lead() == 101
        # the group breaks
        await service.event(Group(0, 0, False, ()))
        assert service.lead() is None and service.members == frozenset()
        assert app.raid._designated is None
    run(main())


def test_a_bad_stored_state_is_no_lead(app):
    app.store.set_meta(KEY, '{"lead": 999, "source": "chat", "members": [101], "his": true}')
    service = RaidLeadService(app)
    assert service.lead() is None and service.members == frozenset({101})
    app.store.set_meta(KEY, "not json")
    assert RaidLeadService(app).lead() is None


def test_his_guild_group_counts_as_his_group(app):
    from leeroy_mind.guild.protocol import GuildGroup
    async def main():
        service = RaidLeadService(app)
        await service.connection(True)
        await service.event(Hello(0, "mod-leeroy", "1", 21, HUMAN, ("raidlead",)))
        await service.event(GuildGroup(0, "built", 25, HUMAN, True, 571, 0, 0, (), (101, 102)))
        assert service.members == frozenset({101, 102}) and service.raid
    run(main())


# -- fences --------------------------------------------------------------------------------------

@pytest.mark.parametrize("fence", ["no_feature", "offline", "not_leader", "offline_raid", "mind_led",
                                   "forming", "member_line", "whisper", "private_room"])
def test_fences_keep_lines_as_talk(app, fence):
    async def main():
        service = await ready(app)
        await drain(service)
        kw = {}
        if fence == "no_feature":
            await service.event(Hello(0, "mod-leeroy", "1", 21, HUMAN, ("playstyle",)))
        elif fence == "offline":
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
        before = list(app.guild.ports.sent)
        assert not await say(service, "everyone stack on me", **kw)
        assert app.guild.ports.sent == before
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
        assert await say(service, "everyone spread out", channel="voice:raid1")
        assert [(c.action, c.param) for c in app.guild.ports.sent] == [("order", "spread")]
    run(main())


# -- orders --------------------------------------------------------------------------------------

def test_an_order_runs_on_his_group_and_is_acknowledged_after_the_job(app):
    async def main():
        service = await ready(app)
        await drain(service)
        assert await say(service, "everyone stack on me", line_id=42)
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
        [cmd] = app.guild.ports.sent
        assert (cmd.param, cmd.guids) == ("run_out", (104,))
        assert said_by(app)[0][0] == 104
    run(main())


@pytest.mark.parametrize("text, guids", [
    ("tanks swap", {101, 105}), ("taunt", {101, 105}), ("healers, spread out", {102}),
    ("melee back off", {104}), ("ranged: spread", {103}), ("dps hold", {103, 104}),
])
def test_addressees(app, text, guids):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, text)
        assert set(app.guild.ports.sent[0].guids) == guids
    run(main())


def test_stack_on_a_named_member(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "stack on belra")
        cmd = app.guild.ports.sent[0]
        assert cmd.target_guid == 102
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
        assert "on it" not in text
    run(main())


def test_refused_at_the_ack_is_plain_and_native_off_is_silent(app):
    async def main():
        service = await ready(app)
        await drain(service)
        app.guild.ports.acks.append((False, "busy"))
        await say(service, "everyone follow me")
        assert app.guild.ports.said[-1][2] == "can't do that one, they're busy" and said_by(app) == []
        app.guild.ports.acks.append((False, "raidlead_off"))
        await say(service, "everyone follow me")
        assert len(app.guild.ports.said) == 1 and said_by(app) == []
    run(main())


def test_lust_uses_the_shaman_and_her_faction(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "lust now")
        [cmd] = app.guild.ports.sent
        assert (cmd.action, cmd.param, cmd.guids) == ("cast", "bloodlust", (102,))
        assert "Bloodlust now" in said_by(app)[0][2]
    run(main())


def test_lust_without_a_shaman_is_refused(app):
    async def main():
        service = await ready(app, members=(101, 103, 104))
        await drain(service)
        await say(service, "bl")
        assert app.guild.ports.sent == []
        assert app.guild.ports.said[-1][2] == C.refusal("no_shaman")
    run(main())


def test_kill_skull_marks_his_target_first(app):
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


def test_hold_is_released_when_he_pulls(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "everyone wait for my pull")
        app.guild.ports.sent.clear()
        await service.event(Boss(0, 631, 77, 0, "", "engaged", (HUMAN, 101)))
        await drain(service)
        [cmd] = app.guild.ports.sent
        assert (cmd.param, set(cmd.guids)) == ("follow", set(CAST))
    run(main())


def test_wipe_releases_the_dead_and_she_calls_it(app):
    async def main():
        service = await ready(app)
        await drain(service)
        await service.event(Death(0, 104, 631, 0))
        await service.event(Death(0, 102, 631, 0))
        await service.event(Boss(0, 631, 77, 0, "", "wiped", (HUMAN, 101, 102, 104)))
        await drain(service)
        [cmd] = app.guild.ports.sent
        assert (cmd.param, cmd.guids) == ("release", (102, 104))
        [(speaker, kind, text)] = said_by(app)
        assert speaker == 101 and kind is TurnKind.URGENT and "2 released" in text
    run(main())


# -- the model fallback --------------------------------------------------------------------------

def model_app(store, clock, tmp_path, answer):
    from leeroy_mind.testing.fake_llm import FakeLLM
    llm = FakeLLM(lambda model, messages: json.dumps(answer))
    return make_app(store, clock, tmp_path, llm=llm)


def test_model_maps_an_unclear_order_onto_the_list(store, clock, tmp_path):
    app = model_app(store, clock, tmp_path, {"order": "stay", "who": "everyone", "member": "",
                                             "confidence": 0.9})

    async def main():
        service = await ready(app)
        await drain(service)
        assert await say(service, "everyone get behind the boss")
        [call] = app.llm.calls
        assert call["model"] == LIVE and call["think"] is False
        assert call["format"]["properties"]["order"]["enum"][-1] == "none"
        [cmd] = app.guild.ports.sent
        assert cmd.param == "stay" and set(cmd.guids) == set(CAST)
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


def test_a_sure_none_goes_back_to_talk(store, clock, tmp_path):
    app = model_app(store, clock, tmp_path, {"order": "none", "who": "member", "member": "belra",
                                             "confidence": 0.9})

    async def main():
        service = await ready(app)
        await drain(service)
        await say(service, "Belra, go get some food", line_id=9)
        assert app.guild.ports.sent == [] and said_by(app) == [] and app.released == [9]
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

def test_ready_check_is_hers_once_she_is_assistant(app):
    async def main():
        service = await ready(app)
        await drain(service)
        app.bridge.sent.clear()
        assert await say(service, "rc")
        [rc] = app.bridge.sent
        assert isinstance(rc, RaidControl) and (rc.member_guid, rc.action, rc.leader_guid) == (HUMAN, "ready_check", 101)
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


def test_boss_brief_is_the_playbook_hook_and_returns_none_for_now():
    context = C.BossContext(631, 0, "Lord Marrowgar", 25, False)
    assert C.boss_brief(context) is None
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
