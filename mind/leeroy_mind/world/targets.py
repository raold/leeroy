"""Complete native snapshots only. Failed/partial observations never retire anyone."""
import asyncio
import uuid

from leeroy_mind.logs import log
from leeroy_mind.protocol import Member
from .config import load
from .protocol import GetWorldRoster, MAX_MEMBERS

FRESH = 3600
_refusal_logged = False


def roster(store, *, active=True):
    try:
        return {r["guid"]:dict(r) for r in store.conn.execute(
            "SELECT * FROM world_member" + (" WHERE retired=0" if active else "") + " ORDER BY guild_id,rank,guid")}
    except Exception as exc:
        import sqlite3
        if isinstance(exc,sqlite3.OperationalError) and "no such table" in str(exc):
            return {}
        raise


def member(row):
    return Member(row["guid"],row["name"],row["race"],row["class"],row["gender"],row["level"],
                  row["rank"],bool(row["online"]),row["map"],row["zone"],-1)


def same_member(before, after):
    """A refresh may change presence while a persona is being generated."""
    fields = ("guid", "account", "guild_id", "guild_name", "name", "race", "class", "gender", "level", "rank", "retired")
    return after is not None and all(before[k] == after[k] for k in fields)


def fresh(store,now):
    try:
        observed = float(store.get_meta("world:observed") or "-inf")
        for current,seen in (("guild_id","world:own_guild"),("human_guid","world:human")):
            known = store.get_meta(current)
            if known not in (None,"0","") and known != store.get_meta(seen):
                return False
        return 0 <= now-observed <= FRESH
    except ValueError:
        return False


def save(store, members, *, own, human, now):
    if len(members) > MAX_MEMBERS or len({m.guid for m in members}) != len(members):
        raise ValueError("world_snapshot_invalid")
    with store.transaction():
        store.conn.execute("UPDATE world_member SET retired=1")
        for m in members:
            if m.guid == human or m.guild_id == own:
                raise ValueError("world_snapshot_scope")
            store.conn.execute("INSERT OR REPLACE INTO world_member VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (m.guid,m.guild_id,m.guild_name,m.account,m.name,m.race,m.class_,m.gender,m.level,
                 m.rank,int(m.online),m.map,m.zone,now))
        store.set_meta("world:observed",str(now))
        store.set_meta("world:own_guild",str(own))
        store.set_meta("world:human",str(human))
    log("world_roster_saved",members=len(members))


class Targets:
    def __init__(self,app):
        self.app=app
        self.identity=None
        self.pending=None
        self.wake=asyncio.Event()
        self.lock=asyncio.Lock()
        self.connected=False

    def enabled(self):
        return load(self.app.data_dir/"mind.toml").enabled

    async def connection(self,connected):
        self.connected=connected
        if not connected:
            self.identity=None
            if self.pending and not self.pending["future"].done():
                self.pending["future"].set_exception(ConnectionError("world_disconnected"))

    async def hello(self,event):
        if not self.app.is_backlog(event) and self.enabled():
            # Hello.human_guid is zero while he is offline, not a configured identity.
            self.identity=(event.guild_id,event.human_guid)
            self.connected=True
            self.wake.set()

    async def event(self,event):
        pending=self.pending
        if (not pending or not self.identity or not self.enabled() or self.app.is_backlog(event)
            or event.request != pending["id"] or event.own_guild != self.identity[0]
            or self.identity[1] not in (0,event.human)):
            return
        header=(event.pages,event.total,event.human)
        if pending["header"] not in (None,header) or event.page in pending["pages"]:
            pending["invalid"] = True
            if not pending["future"].done():
                pending["future"].set_exception(ValueError("world_pages_invalid"))
            return
        pending["header"]=header
        pending["pages"][event.page]=event.members
        if len(pending["pages"]) == event.pages and not pending["future"].done():
            values=tuple(m for page in range(event.pages) for m in pending["pages"][page])
            if len({m.guid for m in values}) != event.total:
                pending["future"].set_exception(ValueError("world_members_invalid"))
            else:
                pending["future"].set_result(values)

    async def refresh(self):
        global _refusal_logged
        async with self.lock:
            if not self.enabled() or not self.connected or not self.identity:
                return False
            identity=self.identity
            future=asyncio.get_running_loop().create_future()
            pending={"id":uuid.uuid4().hex,"header":None,"pages":{},"future":future,"invalid":False}
            self.pending=pending
            try:
                async with asyncio.timeout(15):
                    ack=await self.app.bridge.send(GetWorldRoster("world-roster",pending["id"]))
                    if not ack.ok:
                        if not _refusal_logged:
                            _refusal_logged = True
                            codes = {"realm_off", "world_identity_unavailable", "world_auction_unavailable", "world_roster_capacity", "world_roster_invalid"}
                            log("world_roster_refused",code=ack.error if ack.error in codes else "unsupported_or_refused")
                        return False
                    rows=await future
                if not pending["invalid"] and self.enabled() and self.connected and self.identity == identity:
                    save(self.app.store,rows,own=identity[0],human=pending["header"][2],now=self.app.clock.now())
                    return True
            except Exception:
                log("world_roster_unavailable")
            finally:
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()  # consume a refused page even when the ACK arrived first
                self.pending=None
            return False

    async def run(self):
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(),300)
            except TimeoutError:
                pass
            self.wake.clear()
            await self.refresh()
