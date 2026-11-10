"""His Discord actions: the strict arguments (endpoint.discord_args) and the desk that writes them
as him. Scratch databases and invented text only."""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from conftest import HUMAN, OFFICERS, seed_test_cast
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.desk import WORDS, DiscordDesk
from leeroy_mind.endpoint import BadRequest, discord_args
from leeroy_mind.memory.threads import ThreadTracker
from leeroy_mind.talk.world import WorldView

SECRET = "INVENTEDDISCORDTEXT"


class Talk:
    def __init__(self):
        self.seen = []

    def on_his_post(self, message_id):
        self.seen.append(message_id)


def desk_app(store, clock, **kw):
    """What the desk uses of the app: the store and clock, his guid, the world (for a line's
    audience) and the app's threading hook (MindApp.on_his_discord_line), over a real
    ThreadTracker."""
    world = WorldView(is_officer=lambda g: g in OFFICERS, discord_roster=store.cast)
    world.human_guid = HUMAN
    threads = ThreadTracker(store)
    a = SimpleNamespace(store=store, clock=clock, stopping=False, human_guid=HUMAN, world=world,
                        threads=threads, closed=[], **kw)

    def on_his_discord_line(line, chat, ts):
        if (closed := threads.add(line, chat, ts)) is not None:
            a.closed.append(closed)
    a.on_his_discord_line = on_his_discord_line
    a.discord = DiscordDesk(a)
    return a


@pytest.fixture
def app(store, clock):
    seed_test_cast(store)
    return desk_app(store, clock, discord_talk=Talk())


def ask(app, **args):
    return asyncio.run(app.discord.handle(discord_args(args)))


def test_a_post_is_stored_as_his_and_returns_its_id(app):
    r = ask(app, action="post", channel="general", text="  hello  ")
    assert r["ok"] is True
    [m] = dstore.messages(app.store.conn, "general")
    assert (m["id"], m["author"], m["origin"], m["text"]) == (r["id"], HUMAN, "human", "hello")
    assert app.discord_talk.seen == [r["id"]]
    t = ask(app, action="thread", message=r["id"], name="the plan")
    assert t["ok"] and isinstance(t["id"], int)
    p = ask(app, action="post", channel="general", text="in it", thread=t["id"], reply_to=r["id"])
    assert p["ok"] and app.discord_talk.seen == [r["id"], p["id"]]
    assert ask(app, action="react", message=r["id"], emoji="👍") == {"ok": True}
    assert ask(app, action="pin", message=r["id"]) == {"ok": True}
    assert ask(app, action="unpin", message=r["id"]) == {"ok": True}
    assert ask(app, action="edit", message=r["id"], text="hi") == {"ok": True}
    assert ask(app, action="delete", message=r["id"]) == {"ok": True}
    assert app.discord_talk.seen == [r["id"], p["id"]]  # only posts reach the talk hook


def test_a_reaction_he_adds_twice_is_stored_once(app):
    a = ask(app, action="post", channel="general", text="hi")["id"]
    assert ask(app, action="react", message=a, emoji="🎉") == {"ok": True}
    assert ask(app, action="react", message=a, emoji="🎉") == {"ok": True}
    assert app.store.conn.execute("SELECT count(*) FROM discord_reaction").fetchone()[0] == 1
    assert ask(app, action="unreact", message=a, emoji="🎉") == {"ok": True}
    assert app.store.conn.execute("SELECT count(*) FROM discord_reaction").fetchone()[0] == 0


def test_editing_a_members_message_is_refused_in_words(app):
    m = dstore.post(app.store, channel="general", author=101, origin="mind", text="mine",
                    now=1.0)
    r = ask(app, action="edit", message=m, text="not yours")
    assert r == {"ok": False, "error": WORDS["not_yours"]}
    assert r["error"].endswith(".") and r["error"] != "not_yours"
    assert ask(app, action="delete", message=m)["ok"] is False


