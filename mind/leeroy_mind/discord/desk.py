"""His Discord actions: the control endpoint's `discord` op (endpoint.discord_args checks and
cleans the arguments), then DiscordDesk.handle, which writes through discord.store as him, origin
human. None of it needs him in game. Logs carry the action, the channel id and the outcome, never
a text, an emoji or a name."""
from leeroy_mind.discord import layout
from leeroy_mind.discord import store as dstore
from leeroy_mind.discord.store import DiscordRefused
from leeroy_mind.logs import log as log_event

STOPPING = "The mind is stopping."
NO_HUMAN = "The mind doesn't know who you are yet."
NOT_MEMBER = "That member isn't in the guild's cast, so they can't be messaged."
NO_PERSONA = "That member has no persona, so they can't write back."

WORDS = {
    "empty": "There's nothing to send.",
    "too_long": "That's too long (2000 characters at most).",
    "deleted": "That message was deleted.",
    "not_yours": "That message isn't yours.",
    "not_emoji": "That isn't a single emoji.",
    "too_many": "That message has 20 different reactions already.",
    "too_many_pins": "That channel has 50 pinned messages already.",
    "exists": "That message already has a thread.",
    "bad_name": "A thread name is 1 to 60 characters.",
    "no_parent": "The message you are replying to isn't in this channel.",
    "deleted_parent": "The message you are replying to was deleted.",
    "missing": "There is no such message.",
    "no_thread": "That thread isn't in this channel.",
    "bad_origin": "That message can't be posted that way.",
    "bad_channel": "There is no such channel.",
}


class _Refused(Exception):
    def __init__(self, outcome: str, words: str):
        super().__init__(words)
        self.outcome = outcome


class DiscordDesk:
    """The running mind's side of the `discord` op (endpoint.ControlEndpoint.discord runs one at
    a time). {"ok": True[, "id"]} or {"ok": False, "error"}, in the console's words."""

    def __init__(self, app):
        self.app = app

    async def handle(self, args: dict) -> dict:
        action, channel = args.get("action"), args.get("channel", "")
        try:
            reply = self._handle(args)
            outcome = "ok"
        except DiscordRefused as e:
            reply = {"ok": False, "error": WORDS.get(e.code, "That can't be done.")}
            outcome = e.code
        except _Refused as e:
            reply, outcome = {"ok": False, "error": str(e)}, e.outcome
        finally:
            store = getattr(self.app, "store", None)
            if store is not None and not store.in_unit:
                store.conn.commit()  # end a read outside a unit, so it never pins a snapshot
        log_event("discord", action=action, channel=channel, outcome=outcome)
        return reply

    def _handle(self, args: dict) -> dict:
        app = self.app
        if app.stopping:
            raise _Refused("stopping", STOPPING)
        me = app.human_guid
        if not (type(me) is int and me > 0):
            raise _Refused("no_human", NO_HUMAN)
        store, now, action = app.store, app.clock.now(), args["action"]
        if action == "post":
            self._check_dm(args["channel"], me)
            mid = dstore.post(store, channel=args["channel"], author=me, origin="human",
                              text=args["text"], reply_to=args.get("reply_to"),
                              thread=args.get("thread"), now=now)
            hook = getattr(app, "discord_talk", None)
            if hook is not None:
                hook.on_his_post(mid)
            return {"ok": True, "id": mid}
        message = args["message"]
        if action == "thread":
            thread = dstore.open_thread(store, message, name=args["name"], now=now)
            self._his_action()
            return {"ok": True, "id": thread}
        if action == "react":
            dstore.react(store, message, member=me, emoji=args["emoji"], now=now)
        elif action == "unreact":
            dstore.unreact(store, message, member=me, emoji=args["emoji"], now=now)
        elif action == "edit":
            dstore.edit(store, message, author=me, text=args["text"], now=now)
        elif action == "delete":
            dstore.delete(store, message, author=me, now=now)
        elif action == "pin":
            dstore.pin(store, message, now=now)
        else:
            dstore.unpin(store, message)
        self._his_action()
        return {"ok": True}

    def _his_action(self) -> None:
        """An action of his other than a post keeps his Discord lease (discord.talk)."""
        touch = getattr(getattr(self.app, "discord_talk", None), "on_his_action", None)
        if touch is not None:
            touch()

    def _check_dm(self, channel: str, me: int) -> None:
        peer = layout.dm_peer(channel)
        if peer is None:
            return
        conn = self.app.store.conn
        if peer == me or not conn.execute("SELECT 1 FROM cast_member WHERE guid = ?",
                                          (peer,)).fetchall():
            raise _Refused("not_member", NOT_MEMBER)
        if not conn.execute("SELECT 1 FROM persona_core WHERE guid = ?", (peer,)).fetchall():
            raise _Refused("no_persona", NO_PERSONA)
