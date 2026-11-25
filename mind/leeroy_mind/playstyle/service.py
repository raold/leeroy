"""Bounded event-triggered style requests and retained cleanup receipts; native is authoritative."""
import asyncio
import json
import logging
import re
import tomllib
import uuid
from collections import Counter

from leeroy_mind.guild.ports import JobWaiter
from leeroy_mind.guild.protocol import BotDo, PlayStyleEntry, PlayStyleTable, register
from leeroy_mind.logs import log
from leeroy_mind.protocol import Boss, Cast, Group, Hello, Login, Logout
from leeroy_mind.wow import BOSS_NAMES, CAST_MIN_LEVEL, DUNGEONS
from . import style

KEY = 'playstyle:owned'
LIMIT = 512
# A native that holds the whole table (playstyle_table) and styles each member itself whenever she is free.
TABLE_FEATURE = 'playstyle_table'
# Refusals of the table that one bot_do a member would get as well: wait for his login or the next hello.
TABLE_OFF = frozenset({'playstyle_off', 'disabled'})
INSTANCE_MAPS = frozenset(map_id for map_id,boss_id in BOSS_NAMES) | frozenset(DUNGEONS)
_CODE = re.compile(r'[A-Za-z0-9_:.,-]{1,64}')


def _code(value):
    """A native reason or error as a log code: at most 64 code characters, else '?'."""
    text = str(value or '')[:64]
    return text if _CODE.fullmatch(text) else '?'


