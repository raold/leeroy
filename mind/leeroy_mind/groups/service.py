"""Owner actions and per-member answer timing for small, closed audiences."""
import json
import math
from pathlib import Path

from leeroy_mind.discord.store import DiscordRefused
from leeroy_mind.discord import owntime
from .config import GroupsConfig, load_groups_config
from . import store as groups
from .audience import message_readers, visible

OWED = 'groups:owed:'


def arguments(args):
    shapes = {'group_create': {'action','members'}, 'group_add': {'action','channel','guid'},
        'group_remove': {'action','channel','guid'}, 'group_leave': {'action','channel'},
        'group_rename': {'action','channel','name'}}
    action = args['action']
    shape = shapes[action]
    extra = {'name'} if action == 'group_create' else set()
    if not shape <= set(args) <= shape | extra:
        raise ValueError('That group action has missing or unexpected fields.')
    result = dict(args)
    if 'channel' in args and groups.ident(args['channel']) is None:
        raise ValueError('Choose a group chat.')
    if 'guid' in args and (type(args['guid']) is not int or not 0 < args['guid'] <= 0xFFFFFFFF):
        raise ValueError('Choose a guild member.')
    if 'members' in args:
        members = args['members']
        if (type(members) is not list or not 2 <= len(members) <= 4
                or any(type(g) is not int or not 0 < g <= 0xFFFFFFFF for g in members)
                or len(set(members)) != len(members)):
            raise ValueError(groups.WORDS['group_members'])
    if 'name' in args:
        if action == 'group_create' and args['name'] == '':
            result.pop('name')
        else:
            try:
                result['name'] = groups.name(args['name'])
            except DiscordRefused:
                raise ValueError(groups.WORDS['group_name']) from None
    return result


