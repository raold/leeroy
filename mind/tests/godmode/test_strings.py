"""D6 Part B task 4: his strings in game. One `string_do` at once, settled taken with what she
did (native's done job) or not taken with native's code, never retried; what she did reaches her
card where she is with him."""
import asyncio
import logging
from dataclasses import replace

import pytest

from conftest import run
from selfies.fakes import OWNER, mind_app
from leeroy_mind.awareness.card import CardContext, card
from leeroy_mind.bridge import AckTimeout, NotConnected
from leeroy_mind.endpoint import BadRequest, string_args
from leeroy_mind.godmode import intents as I
from leeroy_mind.godmode.service import native_code
from leeroy_mind.guild.protocol import Job, StringDo
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Ack
from leeroy_mind.storage.rows import Relationship

GODMODE = "[godmode]\nenabled=true\n"


class Wire:
    """The bridge as pull() sees it: connected, native's done job first (it is published before
    the ack), then an ack per command. answers: kind -> (ok, error, what she did) or an exception."""

    def __init__(self, app, answers=None):
        self.app, self.answers, self.sent, self.connected = app, answers or {}, [], True

    async def send(self, cmd, *, preserve_id=False):
        assert preserve_id and isinstance(cmd, StringDo)
        self.sent.append(cmd)
        answer = self.answers.get(cmd.kind, (True, "", "done"))
        if isinstance(answer, Exception):
            raise answer
        ok, error, reason = answer
        if ok and reason is not None:
            self.app.godmode.on_job(Job(ts=0, id=cmd.id, kind="string_do", state="done",
                                        reason=reason, stage="", guids=(cmd.guid,), skipped=(),
                                        kills=0, wipes=0, instance_id=0, guid=cmd.guid,
                                        level=0, avg_ilvl=0))
        return Ack(0, cmd.id, ok, error)


@pytest.fixture
def app(tmp_path):
    app = mind_app(tmp_path, toml=GODMODE)
    app.world.human_online = True
    real = app.bridge
    yield app
    app.bridge = real
    run(app.stop())


def pull(app, member, kind, **args):
    return run(app.godmode.handle({"action": "create", "member": member, "kind": kind, "args": args}))


def woman(app, guid, stance):
    old = app.personas.get(guid)
    person = replace(old, identity=replace(old.identity, player_gender="female"),
                     romance=replace(old.romance, stance=stance))
    save_persona(app.store, person, source="seed", ts=app.clock.now(), race="Orc", class_name="Warrior")
    app.personas.invalidate(guid)


@pytest.mark.parametrize("kind,args,did", [
    ("come_to_me", {}, "came"), ("join_group", {}, "joined"), ("follow", {}, "following"),
    ("duel_me", {}, "challenged"), ("emote", {"emote": "wave"}, "emoted"), ("sit", {}, "sat")])
def test_each_game_string_is_sent_once_and_taken_with_what_she_did(app, kind, args, did):
    wire = app.bridge = Wire(app, {kind: (True, "", did)})
    reply = pull(app, 102, kind, **args)
    assert reply["ok"] and (reply["state"], reply["reason"]) == ("taken", did)
    assert reply["message"] == I.outcome_words(I.get(app.store.conn, reply["id"]))
    [cmd] = wire.sent
    assert (cmd.guid, cmd.kind, cmd.target_guid, cmd.emote) == (102, kind, OWNER, args.get("emote", ""))
    row = I.get(app.store.conn, reply["id"])
    assert (row["status"], row["reason"], row["args"]) == ("taken", did, args)
    assert not app.godmode._busy and not app.godmode.jobs._jobs  # the mailbox entry is gone


def test_follow_sent_again_reports_that_she_stopped(app):
    app.bridge = Wire(app, {"follow": (True, "", "following")})
    assert pull(app, 102, "follow")["reason"] == "following"
    app.bridge = Wire(app, {"follow": (True, "", "stopped")})
    reply = pull(app, 102, "follow")
    assert (reply["state"], reply["reason"]) == ("taken", "stopped")
    assert reply["message"] == "She stopped following you and waits there."