def test_a_dm_to_a_non_member_is_refused(app, store):
    assert ask(app, action="post", channel="dm:101", text="psst")["ok"] is True
    for peer in (999, 1):
        r = ask(app, action="post", channel=f"dm:{peer}", text="psst")
        assert r["ok"] is False and r["error"]
    store.conn.execute("DELETE FROM persona_core WHERE guid = 102")
    store.conn.commit()
    assert ask(app, action="post", channel="dm:102", text="psst")["ok"] is False
    assert store.conn.execute(
        "SELECT count(*) FROM discord_message WHERE channel != 'dm:101'").fetchone()[0] == 0


def test_a_stopping_mind_refuses(app):
    app.stopping = True
    assert ask(app, action="post", channel="general", text="x") == {
        "ok": False, "error": "The mind is stopping."}


def test_every_refusal_code_has_a_sentence():
    codes = {"deleted", "not_yours", "not_emoji", "too_many", "too_many_pins", "exists",
             "bad_name", "no_parent", "deleted_parent", "missing", "no_thread", "bad_origin",
             "bad_channel", "empty", "too_long"}
    assert codes <= set(WORDS)
    assert WORDS["deleted"] == "That message was deleted."
    assert WORDS["too_many"] == "That message has 20 different reactions already."
    for s in WORDS.values():
        assert s[0].isupper() and s.endswith(".")


def test_store_refusals_come_back_in_words(app):
    a = ask(app, action="post", channel="general", text="hi")["id"]
    ask(app, action="delete", message=a)
    assert ask(app, action="react", message=a, emoji="👍") == {
        "ok": False, "error": WORDS["deleted"]}
    assert ask(app, action="pin", message=999)["error"] == WORDS["missing"]


@pytest.mark.parametrize("args", [
    {"action": "nope"},
    {"action": "post", "channel": "general", "text": "x", "extra": 1},
    {"action": "post", "channel": "general"},
    {"action": "post", "channel": "nowhere", "text": "x"},
    {"action": "post", "channel": "dm:abc", "text": "x"},
    {"action": "post", "channel": "general", "text": "   "},
    {"action": "post", "channel": "general", "text": "x" * 2001},
    {"action": "post", "channel": "general", "text": "x", "reply_to": True},
    {"action": "react", "message": 1, "emoji": "👍👍"},
    {"action": "react", "message": 1, "emoji": "a"},
    {"action": "react", "message": 0, "emoji": "👍"},
    {"action": "edit", "message": 1, "text": ""},
    {"action": "thread", "message": 1, "name": " "},
    {"action": "thread", "message": 1, "name": "n" * 61},
    {"action": "delete", "message": "1"},
])
def test_bad_shapes_are_bad_requests(args):
    with pytest.raises(BadRequest) as e:
        discord_args(args)
    assert str(e.value)


def test_args_are_cleaned_and_exact():
    assert discord_args({"action": "post", "channel": "dm:101", "text": " a\r\n\n\n\n\nb ",
                         "reply_to": 4}) == {
        "action": "post", "channel": "dm:101", "text": "a\n\n\nb", "reply_to": 4}
    assert discord_args({"action": "thread", "message": 2, "name": " the  plan "}) == {
        "action": "thread", "message": 2, "name": "the plan"}
    family = "\U0001F468‍\U0001F469‍\U0001F467"
    assert discord_args({"action": "react", "message": 2, "emoji": family})["emoji"] == family


def test_logs_carry_no_text_or_emoji(app, caplog):
    caplog.set_level(logging.DEBUG)
    r = ask(app, action="post", channel="general", text=SECRET + " \U0001F916")
    ask(app, action="react", message=r["id"], emoji="\U0001F916")
    ask(app, action="edit", message=r["id"], text=SECRET)
    ask(app, action="post", channel="dm:999", text=SECRET)
    assert "discord" in caplog.text
    assert SECRET not in caplog.text and "\U0001F916" not in caplog.text
    actions = [record.leeroy_fields for record in caplog.records
               if getattr(record, "leeroy_event", None) == "discord"]
    assert all(row["message"] == r["id"] for row in actions if row["action"] in ("react", "edit"))


