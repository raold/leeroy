"""The running mind's side of intents (godmode.intents): the `string` op, a 30-second tick that
expires, checks and hands out pending Discord intents, and the hooks that settle them.

Attached only with [godmode] enabled (godmode.attach); with it off nothing here exists and the
seeds, cards, openers and reactions are as on main. Logs carry ids, kinds and codes only."""
import asyncio
import logging

from leeroy_mind.logs import log as log_event

from . import intents as I

log = logging.getLogger(__name__)
TICK_S = 30.0


class Godmode:
    def __init__(self, app, config):
        self.app, self.config = app, config
        self._closed = False
        self._busy: set[int] = set()  # intents handed to a reaction or an opener right now
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._missed: set[int] = set()  # bring_up openers posted without naming the topic

    # -- the console's op --------------------------------------------------------------------

    @property
    def server(self):
        return getattr(self.app, "server", None)

    async def handle(self, args: dict) -> dict:
        app, now = self.app, self.app.clock.now()
        action = args["action"]
        if action == "list":
            return {"ok": True, "intents": I.listing(app.store.conn, member=args.get("member"))}
        if action == "cancel":
            done = I.settle(app.store, args["id"], status="cancelled", reason="owner", now=now)
            return {"ok": True, "id": args["id"],
                    "message": "Cancelled." if done else "It had already settled."}
        try:
            pic = app.tuning.picture()
            ident = I.create(app.store, member=args["member"], kind=args["kind"],
                             args=args["args"], human=app.engine.human, now=now, pic=pic,
                             server=self.server is not None)
        except I.IntentRefused as e:
            log_event("godmode_intent_refused", kind=args["kind"], code=e.code)
            return {"ok": False, "code": e.code, "error": str(e)}
        return {"ok": True, "id": ident, "message": f"Saved as string #{ident}."}

    # -- what her turns read -----------------------------------------------------------------

    def card_lines(self, guid, channel, *, to_guid=0, conn=None) -> list[str]:
        if self._closed:
            return []
        conn = self.app.store.conn if conn is None else conn
        return I.card_lines(conn, guid, channel, to_guid=to_guid, human=self.app.human_guid)

    def seed_valid(self, seed) -> bool:
        """An intent seed's conversation goes on only while its intent is still waiting."""
        ident = intent_of(seed)
        row = I.get(self.app.store.conn, ident) if ident else None
        return row is not None and row["status"] == "pending" and row["due"] > self.app.clock.now()

    def taken(self, ident, message, now) -> None:
        """DB-only, inside the post's own transaction: the intent is taken by this message, and
        a thread intent opens its thread on it."""
        store = self.app.store
        row = I.get(store.conn, ident)
        if row is None or row["status"] != "pending":
            return
        if row["kind"] == "bring_up":
            text = store.conn.execute("SELECT text FROM discord_message WHERE id=?",
                                      (message,)).fetchone()
            if text is None or not I.mentions(row["args"]["topic"], text[0]):
                self._missed.add(ident)  # posted, but not about it: the seed tries again later
                return
        if row["kind"] == "thread":
            from leeroy_mind.discord import store as dstore
            try:
                dstore.open_thread(store, message, name=row["args"]["name"], now=now)
            except dstore.DiscordRefused:
                return
        store.conn.execute("UPDATE godmode_intent SET status='taken',reason='sent',settled=? "
                           "WHERE id=? AND status='pending'", (now, ident))
        log_event("godmode_intent_settled", id=ident, status="taken", reason="sent")

    def opened(self, seed, posted: bool) -> None:
        """The engine's word after an intent seed's opener: one that never posted (dropped), or
        a bring_up that posted without naming its topic, backs the intent off (I.tried)."""
        ident = intent_of(seed)
        if ident is None or not I.waiting(self.app.store.conn, ident):
            self._missed.discard(ident)
            return
        missed = ident in self._missed
        self._missed.discard(ident)
        if not posted or missed:
            I.tried(self.app.store, ident, now=self.app.clock.now(),
                    reason="not_mentioned" if missed else "dropped")

    def on_line(self, line_id, chat) -> None:
        """Every line the mind said: a Discord line of hers that mentions a pending bring_up's
        topic (in its scope), or her post/DM about a post or dm_him intent, takes it. Group DMs
        and game lines never do."""
        if self._closed:
            return
        from leeroy_mind.discord.channels import channel_id, is_discord, is_group
        if not is_discord(chat.channel) or is_group(chat.channel):
            return
        cid = channel_id(chat.channel)
        try:
            rows = I.pending(self.app.store.conn, chat.from_guid)
        except Exception:
            return
        for i in rows:
            words = i["args"].get("topic") or i["args"].get("about") or ""
            if (I.in_place(i, cid, to_guid=chat.to_guid, human=self.app.human_guid)
                    and I.mentions(words, chat.text)):
                I.settle(self.app.store, i["id"], status="taken", reason="mentioned",
                         now=self.app.clock.now())

    # -- the tick ----------------------------------------------------------------------------

    async def run(self):
        while not self._closed:
            try:
                await self.tick(self.app.clock.now())
            except Exception as error:
                log.warning("godmode_tick_failed code=%s", type(error).__name__)
            await self.app.clock.sleep(TICK_S)

    async def tick(self, now):
        if self._lock.locked():
            return
        async with self._lock:
            store = self.app.store
            for i in I.pending(store.conn):
                if i["due"] <= now:
                    I.settle(store, i["id"], status="expired", reason="due", now=now)
                    continue
                if i["id"] in self._busy:
                    continue
                ok, reason = I.fit(store.conn, i, self.app.personas, now=now)
                if not ok:
                    I.settle(store, i["id"], status="not_taken", reason=reason, now=now)
                    continue
                server = self.server
                if server is None:
                    continue
                if i["kind"] == "react" and not getattr(server.config, "reactions", False):
                    I.settle(store, i["id"], status="not_taken", reason="reactions_off", now=now)
                elif i["kind"] == "react" and server.reactions is not None:
                    self._hand(i, server.reactions.intent_react(i, settle=self.react_settle))
                elif i["kind"] == "dm_him" and getattr(server.config, "dms_per_day", 1) <= 0:
                    I.settle(store, i["id"], status="not_taken", reason="dms_off", now=now)
                elif i["kind"] == "dm_him" and self.app.engine.cfg.talk.chattiness <= 0:
                    I.settle(store, i["id"], status="not_taken", reason="chattiness_off", now=now)
                elif (i["kind"] == "dm_him" and server.dms is not None
                      and I.may_try(store.conn, i["id"], now)):
                    opener = server.dms.intent_opener(i, settled=self.taken)
                    if opener is not None:
                        self._hand(i, self._dm(i, opener))

    async def _dm(self, intent, opener):
        """A dm_him opener; one that ran and sent nothing backs the intent off."""
        ran = await opener
        if ran and I.waiting(self.app.store.conn, intent["id"]):
            I.tried(self.app.store, intent["id"], now=self.app.clock.now())

    def _hand(self, intent, coro):
        if coro is None:
            return
        ident = intent["id"]
        self._busy.add(ident)
        task = asyncio.create_task(coro, name="godmode-intent")
        self._tasks.add(task)

        def done(t, ident=ident):
            self._busy.discard(ident)
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.warning("godmode_intent_failed id=%d code=%s", ident,
                            type(t.exception()).__name__)
        task.add_done_callback(done)

    def react_settle(self, ident, now) -> None:
        """DB-only, inside the reaction's transaction."""
        self.app.store.conn.execute("UPDATE godmode_intent SET status='taken',reason='reacted',"
                                    "settled=? WHERE id=? AND status='pending'", (now, ident))
        log_event("godmode_intent_settled", id=ident, status="taken", reason="reacted")

    async def close(self):
        self._closed = True
        tasks = tuple(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def intent_of(seed) -> int | None:
    """The intent id an `intent` seed carries (key intent:<id>:<kind>)."""
    if getattr(seed, "kind", "") != "intent":
        return None
    parts = seed.key.split(":")
    return int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else None


def intent_kind(seed) -> str:
    parts = seed.key.split(":")
    return parts[2] if len(parts) == 3 else ""
