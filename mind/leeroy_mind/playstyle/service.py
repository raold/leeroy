"""Bounded event-triggered style requests and retained cleanup receipts; native is authoritative."""
import asyncio
import json
import tomllib
import uuid

from leeroy_mind.guild.ports import JobWaiter
from leeroy_mind.guild.protocol import BotDo, register
from leeroy_mind.logs import log
from leeroy_mind.protocol import Boss, Cast, Group, Hello, Login, Logout
from leeroy_mind.wow import BOSS_NAMES, CAST_MIN_LEVEL, DUNGEONS
from . import style

KEY = 'playstyle:owned'
LIMIT = 512
INSTANCE_MAPS = frozenset(map_id for map_id,boss_id in BOSS_NAMES) | frozenset(DUNGEONS)


def enabled(path):
    try:
        table = tomllib.loads(path.read_text(encoding='utf-8-sig')).get('playstyle', {})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    return (isinstance(table,dict) and not set(table)-{'enabled'}
            and type(table.get('enabled',False)) is bool and table.get('enabled',False))


def _saved(store):
    try:
        value = json.loads(store.get_meta(KEY) or '[]')
        if not isinstance(value,list) or len(value)>LIMIT or any(type(g) is not int or not 0<g<=0xFFFFFFFF for g in value):
            raise ValueError
        return set(value)
    except (ValueError,TypeError,RecursionError):
        # Do not treat malformed ownership as permission for fresh additions.
        return None


class PlayStyles:
    def __init__(self, app):
        self.app = app
        self.owned = _saved(app.store)
        self.on = enabled(app.data_dir/'mind.toml')
        self.supported = False
        self.connected = False
        self.pending = {}  # guid -> restore-only; deduplicated, capped
        self.fights = {}  # exact (map,instance,boss) -> members
        self.seen = set()
        self.group_members = set()
        self.jobs = JobWaiter(capacity=4)
        self.wake = asyncio.Event()
        self.stopping = False

    def save(self):
        with self.app.store.transaction():
            if self.owned:
                self.app.store.set_meta(KEY,json.dumps(sorted(self.owned)))
            else:
                self.app.store.conn.execute('DELETE FROM meta WHERE key=?',(KEY,))

    def queue(self,guid,restore=False):
        if (type(guid) is int and 0<guid<=0xFFFFFFFF and guid != self.app.human_guid
                and (guid in self.pending or len(self.pending)<LIMIT)):
            self.pending[guid] = restore or self.pending.get(guid,False)
            self.wake.set()

    def safe(self,guid):
        member = self.app.world.members.get(guid)
        group = self.app.world.group
        return (self.on and self.owned is not None and self.supported and self.connected and not self.stopping
                and not self.app.stopping and member is not None and member.level>=CAST_MIN_LEVEL
                and guid in self.app.world.online and member.map not in INSTANCE_MAPS
                and not any(guid in who for who in self.fights.values())
                and (group is None or not group.raid)
                and guid != self.app.human_guid)

    async def connection(self,connected):
        self.connected = connected
        if not connected:
            self.supported = False
            self.pending.clear()
            self.jobs.close()

    async def event(self,ev):
        if self.app.is_backlog(ev) or self.stopping:
            return
        if isinstance(ev,Hello):
            self.supported = 'playstyle' in ev.features
            self.on = enabled(self.app.data_dir/'mind.toml')
            self.seen.clear()
            self.group_members.clear()
            self.fights.clear() # the new native snapshot owns combat admission; old unmatched events do not
            if self.owned:
                for guid in sorted(self.owned):
                    self.queue(guid,True) # reconcile old/unknown receipts before any new style
        elif isinstance(ev,Boss):
            key = (ev.map,ev.instance_id,ev.boss_id)
            if ev.state=='engaged' and (key in self.fights or len(self.fights)<LIMIT):
                self.fights[key] = frozenset(ev.members)
            elif ev.state in ('killed','wiped'):
                self.fights.pop(key,None)
        elif isinstance(ev,Logout) and not ev.human:
            self.pending.pop(ev.guid,None) # native logout discards that retired AI journal
            self.seen.discard(ev.guid)
        elif isinstance(ev,Login):
            self.on = enabled(self.app.data_dir/'mind.toml')
            if not self.on and self.owned:
                for guid in sorted(self.owned): self.queue(guid,True)
            if not ev.human: self.queue(ev.guid)
        elif isinstance(ev,Cast):
            online = {member.guid for member in ev.members if member.online}
            for guid in sorted(online-self.seen): self.queue(guid)
            self.seen = online
        elif isinstance(ev,Group):
            members = set(ev.members)
            for guid in sorted(members-self.group_members if not ev.raid else members):
                self.queue(guid,ev.raid and bool(self.owned and guid in self.owned))
            self.group_members = members

    async def command(self,guids,restore):
        if not self.connected or not self.supported or self.owned is None:
            return False
        if not restore:
            guid = guids[0]
            if not self.safe(guid) or (guid not in self.owned and len(self.owned)>=LIMIT):
                return False
            persona = self.app.personas.get(guid)
            if persona is None: return False
            param = style(persona).param
            self.owned.add(guid)
            self.save() # possible ownership is durable before bytes can leave
        else:
            param = 'playstyle'
        ident = uuid.uuid4().hex
        self.jobs.expect(ident)
        try:
            ack = await self.app.bridge.send(BotDo(ident,tuple(guids),'restore' if restore else 'playstyle',param), preserve_id=True)
            if not ack.ok:
                return False # keep prior/unknown ownership, even when this attempt was refused
            job = await self.jobs.wait(ident,5)
            good = bool(job and job.kind=='bot_do' and job.state=='done' and not job.reason
                        and not job.skipped and set(job.guids)==set(guids))
            if good and restore:
                self.owned.difference_update(guids)
                self.save()
            log('playstyle_result',count=len(guids),restore=int(restore),ok=int(good))
            return good
        except Exception as exc:
            log('playstyle_transport',count=len(guids),error=type(exc).__name__)
            return False
        finally:
            self.jobs.close()

    async def run(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.pending:
                guid = next(iter(self.pending))
                restore = self.pending.pop(guid)
                if restore:
                    if await self.command([guid],True) and self.safe(guid):
                        self.queue(guid)
                else:
                    await self.command([guid],False)
                await asyncio.sleep(0) # bounded work and cancellation between members

    async def close(self):
        self.stopping = True
        self.pending.clear()
        if self.owned:
            guids = sorted(self.owned)
            try:
                async with asyncio.timeout(20):
                    for start in range(0,len(guids),40):
                        if not self.connected or not self.supported: break
                        await self.command(guids[start:start+40],True)
            except TimeoutError:
                log('playstyle_cleanup_pending',count=len(self.owned))


def attach(app):
    if not enabled(app.data_dir/'mind.toml') and not app.store.get_meta(KEY):
        return None # off/no receipt: no hooks, tasks, requests, prompt or store changes
    register()
    service = PlayStyles(app)
    for kind in ('hello','cast','login','logout','group','boss'):
        app.register_event_handler(kind,service.event)
    async def job(ev): service.jobs.feed(ev)
    app.register_event_handler('job',job)
    app.register_shutdown_receipt('job',service.jobs.feed)
    app.register_connection_hook(service.connection)
    app.register_background(service.run)
    app.register_shutdown_drain(service.close)
    return service