def test_strings_refused_in_his_fight_and_copy(app):
    """Review focus 1: native refuses a string in his fight, in his group's raid or onto his copy;
    the mind settles it not_taken with native's code and nothing retries it, tick after tick."""
    wire = app.bridge = Wire(app, {"come_to_me": (False, "combat", None),
                                   "sit": (False, "in_instance", None),
                                   "follow": (False, "busy:raid_owner", None)})
    replies = [pull(app, 102, "come_to_me"), pull(app, 103, "sit"), pull(app, 101, "follow")]
    assert [(r["state"], r["reason"]) for r in replies] == [
        ("not_taken", "combat"), ("not_taken", "in_instance"), ("not_taken", "busy:raid_owner")]
    assert all(r["message"].startswith("Not taken: ") for r in replies)
    assert len(wire.sent) == 3
    for step in range(1, 25):  # ten minutes of ticks: nothing is sent again
        run(app.godmode.tick(app.clock.now() + 30 * step))
    assert len(wire.sent) == 3
    assert [I.get(app.store.conn, r["id"])["status"] for r in replies] == ["not_taken"] * 3
    assert not app.godmode.jobs._jobs


def test_native_codes_are_kept_and_its_text_never_is():
    assert native_code("combat") == "combat"
    assert native_code("busy:training_owner") == "busy:training_owner"
    assert native_code("unknown type: string_do") == "unsupported"
    assert native_code("bad field: guid") == "invalid"
    assert native_code("Some Long Sentence From A Handler") == "refused"
    assert native_code("") == "refused"


@pytest.mark.parametrize("failure,reason", [
    (AckTimeout("no ack"), "no_answer"), (NotConnected("down"), "no_link"),
    ((False, "unknown type: string_do", None), "unsupported")])
def test_the_link_failing_or_an_older_worldserver_reports_it(app, failure, reason):
    app.bridge = Wire(app, {"sit": failure})
    reply = pull(app, 102, "sit")
    assert (reply["state"], reply["reason"]) == ("not_taken", reason)
    assert not app.godmode._busy and not app.godmode.jobs._jobs


def test_a_done_ack_without_its_job_is_taken_as_done(app, monkeypatch):
    monkeypatch.setattr("leeroy_mind.godmode.service.JOB_WAIT_S", 0.01)
    app.bridge = Wire(app, {"sit": (True, "", None)})
    assert (pull(app, 102, "sit")["state"], I.get(app.store.conn, 1)["reason"]) == ("taken", "done")


def test_nothing_is_sent_when_he_is_offline_the_link_is_down_or_she_does_not_fit(app):
    wire = app.bridge = Wire(app)
    app.world.human_online = False
    assert pull(app, 102, "come_to_me")["reason"] == "human_offline"
    app.world.human_online = True
    wire.connected = False
    assert pull(app, 102, "come_to_me")["reason"] == "no_link"
    wire.connected = True
    # a duel is absurd from one who dislikes him
    app.store.put_relationship(Relationship(a=103, b=OWNER, affinity=-60))
    assert pull(app, 103, "duel_me")["reason"] == "stance"
    # a kiss only from a woman drawn to him; a wave from anyone
    assert pull(app, 101, "emote", emote="kiss")["reason"] == "stance"  # a man
    woman(app, 104, "fond")
    assert pull(app, 104, "emote", emote="kiss")["reason"] == "stance"
    assert pull(app, 104, "emote", emote="hug")["state"] == "taken"
    woman(app, 105, "lust")
    assert pull(app, 105, "emote", emote="kiss")["state"] == "taken"
    assert pull(app, 101, "emote", emote="wave")["state"] == "taken"
    # an away member is not pulled
    app.store.conn.execute("INSERT INTO away(guid,reason,left_at) VALUES (102,'trip',?)",
                           (app.clock.now(),))
    app.store.conn.commit()
    assert pull(app, 102, "come_to_me")["reason"] == "away"
    assert [(c.guid, c.kind, c.emote) for c in wire.sent] == [
        (104, "emote", "hug"), (105, "emote", "kiss"), (101, "emote", "wave")]