def his_lines(store):
    return [dict(r) for r in store.conn.execute(
        "SELECT id, ts, channel, from_guid, from_name, to_guid, text, session_id, origin, "
        "audience, thread_id FROM chat_line WHERE origin = 'human' ORDER BY id")]


def test_his_post_is_also_his_chat_line(app, store, clock):
    """Ruling R7: his post is a chat line of his as the members' answers are, on the talk's
    channel, to a DM's member, with the world's audience, threaded by the app's hook."""
    store.set_meta("human_name", "Testmage")
    g = ask(app, action="post", channel="general", text="  raid at eight\r\nbring flasks  ")
    d = ask(app, action="post", channel="dm:102", text="dont tell anyone but i might quit")
    o = ask(app, action="post", channel="officer-chat", text="bank is low")
    general, dm, officers = his_lines(store)
    stored = {m["id"]: m["text"] for c in ("general", "dm:102", "officer-chat")
              for m in dstore.messages(store.conn, c)}
    assert [(x["channel"], x["to_guid"], x["text"]) for x in (general, dm, officers)] == [
        ("discord:general", 0, stored[g["id"]]), ("dm:102", 102, stored[d["id"]]),
        ("discord:officer-chat", 0, stored[o["id"]])]
    assert general["text"] == "raid at eight\nbring flasks"
    assert all(x["from_guid"] == HUMAN and x["from_name"] == "Testmage" and x["ts"] == clock.now()
               and x["session_id"] is None for x in (general, dm, officers))
    assert json.loads(general["audience"]) == [HUMAN, 101, 102, 103, 104, 105]
    assert json.loads(dm["audience"]) == [HUMAN, 102]
    assert json.loads(officers["audience"]) == [HUMAN, *sorted(OFFICERS)]
    keys = dict(store.conn.execute("SELECT id, key FROM chat_thread"))
    assert [keys[x["thread_id"]] for x in (general, dm, officers)] == [
        "discord:general", "dm:102", "discord:officer-chat"]


def test_his_post_moves_no_game_clock(app, store, clock):
    """Ruling R11: the line has no session and leaves the world's history, his activity and
    every member's last line alone."""
    sid = store.start_session(clock.now() - 60)
    world = app.world
    before = (world.human_last_active, world.human_zone, dict(world.last_spoke))
    ask(app, action="post", channel="dm:101", text="you around?")
    ask(app, action="post", channel="general", text="anyone?")
    assert not world.history
    assert (world.human_last_active, world.human_zone, dict(world.last_spoke)) == before
    assert store.session_last_activity(sid) == clock.now() - 60
    assert all(x["session_id"] is None for x in his_lines(store))


def test_a_refused_post_stores_no_line(app, store, clock):
    a = ask(app, action="post", channel="general", text="hi")["id"]
    ask(app, action="delete", message=a)
    assert ask(app, action="post", channel="general", text="re", reply_to=a)["ok"] is False
    assert ask(app, action="post", channel="dm:999", text="psst")["ok"] is False
    assert ask(app, action="react", message=a, emoji="\U0001F44D")["ok"] is False
    assert [x["text"] for x in his_lines(store)] == ["hi"]  # the first post's line stays
    assert store.conn.execute("SELECT count(*) FROM discord_message").fetchone()[0] == 1


def test_his_line_and_its_message_are_one_write(app, store, monkeypatch):
    """A chat line that cannot be stored takes his message with it: no post without its line."""
    def broken(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(store, "insert_chat_line", broken)
    with pytest.raises(RuntimeError):
        asyncio.run(app.discord.handle(discord_args({"action": "post", "channel": "general",
                                                     "text": "hi"})))
    assert store.conn.execute("SELECT count(*) FROM discord_message").fetchone()[0] == 0
    assert app.discord_talk.seen == []
