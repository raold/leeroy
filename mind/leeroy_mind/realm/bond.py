"""One private inn visit at a time. Every irreversible intent is persisted before dispatch.

Native supports moving to a whitelisted inn and the sit emote. No fishing, mounted
route or dungeon controller is invented here. Fresh native proximity is required
for arrival acceptance and the shared conversation; a Zone event is insufficient.
"""
import re
import uuid

from leeroy_mind.bond.policy import eligible
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.tuning.effective import dials_of
from .protocol import RealmMove, RealmEmote, RealmVisit, RealmVisitState

YES = re.compile(r'(?:yes|yeah|yep|sure|okay|ok|let[’\']?s go|on my way)[.! ]*', re.I)
NO = re.compile(r'(?:no|nope|not now|no thanks|maybe later|stop|leave me alone)[.! ]*', re.I)
TTL, VISIT_S, PROBE_S = 600, 900, 10


class Bond:
    def __init__(self, realm):
        self.realm, self.app, self.ports = realm, realm.app, realm.ports
        self.recovered = False
        self.query = None
        self.observed = None
        self.last_probe = float('-inf')
        self.started = self.app.clock.now()
        self.work = set()
        self.offered_context = None
        self.talked = None

    @property
    def conn(self):
        return self.app.store.conn

    def now(self):
        return self.app.clock.now()

    def row(self, ident=None):
        if ident is not None:
            return self.conn.execute('SELECT * FROM bond_invite WHERE id=?', (ident,)).fetchone()
        return self.conn.execute("SELECT * FROM bond_invite WHERE state IN ('prepared','offered','accepted','moving','seated') "
                                 'ORDER BY created,id LIMIT 1').fetchone()

    def enabled(self):
        return getattr(self.app.cfg.romance, 'activities', False) is True

    def available(self, guid):
        context = self.realm.action_context()
        return (self.enabled() and context.enabled and context.connected and context.presence_known
            and self.ports.human_online() and not self.ports.human_grouped()
            and not self.ports.raid_holds_all()
            and guid in self.app.world.online and not self.app.world.in_group(guid)
            and eligible(self.ports.persona(guid), self.app.cfg.romance.dial))

    def fresh(self, place, *, near=False):
        if self.observed is None:
            return False
        ev, received, context = self.observed
        return (ev.place == place and ev.ready and (not near or ev.near)
                and context == self.realm.action_context() and 0 <= self.now()-received <= PROBE_S+2)

    def valid(self, ident, states, *, near=False):
        row = self.row(ident)
        return bool(row and row['state'] in states and row['expires'] > self.now()
            and row['human'] == self.realm.action_context().human and self.available(row['guid'])
            and self.offered_context == self.realm.action_context()
            and self.fresh(row['place'], near=near))

    def owns_body(self, row):
        owner = 'bond:'+row['id']
        rank = self.realm.bodies.rank_for(owner)
        return rank is not None and self.realm.bodies.may_move_all(owner, (row['guid'],), rank=rank)

    def close(self, row, state='closed'):
        with self.app.store.transaction():
            self.conn.execute('UPDATE bond_invite SET state=? WHERE id=?', (state, row['id']))
        owner = 'bond:'+row['id']
        self.realm.queue.drop_owner(owner)  # Prepared only; never cancels an uncertain native move.
        self.realm.bodies.release(owner)

    def invalidate(self):
        self.query = self.observed = None
        if self.recovered and (row := self.row()):
            self.close(row)

    def spawn(self, key, call):
        if key in self.work:
            return
        self.work.add(key)
        async def run():
            try:
                await call()
            finally:
                self.work.discard(key)
        if self.realm.spawn(run) is None:
            self.work.discard(key)

    async def tick(self):
        if not self.enabled():
            self.invalidate()
            return
        if not self.recovered:
            # Never repeat an invitation or move after a crash. The Realm journal,
            # independently, retains unknown native ownership until actual proof.
            with self.app.store.transaction():
                self.conn.execute("UPDATE bond_invite SET state=CASE WHEN state='moving' THEN 'unknown' ELSE 'expired' END "
                                  "WHERE state IN ('prepared','offered','accepted','moving','seated')")
            self.recovered = True
        row = self.row()
        if row:
            if row['expires'] <= self.now() or not self.available(row['guid']) or self.offered_context != self.realm.action_context():
                self.close(row, 'expired')
                return
            if row['state'] == 'seated' and not self.owns_body(row):
                self.close(row)
                return
            if row['state'] == 'accepted' and self.fresh(row['place']):
                self.move(row)
            if row['state'] == 'seated' and self.fresh(row['place'], near=True):
                if not row['emote_id']:
                    self.spawn('sit:'+row['id'], lambda: self.sit(row))
                elif self.talked != row['id']:
                    receipt = self.conn.execute('SELECT state FROM realm_direct WHERE command_id=?',
                                                (row['emote_id'],)).fetchone()
                    guards = getattr(self.app.engine, 'guards', None)
                    if receipt and receipt['state'] == 'acked' and (guards is None or guards.allow(
                            row['guid'], TurnKind.WHISPER, self.now())):
                        self.spawn('talk:'+row['id'], lambda: self.talk(row))
            await self.probe(row['place'], row['id'])
            return
        # Let login greetings settle. Forward/romance dials also lengthen this wait.
        if self.now()-self.started < 600 or self.work:
            return
        day = self.app.clock.local().date().isoformat()
        if self.conn.execute('SELECT count(*) FROM bond_invite WHERE day=?', (day,)).fetchone()[0] >= 2:
            return
        speech = self.realm.part('speech')
        if speech is None:
            return
        places = speech.places.by_kind('inn', zone=self.ports.human_zone())
        if not places:
            return
        last = dict(self.conn.execute('SELECT guid,max(created) FROM bond_invite WHERE human=? GROUP BY guid',
                                      (self.app.human_guid,)))
        # Least recently invited first; a small guild-wide cap must not always
        # reserve both daily invitations for the lowest two character ids.
        for member in sorted(self.ports.cast(), key=lambda m: (last.get(m.guid, float('-inf')), m.guid)):
            guid = member.guid
            if not self.available(guid):
                continue
            level = min(10, dials_of(self.ports.persona(guid)).level('forward'))
            wait = 600 + (10-level)*30 + (10-self.app.cfg.romance.dial)*30
            if self.now()-self.started < wait:
                continue
            if self.conn.execute('SELECT 1 FROM bond_invite WHERE day=? AND guid=? AND human=?',
                                 (day, guid, self.app.human_guid)).fetchone():
                continue
            await self.probe(places[0].name, ('offer', guid, day))
            return

    async def probe(self, place, purpose):
        if self.now()-self.last_probe < PROBE_S:
            return
        self.last_probe = self.now()
        cmd = RealmVisit(uuid.uuid4().hex, place)
        self.query = (cmd, purpose, self.now(), self.realm.action_context(), self.realm._transport().epoch)
        self.spawn('probe', lambda: self.ports.send(cmd))

    async def on_event(self, ev):
        if not self.enabled():
            return
        if type(ev) is RealmVisitState:
            query = self.query
            if not query:
                return
            cmd, purpose, sent, context, epoch = query
            if (ev.id != cmd.id or ev.place != cmd.place or context != self.realm.action_context()
                or self.realm._delivery_epoch(ev) != epoch or ev.guild_id != context.guild
                or ev.human_guid != context.human or not 0 <= self.now()-sent <= PROBE_S
                or not sent-2 <= ev.ts/1000 <= self.now()+2):
                return
            self.query = None
            self.observed = (ev, self.now(), context)
            if not ev.ready:
                if row := self.row(): self.close(row)
                return
            if isinstance(purpose, tuple):
                if self.row() is None and not ev.near:
                    self.offer(purpose[1], purpose[2], ev.place)
                return
            row = self.row(purpose)
            if row and row['state'] == 'offered' and ev.near and self.valid(row['id'], ('offered',)):
                self.accept(row)
        elif type(ev) is Chat:
            row = self.row()
            if (not row or row['state'] not in ('offered','accepted','moving','seated')
                    or ev.channel != 'whisper' or ev.from_guid != row['human'] or ev.to_guid != row['guid']
                    or row['offered'] is None or not row['offered'] <= ev.ts/1000 <= self.now()+2):
                return
            if NO.fullmatch(ev.text.strip()):
                self.close(row, 'declined')
            elif row['state'] == 'offered' and YES.fullmatch(ev.text.strip()) and self.valid(row['id'], ('offered',)):
                self.accept(row)

    def offer(self, guid, day, place):
        if not self.available(guid) or not self.fresh(place) or day != self.app.clock.local().date().isoformat():
            return
        ident, now = uuid.uuid4().hex, self.now()
        with self.app.store.transaction():
            if self.conn.execute('SELECT count(*) FROM bond_invite WHERE day=?', (day,)).fetchone()[0] >= 2:
                return
            if self.conn.execute('SELECT 1 FROM bond_invite WHERE day=? AND guid=? AND human=?',
                                 (day,guid,self.app.human_guid)).fetchone():
                return
            self.conn.execute('INSERT INTO bond_invite(id,guid,human,day,place,created,expires,state) '
                "VALUES (?,?,?,?,?,?,?,'prepared')", (ident,guid,self.app.human_guid,day,place,now,now+TTL))
        self.offered_context = self.realm.action_context()
        self.spawn('offer:'+ident, lambda: self.say_offer(ident))

    async def say_offer(self, ident):
        row = self.row(ident)
        def before():
            return self.valid(ident, ('prepared',))
        def recorded(line_id, ack):
            if ack.ok and self.valid(ident, ('prepared',)):
                with self.app.store.transaction():
                    self.conn.execute("UPDATE bond_invite SET state='offered',offered=? WHERE id=?", (self.now(),ident))
        try:
            # The whitelist identifier becomes a readable label, never model input or a command.
            text = f"Fancy sitting with me at {row['place'].replace('_',' ')}? Say yes or meet me there; no rush."
            await self.ports.say(row['guid'], 'whisper', text, to_guid=row['human'],
                                 before_transport=before, on_recorded=recorded)
        finally:
            if (current := self.row(ident)) and current['state'] == 'prepared':
                self.close(current, 'unknown')

    def accept(self, row):
        with self.app.store.transaction():
            self.conn.execute("UPDATE bond_invite SET state='accepted',expires=? WHERE id=? AND state='offered'",
                              (self.now()+VISIT_S, row['id']))

    def move(self, row):
        ident, guid = row['id'], row['guid']
        owner = 'bond:'+ident
        if guid not in self.realm.bodies.claim(owner, [guid], rank='realm_day', until=row['expires']):
            self.close(row)
            return
        cmd = RealmMove(uuid.uuid4().hex, guid, row['place'], social=True)
        # Persist the id first: even a crash before enrollment must not create another move.
        with self.app.store.transaction():
            self.conn.execute("UPDATE bond_invite SET state='moving',move_id=? WHERE id=?", (cmd.id,ident))
        def done(ok, reason, value):
            current = self.row(ident)
            if current['state'] != 'moving': return
            if not ok or not self.owns_body(current) or not self.valid(ident, ('moving',)):
                self.close(current)
                return
            with self.app.store.transaction():
                self.conn.execute("UPDATE bond_invite SET state='seated' WHERE id=?", (ident,))
        try:
            self.realm.queue.submit(cmd, owner=owner, on_done=done,
                still_valid=lambda: self.valid(ident, ('moving',)))
        except Exception:
            self.close(self.row(ident), 'unknown')
            raise

    async def sit(self, row):
        ident = row['id']
        if not self.owns_body(row) or not self.valid(ident, ('seated',), near=True) or row['emote_id']:
            return
        cmd = RealmEmote(uuid.uuid4().hex, row['guid'], 'sit', row['human'], social=True)
        direct = self.realm.part('direct')
        with self.app.store.transaction():
            if not direct.prepare(cmd, owner='speech', ref='emote:bond:'+ident,
                                  context=self.realm.action_context(), expects_job=False):
                return
            self.conn.execute('UPDATE bond_invite SET emote_id=? WHERE id=?', (cmd.id,ident))
        ack = await self.ports.send(cmd)
        if not ack.ok or not self.valid(ident, ('seated',), near=True):
            self.close(self.row(ident))
            return

    async def talk(self, row):
        ident = row['id']
        if not self.owns_body(row) or not self.valid(ident, ('seated',), near=True) or self.talked == ident:
            return
        self.talked = ident
        turn = Turn(TurnKind.WHISPER, row['guid'], 'whisper', to_guid=row['human'],
            addressee=row['human'], max_lines=1, deadline=self.now()+60,
            instruction='He is here with you at the inn, where you have sat down. '
                        'Share one gentle, playful observation about sitting here; no invented actions or promises.')
        await self.app.engine.speak(turn, why='source:bond',
            still_valid=lambda: self.owns_body(row) and self.valid(ident, ('seated',), near=True))


def attach(realm):
    return Bond(realm), ('chat', 'realm_visit_state')