def test_game_string_arguments_and_emotes(app):
    assert string_args({"action": "create", "member": 102, "kind": "come_to_me", "args": {}})
    assert string_args({"action": "create", "member": 102, "kind": "emote",
                        "args": {"emote": "wave"}})
    for bad in ({"kind": "emote", "args": {}}, {"kind": "sit", "args": {"emote": "wave"}},
                {"kind": "emote", "args": {"emote": 5}}, {"kind": "duel_me", "args": {"x": 1}}):
        with pytest.raises(BadRequest):
            string_args({"action": "create", "member": 102, **bad})
    reply = pull(app, 102, "emote", emote="spit")
    assert not reply["ok"] and reply["code"] == "bad_emote"
    reply = pull(app, 999, "sit")
    assert not reply["ok"] and reply["code"] == "member"
    assert set(I.GAME_EMOTES) >= set(I._ROMANTIC) and set(I.GAME_EMOTES) == set(I._EMOTE_DONE)


def test_a_game_string_waits_ten_minutes_at_most(app):
    """One a crash left in flight never goes again: it expires after GAME_DUE_S."""
    wire = app.bridge = Wire(app)
    ident = I._insert(app.store, 102, "sit", {}, app.clock.now())
    run(app.godmode.tick(app.clock.now() + I.GAME_DUE_S - 1))
    assert I.get(app.store.conn, ident)["status"] == "pending" and not wire.sent
    run(app.godmode.tick(app.clock.now() + I.GAME_DUE_S + 1))
    assert (I.get(app.store.conn, ident)["status"], wire.sent) == ("expired", [])


def test_taken_string_reaches_her_card_where_she_is_with_him(app):
    """Review focus 3 for strings: what her body did is on her card, as her own past, in his
    group's chat, near him, a whisper or her DM with him; nowhere else, and only for half an hour."""
    app.bridge = Wire(app, {"join_group": (True, "", "joined"), "emote": (True, "", "emoted"),
                            "duel_me": (False, "too_far", None), "sit": (False, "human_dead", None)})
    pull(app, 102, "join_group")
    now = app.clock.now()
    conn = app.store.conn
    line = "You joined his group because he asked."
    for channel, to in (("party", 0), ("raid", 0), ("say", 0), ("whisper", OWNER), ("dm:102", OWNER)):
        assert I.card_lines(conn, 102, channel, to_guid=to, human=OWNER, now=now) == [line], channel
    for channel, to in (("guild", 0), ("officer", 0), ("whisper", 103), ("discord:general", 0),
                        ("dm:103", OWNER), ("gdm:1", 0)):
        assert I.card_lines(conn, 102, channel, to_guid=to, human=OWNER, now=now) == [], channel
    assert I.card_lines(conn, 102, "party", human=OWNER) == []  # no clock, no game lines
    assert I.card_lines(conn, 102, "party", human=OWNER, now=now + I.GAME_CARD_S + 1) == []
    assert I.card_lines(conn, 103, "party", human=OWNER, now=now) == []
    # an emote in her own words; a refusal she knew of; one she could not have known of says nothing
    pull(app, 103, "emote", emote="wave")
    pull(app, 103, "duel_me")
    pull(app, 103, "sit")
    assert I.card_lines(conn, 103, "say", human=OWNER, now=app.clock.now()) == [
        "He asked you for a duel, but you couldn't just then.", "You waved at him because he asked."]
    # through the service, on the game card's game part
    lines = app.godmode.card_lines(102, "whisper", to_guid=OWNER)
    assert lines == [line]
    game = card(conn, 102, context=CardContext(channel="whisper", to_guid=OWNER, intents=tuple(lines)))
    assert line in game.parts[0].render()


