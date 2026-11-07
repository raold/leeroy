"""His Discord actions: the strict arguments (endpoint.discord_args) and the desk that writes them
as him. Scratch databases and invented text only."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

from conftest import HUMAN, seed_test_cast
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.desk import WORDS, DiscordDesk
from leeroy_mind.endpoint import BadRequest, discord_args

SECRET = "INVENTEDDISCORDTEXT"


class Talk:
    def __init__(self):
        self.seen = []

    def on_his_post(self, message_id):
        self.seen.append(message_id)


@pytest.fixture
def app(store, clock):
    seed_test_cast(store)
    a = SimpleNamespace(store=store, clock=clock, stopping=False, human_guid=HUMAN,
                        discord_talk=Talk())
    a.discord = DiscordDesk(a)
    return a


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