class Groups:
    def __init__(self, app):
        self.app = app
        self.config = (load_groups_config(Path(app.data_dir)/'mind.toml')
                       if getattr(app,'data_dir',None) is not None else GroupsConfig())
        world = getattr(app,'world',None)
        if world is not None:
            world.group_audience = self.readers
        self._queued = {}

    def readers(self, channel):
        return groups.readers(self.app.store.conn,channel) if self.config.enabled else frozenset()

    def check(self, channel):
        if not self.config.enabled:
            raise DiscordRefused('groups_off')
        groups._open(self.app.store,channel)
        if self.app.human_guid not in self.readers(channel):
            raise DiscordRefused('group_member')

    def member(self, guid):
        from leeroy_mind.personas.tiers import is_cast
        if (guid == self.app.human_guid or self.app.personas.get(guid) is None
                or not is_cast(self.app.store.conn,guid)):
            raise DiscordRefused('group_members')

    def handle(self, args):
        if not self.config.enabled:
            raise DiscordRefused('groups_off')
        app, action = self.app,args['action']
        if action == 'group_create':
            for guid in args['members']:
                self.member(guid)
            channel = groups.create(app.store,human=app.human_guid,members=args['members'],
                title=args.get('name'),created_by=app.human_guid,now=app.clock.now(),config=self.config)
            return {'ok':True,'channel':channel}
        channel = args['channel']
        self.check(channel)
        if action == 'group_add':
            self.member(args['guid'])
            groups.add(app.store,channel,args['guid'],human=app.human_guid,now=app.clock.now())
        elif action in {'group_remove','group_leave'}:
            guid = app.human_guid if action == 'group_leave' else args['guid']
            groups.remove(app.store,channel,guid,human=app.human_guid,now=app.clock.now())
        else:
            groups.rename(app.store,channel,args['name'])
        return {'ok':True,'channel':channel}

    def his_post(self, talk, mid):
        """Choose once, persist each recipient independently, survive a restart."""
        if not self.config.enabled:
            return
        app = self.app
        key = OWED+str(mid)
        if app.store.get_meta(key) is not None:
            return
        row = app.store.conn.execute('SELECT channel,ts FROM discord_message WHERE id=?',(mid,)).fetchone()
        items = talk.plan(mid)
        pending = {str(turn.speaker):self._due_of(turn.speaker,mid,row) for turn,_ in items}
        app.store.set_meta(key,json.dumps(pending))
        self.tick(talk,app.clock.now())

    def _due_of(self, guid, mid, row):
        app, conn = self.app,self.app.store.conn
        last = conn.execute("SELECT max(ts) FROM discord_message WHERE channel=? AND author=? "
            "AND origin='mind' AND ts<=?",(row[0],guid,row[1])).fetchone()[0]
        previous = conn.execute("SELECT coalesce(max(id),0) FROM discord_message WHERE channel=? "
            "AND author=? AND origin='mind' AND id<?",(row[0],guid,mid)).fetchone()[0]
        his = [r[0] for r in conn.execute("SELECT text FROM discord_message WHERE channel=? "
            "AND origin='human' AND deleted IS NULL AND id>? AND id<=?",(row[0],previous,mid))]
        urgent = any(owntime.URGENT_RE.search(text) for text in his)
        due = owntime.due_at(app.personas.get(guid),post_ts=row[1],now=app.clock.now(),
            local_now=app.clock.local(),message_id=mid,urgent=urgent,
            talking=last is not None and row[1]-last<=owntime.TALKING_S,insist=len(his)>=owntime.INSIST)
        return {'due':due,'urgent':urgent}

    def refresh_due(self):
        """Edits change timing only for recipients still owed; never choose again."""
        if not self.config.enabled:
            return
        for key, mid, row, pending in self._pending(self.app.clock.now()):
            pending = {guid:self._due_of(int(guid),mid,row) for guid in pending}
            self.app.store.set_meta(key,json.dumps(pending))

    def _pending(self, now):
        app, conn = self.app,self.app.store.conn
        for key, raw in conn.execute("SELECT key,value FROM meta WHERE key GLOB 'groups:owed:*' AND value<>'{}'").fetchall():
            try:
                mid, pending = int(key[len(OWED):]),json.loads(raw)
            except (ValueError,TypeError):
                continue
            if (not isinstance(pending,dict) or not 1 <= len(pending) <= 4 or any(
                    not str(g).isascii() or not str(g).isdecimal() or len(str(g))>10 or int(g)<=0
                    or not isinstance(d,dict) or type(d.get('due')) not in (int,float)
                    or not math.isfinite(d['due']) or type(d.get('urgent')) is not bool
                    for g,d in pending.items())):
                app.store.set_meta(key,'{}')
                continue
            row = conn.execute('SELECT channel,ts,deleted FROM discord_message WHERE id=?',(mid,)).fetchone()
            current = self.readers(row[0]) if row else frozenset()
            for raw_guid in list(pending):
                guid = int(raw_guid)
                valid = (row is not None and row[2] is None and now-row[1]<=owntime.LET_GO_S
                    and visible(message_readers(conn,mid),row[0],speaker=guid,to_guid=0,readers=current))
                sent = conn.execute("SELECT 1 FROM discord_message WHERE reply_to=? AND author=? "
                    "AND origin='mind' LIMIT 1",(mid,guid)).fetchone()
                if not valid or sent or app.personas.get(guid) is None:
                    pending.pop(raw_guid)
            yield key, mid, row, pending

    def tick(self, talk, now):
        if not self.config.enabled:
            return
        from leeroy_mind.arc.live import away_now
        from leeroy_mind.discord.talk import RETRY_S, TRIES
        app = self.app
        away = away_now(app.store,now)
        for key, mid, _, pending in self._pending(now):
            for raw_guid, due in list(pending.items()):
                guid = int(raw_guid)
                if guid in away or talk.own_time() and now < due['due']:
                    continue
                stamp, tries = self._queued.get((mid,guid),(-float('inf'),0))
                if now-stamp<RETRY_S:
                    continue
                if tries>=TRIES:
                    pending.pop(raw_guid)
                    continue
                if talk.own_time() and not due['urgent'] and not talk._model_ready():
                    continue
                items = talk.plan(mid,group_chosen=[guid])
                if not items:
                    pending.pop(raw_guid)
                    continue
                self._queued[(mid,guid)] = (now,tries+1)
                while len(self._queued)>256:
                    del self._queued[next(iter(self._queued))]
                talk._take_lease()
                app.engine._queue(items)
            app.store.set_meta(key,json.dumps(pending))