def _outcome(job, guids):
    """'ok' when the job proves the request, else why not: no_job (no terminal in 5 s), kind,
    native's reason (or the state), skipped:<the first skip's reason>, or guids."""
    if job is None:
        return 'no_job'
    if job.kind!='bot_do':
        return 'kind'
    if job.state!='done' or job.reason:
        return _code(job.reason or job.state)
    if job.skipped:
        return 'skipped:'+_code(getattr(job.skipped[0],'reason',''))
    if set(job.guids)!=set(guids):
        return 'guids'
    return 'ok'


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
        self.tally = Counter()  # (restore, reason) of the commands since the last summary
        # The table (playstyle_table) on this connection: whether hello lists it, and its mode: None
        # (not tried yet), 'table' (native holds `sent`), 'off' (native's style switch refused it) or
        # 'legacy' (refused for itself: one bot_do a member, as without the feature).
        self.table_supported = False
        self.mode = None
        self.sent = None
        self.table_due = False

    def tabled(self):
        """Native holds (or is about to be sent) the table: no per-member style request is made."""
        return self.table_supported and self.mode != 'legacy'

    def want_table(self):
        if self.table_supported and self.mode in (None, 'table'):
            self.table_due = True
            self.wake.set()

    def styles(self):
        """The cast's whole table, in guid order: every member with a persona, online or not."""
        out = []
        for guid in sorted(self.app.world.cast_guids()):
            persona = self.app.personas.get(guid)
            if persona is not None and guid != self.app.human_guid:
                out.append(PlayStyleEntry(guid, style(persona).param))
            if len(out) >= LIMIT:
                break
        return tuple(out)

    def present(self):
        """He is in game. A style is only added then: it changes how his bots fight beside
        him, and for nobody else. Restoration never waits for him."""
        return bool(getattr(self.app.world,'human_online',False))

    def flush(self):
        """One line for the pass just worked off (a hello's reconciliation, his login, a
        group): counts by outcome, never a line a member."""
        if not self.tally:
            return
        tally,self.tally = self.tally,Counter()
        ok = lambda restore: tally.get((restore,'ok'),0)
        other = sorted((('restore:' if restore else 'apply:')+reason,n)
                       for (restore,reason),n in tally.items() if reason!='ok')
        log('playstyle_pass',commands=sum(tally.values()),applied=ok(False),restored=ok(True),
            other=','.join(f'{k}={n}' for k,n in other) or '-',owner=int(self.present()),
            owned=len(self.owned or ()))

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
        return (self.on and self.present() and self.owned is not None and self.supported
                and self.connected and not self.stopping
                and not self.app.stopping and member is not None and member.level>=CAST_MIN_LEVEL
                and guid in self.app.world.online and member.map not in INSTANCE_MAPS
                and not any(guid in who for who in self.fights.values())
                and (group is None or not group.raid)
                and guid != self.app.human_guid)

    async def connection(self,connected):
        self.connected = connected
        if not connected:
            self.supported = self.table_supported = self.table_due = False
            self.mode = self.sent = None  # native drops the table with this connection and takes back its styles
            self.pending.clear()
            self.jobs.close()

    async def event(self,ev):
        if self.app.is_backlog(ev) or self.stopping:
            return
        if isinstance(ev,Hello):
            self.supported = 'playstyle' in ev.features
            self.table_supported = self.supported and TABLE_FEATURE in ev.features
            self.mode = self.sent = None
            self.on = enabled(self.app.data_dir/'mind.toml')
            self.seen.clear()
            self.group_members.clear()
            self.fights.clear() # the new native snapshot owns combat admission; old unmatched events do not
            if self.owned:
                for guid in sorted(self.owned):
                    self.queue(guid,True) # reconcile old/unknown receipts before any new style
            self.want_table()
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
            if ev.human and self.table_supported:
                if self.mode in ('off','legacy'): self.mode = None # his login tries the table again
                self.want_table()
            elif self.tabled(): self.want_table() # a member's persona or the switch may have changed
            elif not ev.human: self.queue(ev.guid)
            else:
                # he is here now: the members a hello or a cast found while he was away
                for guid in sorted(self.seen): self.queue(guid)
        elif isinstance(ev,Cast):
            online = {member.guid for member in ev.members if member.online}
            if self.tabled(): self.want_table()
            else:
                for guid in sorted(online-self.seen): self.queue(guid)
            self.seen = online
        elif isinstance(ev,Group):
            members = set(ev.members)
            if self.tabled(): # native takes the table's styles back in a raid; only old receipts are ours
                for guid in sorted(members if ev.raid else ()):
                    if self.owned and guid in self.owned: self.queue(guid,True)
            else:
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
                self.tally[restore,'refused:'+_code(ack.error)] += len(guids)
                log('playstyle_refused',level=logging.DEBUG,count=len(guids),restore=int(restore),error=_code(ack.error))
                return False # keep prior/unknown ownership, even when this attempt was refused
            job = await self.jobs.wait(ident,5)
            reason = _outcome(job,guids)
            good = reason=='ok'
            if good and restore:
                self.owned.difference_update(guids)
                self.save()
            self.tally[restore,reason] += len(guids)
            log('playstyle_result',level=logging.DEBUG,count=len(guids),restore=int(restore),ok=int(good),reason=reason)
            return good
        except Exception as exc:
            self.tally[restore,'transport:'+type(exc).__name__] += len(guids)
            log('playstyle_transport',level=logging.DEBUG,count=len(guids),error=type(exc).__name__)
            return False
        finally:
            self.jobs.close()

    async def send_table(self):
        """The whole table in one command, when it differs from what native holds. Refused
        playstyle_table_off (Leeroy.PlayStyle.Table at 0) or for any reason of its own, this
        connection keeps one bot_do a member; refused as one bot_do would be, it waits."""
        if (not self.connected or not self.supported or not self.table_supported or self.owned is None
                or self.stopping or self.app.stopping or self.mode not in (None, 'table')):
            return
        styles = self.styles() if self.on else ()
        if self.mode == 'table' and styles == self.sent:
            return # native holds this very table
        if self.mode is None and not styles:
            return # nothing held to clear, and no cast to style yet
        try:
            ack = await self.app.bridge.send(PlayStyleTable(uuid.uuid4().hex,styles), preserve_id=True)
        except Exception as exc:
            log('playstyle_table_failed',error=type(exc).__name__) # the next hello, login or cast sends it
            return
        if ack.ok:
            self.mode,self.sent = 'table',styles
            log('playstyle_table_sent',styles=len(styles),on=int(self.on),owner=int(self.present()))
            return
        self.sent = None
        self.mode = 'off' if ack.error in TABLE_OFF else 'legacy'
        log('playstyle_table_refused',error=_code(ack.error),mode=self.mode)
        if self.mode=='legacy' and self.present():
            for guid in sorted(self.seen): self.queue(guid)

    async def run(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.pending:
                guid = next(iter(self.pending))
                restore = self.pending.pop(guid)
                if restore:
                    if await self.command([guid],True) and self.safe(guid) and not self.tabled():
                        self.queue(guid)
                elif self.present() and not self.tabled():
                    await self.command([guid],False)
                await asyncio.sleep(0) # bounded work and cancellation between members
            self.flush()
            if self.table_due:
                self.table_due = False
                await self.send_table()

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
            self.flush()


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
