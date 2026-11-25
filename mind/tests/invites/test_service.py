"""His invitations carried out in a real MindApp (not started, a scripted wire for the bridge): his
line read, the members decided, one string_do per step marked as an invitation, and her answer's
instruction telling what her body really did."""
import asyncio
from types import SimpleNamespace

import pytest

from conftest import run
from selfies.fakes import OWNER, mind_app
from leeroy_mind.bridge import NotConnected
from leeroy_mind.guild.protocol import Job, StringDo
from leeroy_mind.invites import policy, service as svc
from leeroy_mind.invites.service import native_code, read_answer
from leeroy_mind.protocol import Ack, Chat, Group, Hello, Member
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.testing.wire import WireContract
from leeroy_mind.talk.pipeline import TurnMeta
from leeroy_mind.talk.turns import Turn, TurnKind

ORG = (1, 1637)  # Orgrimmar: map, zone
NAMES = {101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna", 105: "Ebbo"}


class Wire:
    """The bridge as the service sees it: native's done job first, then the ack. answers: kind ->
    (ok, error, what she did) or an exception."""

    def __init__(self, app, answers=None):
        self.app, self.answers, self.sent, self.connected = app, answers or {}, [], True
        self.wire = WireContract()

    async def send(self, cmd, *, preserve_id=False):
        assert preserve_id and isinstance(cmd, StringDo)
        answer = self.answers.get(cmd.kind, (True, "", {"come_to_me": "came", "go_to": "went",
                                                         "fish": "fishing", "sit": "sat",
                                                         "join_group": "joined",
                                                         "follow": "following"}[cmd.kind]))
        if callable(answer):
            answer = answer(cmd)
        if isinstance(answer, NotConnected):
            raise answer  # BridgeClient refuses before it claims the identity
        _, refusal = self.wire.send(cmd, preserve_id=preserve_id)
        self.sent.append(cmd)
        if refusal:
            return Ack(0, cmd.id, False, refusal)
        if isinstance(answer, Exception):
            raise answer
        ok, error, reason = answer
        if ok and reason is not None:
            self.app.invites.on_job(Job(ts=0, id=cmd.id, kind="string_do", state="done",
                                        reason=reason, stage="", guids=(cmd.guid,), skipped=(),
                                        kills=0, wipes=0, instance_id=0, guid=cmd.guid,
                                        level=0, avg_ilvl=0))
        return Ack(0, cmd.id, ok, error)


@pytest.fixture
def app(tmp_path):
    app = mind_app(tmp_path, toml='[human]\npronouns = "he"\n')
    world = app.world
    world.human_guid, world.human_online, world.human_zone = OWNER, True, ORG[1]
    world.members = {g: Member(guid=g, name=n, race=2, class_=1, gender=1, level=80, rank=3,
                               online=True, map=ORG[0], zone=ORG[1], spec_tab=-1)
                     for g, n in NAMES.items()}
    world.online = set(NAMES)
    app.hello = Hello(ts=0, server="mod-leeroy", version="0.1.0", guild_id=0, human_guid=OWNER,
                      features=("invites",))
    real = app.bridge
    app.bridge = Wire(app)
    yield app
    app.bridge = real
    run(app.stop())


@pytest.fixture
def yes(monkeypatch):
    """Every member goes along (the seeded draw always under her willingness)."""
    monkeypatch.setattr(policy, "seeded", lambda line, guid: SimpleNamespace(random=lambda: 0.0))


@pytest.fixture
def no(monkeypatch):
    monkeypatch.setattr(policy, "seeded", lambda line, guid: SimpleNamespace(random=lambda: 0.999))


def chat(text, *, channel="whisper", to=102, zone=ORG[1], map_=ORG[0]):
    return Chat(ts=0, channel=channel, from_guid=OWNER, from_name="Ownerly",
                to_guid=to if channel == "whisper" else 0, text=text, map=map_, zone=zone)


async def settle(app, seconds=0.0):
    for _ in range(3):
        await asyncio.sleep(0)
    if seconds:
        await app.clock.advance(seconds, 1.0)
    tasks = [t for t in app.invites._tasks if not t.done()]
    if tasks:
        await asyncio.wait(tasks, timeout=5)


def told(app, line, guid, channel="whisper"):
    turn = Turn(TurnKind.REPLY_HUMAN, guid, channel, trigger_line=line)
    return app.invites.instruction(turn, TurnMeta("t", why="reply:invite"), "")


def test_meet_me_at_the_inn_puts_her_there_and_she_says_so(app, yes):
    async def main():
        assert app.invites.claims(chat("meet me at the inn in dalaran"), 501) == "invite"
        await settle(app)
        [cmd] = app.bridge.sent
        assert (cmd.guid, cmd.kind, cmd.spot, cmd.slot, cmd.via, cmd.target_guid) == (
            102, "go_to", "dal_inn", 0, "invite", OWNER)
        [rt] = app.invites.reply_turns(SimpleNamespace(id=501), "whisper")
        assert (rt.speaker, rt.kind, rt.reply_to, rt.to_guid) == (102, TurnKind.REPLY_HUMAN, "whisper", OWNER)
        assert app.invites.ready(501, 102).is_set()
        said = told(app, 501, 102)
        assert "the Legerdemain Lounge in Dalaran" in said and "waiting for him there" in said
    run(main())


def test_a_native_refusal_is_her_reason_and_never_on_her_way(app, yes):
    async def main():
        app.bridge.answers = {"go_to": (False, "combat", None)}
        assert app.invites.claims(chat("meet me at the bank"), 502) == "invite"
        await settle(app)
        said = told(app, 502, 102)
        assert "middle of a fight" in said and "Don't say you're on your way" in said
    run(main())


def test_fishing_comes_first_then_casts(app, yes):
    async def main():
        assert app.invites.claims(chat("let's go fishing"), 503) == "invite"
        await settle(app, 10)
        assert [c.kind for c in app.bridge.sent] == ["come_to_me", "fish"]
        said = told(app, 503, 102)
        assert "beside him now" in said and "line is in the water" in said
    run(main())


def test_fishing_in_dalaran_goes_to_the_fountain(app, yes):
    async def main():
        app.invites.claims(chat("let's go fishing in Dalaran"), 504)
        await settle(app, 10)
        assert [(c.kind, c.spot) for c in app.bridge.sent] == [("go_to", "dal_fountain"), ("fish", "")]
    run(main())


def test_a_landing_member_is_asked_again_and_a_refusal_after_her_answer_gets_a_follow_up(app, yes):
    async def main():
        tries = []

        def fish(cmd):
            tries.append(cmd)
            return (False, "travelling", None) if len(tries) == 1 else (False, "no_fishing", None)
        app.bridge.answers = {"fish": fish}
        spoken = []

        async def speak(turn, *, why="speak", **kw):
            spoken.append((turn, why))
            return []
        app.engine.speak = speak
        app.invites.claims(chat("let's go fishing"), 505)
        await settle(app)
        first = told(app, 505, 102)  # her answer, written once she had come over
        assert "beside him now" in first
        await settle(app, 20)
        assert len(tries) == 2  # travelling: still landing, so asked again
        [(turn, why)] = spoken
        assert why == "source:invite" and turn.speaker == 102 and turn.channel == "whisper"
        assert "never learned to fish" in turn.instruction
    run(main())


def test_she_says_no_and_nothing_moves(app, no):
    async def main():
        assert app.invites.claims(chat("come here"), 506) == "invite"
        await settle(app)
        assert app.bridge.sent == []
        said = told(app, 506, 102)
        assert "You're not going" in said and "Don't say you're on your way" in said
    run(main())


def test_dislike_says_no_whatever_the_draw(app, yes):
    async def main():
        app.store.put_relationship(Relationship(a=102, b=OWNER, affinity=-60))
        app.invites.claims(chat("come here"), 507)
        await settle(app)
        assert app.bridge.sent == [] and "not with him" in told(app, 507, 102)
    run(main())


def test_an_unknown_place_moves_nobody_and_she_asks_where(app, yes):
    async def main():
        app.invites.claims(chat("meet me at the docks"), 508)
        await settle(app)
        assert app.bridge.sent == [] and "which place he means" in told(app, 508, 102)
    run(main())


def test_later_moves_nobody(app, yes):
    async def main():
        app.invites.claims(chat("lets go fishing later"), 509)
        await settle(app)
        assert app.bridge.sent == [] and "later, not right now" in told(app, 509, 102)
    run(main())


def test_anyone_takes_one_or_two_willing_members_and_only_they_answer(app, yes):
    async def main():
        assert app.invites.claims(chat("anyone wanna go fishing?", channel="guild"), 510) == "invite"
        await settle(app, 10)
        turns = app.invites.reply_turns(SimpleNamespace(id=510), "guild")
        assert 1 <= len(turns) <= 2
        assert {c.guid for c in app.bridge.sent} == {rt.speaker for rt in turns}
        other = next(g for g in NAMES if g not in {rt.speaker for rt in turns})
        assert "not you" in told(app, 510, other, channel="guild")
    run(main())


def test_at_most_two_named(app, yes):
    async def main():
        app.invites.claims(chat("Arkon, Belra and Coros come here", channel="guild"), 511)
        await settle(app)
        assert sorted(c.guid for c in app.bridge.sent) == [101, 102]
    run(main())


def test_six_moves_an_hour_then_she_cannot_get_away(app, yes):
    async def main():
        for n, guid in enumerate((101, 102, 103, 104, 105, 101, 102)):
            app.invites._pending.clear()
            app.invites.claims(chat("come here", to=guid), 520 + n)
            await settle(app)
        assert len(app.bridge.sent) == 6
        assert "can't get away right now" in told(app, 526, 102)
        await app.clock.advance(3601, 60)
        app.invites.claims(chat("come here", to=103), 530)
        await settle(app)
        assert len(app.bridge.sent) == 7
    run(main())


def test_one_pending_invitation_per_member(app, yes):
    async def main():
        never = asyncio.Event()

        async def hang(cmd):
            app.bridge.sent.append(cmd)
            await never.wait()
        app.bridge.send = lambda cmd, preserve_id=False: hang(cmd)
        app.invites.claims(chat("come here"), 540)
        await asyncio.sleep(0.01)
        app.invites.claims(chat("come here"), 541)
        assert "still sorting out" in told(app, 541, 102)
        assert len(app.bridge.sent) == 1
        await app.invites.close()
    run(main())


@pytest.mark.parametrize("phase", ["combat", "wipe_silence", "wipe_debrief"])
def test_nothing_in_his_raids_quiet(app, yes, phase, monkeypatch):
    monkeypatch.setattr(app.raid, "phase", lambda now: phase)
    async def main():
        assert app.invites.claims(chat("come here"), 550) is None
    run(main())


def test_nobody_moves_into_or_out_of_an_instance(app, yes):
    async def main():
        app.invites.claims(chat("come here", map_=631, zone=4812), 551)  # he is in ICC
        await settle(app)
        assert app.bridge.sent == [] and "inside an instance" in told(app, 551, 102)
        app.world.members[103] = Member(guid=103, name="Coros", race=2, class_=1, gender=1, level=80,
                                        rank=3, online=True, map=574, zone=206, spec_tab=-1)
        app.invites.claims(chat("come here", to=103), 552)
        await settle(app)
        assert app.bridge.sent == []
    run(main())


def test_switch_off_is_plain_talk(app, yes, monkeypatch):
    from dataclasses import replace
    app.cfg = replace(app.cfg, talk=replace(app.cfg.talk, invites=False))
    async def main():
        assert app.invites.claims(chat("come here"), 560) is None
    run(main())


def test_an_older_worldserver_meets_him_where_he_is_or_cannot_reach(app, yes):
    app.hello = Hello(ts=0, server="mod-leeroy", version="0.1.0", guild_id=0, human_guid=OWNER)
    async def main():
        app.invites.claims(chat("meet me at the bank"), 570)  # Orgrimmar's bank, he is in Orgrimmar
        await settle(app)
        assert [c.kind for c in app.bridge.sent] == ["come_to_me"]
        app.invites._pending.clear()
        app.invites.claims(chat("meet me at krasus", to=103), 571)  # Dalaran: out of reach
        await settle(app)
        assert len(app.bridge.sent) == 1 and "can't get there" in told(app, 571, 103)
    run(main())


def test_in_his_group_she_just_goes_along(app, yes):
    app.world.group = Group(ts=0, leader_guid=OWNER, raid=False, members=(OWNER, 102))
    async def main():
        app.invites.claims(chat("meet me at the inn"), 580)
        await settle(app)
        assert app.bridge.sent == [] and "in his group already" in told(app, 580, 102)
        app.invites.claims(chat("let's go fishing"), 581)
        await settle(app)
        assert [c.kind for c in app.bridge.sent] == ["fish"]
    run(main())


def test_a_discord_dm_invitation_only_agrees_to_meet_later(app, yes, monkeypatch):
    monkeypatch.setattr(app.engine, "_his_line", lambda turn, meta: "let's go fishing in dalaran")
    turn = Turn(TurnKind.REPLY_HUMAN, 102, "dm:102")
    said = app.invites.instruction(turn, TurnMeta("t", answers_human=True), "")
    assert "only agree to meet up in game later" in said
    assert app.bridge.sent == []


def test_native_codes_and_model_answers():
    assert native_code("combat") == "combat" and native_code("busy:string") == "busy:string"
    assert native_code("bad field: kind") == "unsupported" and native_code("off") == "invites_off"
    assert native_code("something odd happened") == "refused"
    assert read_answer('{"what": "fish", "sure": 0.9}') == "fish"
    assert read_answer('{"what": "fish", "sure": 0.5}') is None
    assert read_answer('{"what": "dance", "sure": 0.9}') is None
    assert read_answer("not json") is None


def test_a_line_for_the_model_is_held_and_goes_to_talk_in_order(app, yes, monkeypatch):
    released = []

    async def release(ev, line_id):
        released.append(line_id)
    monkeypatch.setattr(app, "release_to_talk", release)
    monkeypatch.setattr(svc.Invites, "_may_read", lambda self: True)

    async def classify(self, text):
        return "sit"
    monkeypatch.setattr(svc.Invites, "_classify", classify)

    async def main():
        assert app.invites.claims(chat("the inn?"), 590) == "held"
        await settle(app)
        await asyncio.sleep(0.01)
        assert released == [590]
        assert [c.kind for c in app.bridge.sent] == ["go_to"]  # the inn: she waits for him there
    run(main())


def test_his_next_line_sends_a_line_still_being_read_to_talk_first(app, yes, monkeypatch):
    released = []

    async def release(ev, line_id):
        released.append(line_id)
    monkeypatch.setattr(app, "release_to_talk", release)
    monkeypatch.setattr(svc.Invites, "_may_read", lambda self: True)
    never = asyncio.Event()

    async def classify(self, text):
        await never.wait()
    monkeypatch.setattr(svc.Invites, "_classify", classify)

    async def main():
        assert app.invites.claims(chat("the inn?"), 591) == "held"
        await asyncio.sleep(0)
        await app.invites.before_his_line()
        assert released == [591] and app.bridge.sent == []
    run(main())


def test_native_switch_off_is_said_once_then_plain_talk_until_hello(app, yes):
    async def main():
        app.bridge.answers = {"come_to_me": (False, "off", None)}
        assert app.invites.claims(chat("come here"), 600) == "invite"
        await settle(app)
        assert "can't make it right now" in told(app, 600, 102)
        assert app.invites.claims(chat("come here", to=103), 601) is None  # plain talk now
        await app.invites.on_hello(None)
        app.bridge.answers = {}
        assert app.invites.claims(chat("come here", to=103), 602) == "invite"
    run(main())


def test_naming_a_member_who_is_away_sends_nobody_else(app, yes):
    async def main():
        app.world.online.discard(103)
        assert app.invites.claims(chat("Coros come here", channel="guild"), 610) is None
        await settle(app)
        assert app.bridge.sent == []
    run(main())


def test_a_slow_reading_lets_his_line_go_to_talk_and_moves_nobody(app, yes, monkeypatch):
    released = []

    async def release(ev, line_id):
        released.append(line_id)
    monkeypatch.setattr(app, "release_to_talk", release)
    monkeypatch.setattr(svc.Invites, "_may_read", lambda self: True)

    async def ready(self):
        return True
    monkeypatch.setattr(svc.Invites, "_model_ready", ready)

    async def slow(priority, job, *, deadline):
        await app.clock.sleep(60)
        return SimpleNamespace(text='{"what": "sit", "sure": 0.95}')
    monkeypatch.setattr(app.scheduler, "submit", slow)

    async def main():
        assert app.invites.claims(chat("the inn?"), 620) == "held"
        await app.clock.advance(svc.MODEL_S + 2, 1.0)
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert released == [620] and app.bridge.sent == []
        await app.clock.advance(60, 5.0)
        await asyncio.sleep(0.01)
        assert app.bridge.sent == []  # a late reading never moves anyone
    run(main())