def test_her_card_carries_it_through_the_live_review(app):
    from leeroy_mind.llm.scheduler import Priority
    from leeroy_mind.talk.pipeline import TurnMeta
    from leeroy_mind.talk.turns import Turn, TurnKind
    app.engine.cfg = replace(app.engine.cfg, talk=replace(app.engine.cfg.talk, aware=True))
    app.bridge = Wire(app, {"come_to_me": (True, "", "came")})
    pull(app, 102, "come_to_me")
    turn = Turn(TurnKind.REPLY_HUMAN, 102, "whisper", to_guid=OWNER, priority=Priority.REPLY_HUMAN)
    block = app.engine.live_review.block(turn, TurnMeta("t1", review_required=True), close=False)
    assert "You went over to him because he asked you to come." in block
    guild = Turn(TurnKind.REPLY_HUMAN, 102, "guild", priority=Priority.REPLY_HUMAN)
    assert "because he asked" not in app.engine.live_review.block(
        guild, TurnMeta("t2", review_required=True), close=False)


def test_strings_log_ids_kinds_and_codes_only(app, caplog):
    caplog.set_level(logging.INFO)
    app.bridge = Wire(app, {"emote": (True, "", "emoted"), "sit": (False, "combat", None)})
    pull(app, 104, "emote", emote="dance")
    pull(app, 104, "sit")
    text = caplog.text
    assert "godmode_intent_settled" in text and "reason=combat" in text
    assert "dance" not in text and "Dunna" not in text


def test_an_unknown_job_never_feeds_the_mailbox(app):
    app.godmode.jobs.expect("abc")
    app.godmode.on_job(Job(ts=0, id="abc", kind="bot_do", state="done", reason="", stage="",
                           guids=(), skipped=(), kills=0, wipes=0, instance_id=0, guid=0,
                           level=0, avg_ilvl=0))
    assert app.godmode.jobs._jobs["abc"] is None
    assert run(asyncio.wait_for(app.godmode.jobs.wait("abc", 0), 1)) is None


def test_string_do_crosses_the_real_bridge(tmp_path, clock):
    """A real MindApp and BridgeClient on the FakeBridge: the wire carries the command as the
    fixture does, and native's done job (here after its ack) names what she did."""
    from test_app import CAST, hello, running
    from conftest import HUMAN
    from leeroy_mind.godmode.attach import attach
    near = {}

    def ack(cmd):
        if cmd.get("type") == "string_do":
            near["loop"].create_task(near["bridge"].push({
                "type": "job", "id": cmd["id"], "kind": "string_do", "state": "done",
                "reason": "came", "stage": "", "guids": [cmd["guid"]], "skipped": [], "kills": 0,
                "wipes": 0, "instance_id": 0, "guid": cmd["guid"], "level": 0, "avg_ilvl": 0}))
        return True, ""

    def prepare(app):
        with (tmp_path / "mind.toml").open("a", encoding="utf-8") as f:
            f.write("\n" + GODMODE)
        app.godmode = attach(app)

    async def scenario():
        async with running(tmp_path, clock, events=[hello(HUMAN), CAST], prepare=prepare,
                           bridge_kw={"ack": ack}) as h:
            near["bridge"], near["loop"] = h.bridge, asyncio.get_running_loop()
            await h.wait(lambda: h.app.world.human_online)
            reply = await h.app.godmode.handle({"action": "create", "member": 102,
                                                "kind": "come_to_me", "args": {}})
            assert (reply["state"], reply["reason"]) == ("taken", "came"), reply
            [cmd] = [c for c in h.bridge.commands if c["type"] == "string_do"]
            assert {k: cmd[k] for k in ("guid", "kind", "target_guid", "emote")} == {
                "guid": 102, "kind": "come_to_me", "target_guid": HUMAN, "emote": ""}
    run(scenario())
