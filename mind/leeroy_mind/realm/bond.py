"""One private inn visit at a time. Every irreversible intent is persisted before dispatch.

Native supports moving to a whitelisted inn and the sit emote. No fishing, mounted
route or dungeon controller is invented here. Fresh native proximity is required
for arrival acceptance and the shared conversation; a Zone event is insufficient.
"""
import re
import uuid
import math
import random

from leeroy_mind.bond.policy import eligible
from leeroy_mind.bodies import BodyEvidenceUnavailable
from leeroy_mind.logs import log
from leeroy_mind.protocol import Chat
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.tuning.effective import dials_of
from leeroy_mind.wow import DUNGEONS, RAID_INSTANCES
from .actions import ActionRejected
from .action_store import ActionCapacity, ActionUnavailable
from .direct import DirectRefused
from .protocol import RealmMove, RealmEmote, RealmVisit, RealmVisitState

YES = re.compile(r'(?:yes|yeah|yep|sure|okay|ok|let[’\']?s go|on my way)[.! ]*', re.I)
NO = re.compile(r'(?:no|nope|not now|no thanks|maybe later|stop|leave me alone)[.! ]*', re.I)
TTL, VISIT_S, PROBE_S = 600, 900, 10
OBSERVATION_S = PROBE_S * 3 + 5
BG_MAPS = frozenset({30,489,529,559,562,566,572,607,617,618,628})


class Bond:
    def __init__(self, realm):
        self.realm, self.app, self.ports = realm, realm.app, realm.ports
        self.recovered = False
        self.query = None
        self.observed = None
        self.last_probe = float('-inf')
        self.work = {}
        self.unsupported_epoch = None
        self.offered_context = None
        self.talked = None
        self.body_checked = {}

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

    def available(self, guid, *, body=True):
        context = self.realm.action_context()
        member = self.app.world.members.get(guid)
        if not member or member.map in DUNGEONS or member.map in RAID_INSTANCES or member.map in BG_MAPS:
            return False
        if not (self.enabled() and context.enabled and context.connected and context.presence_known
            and self.ports.human_online() and not self.ports.human_grouped()
            and not self.ports.raid_holds_all()
            and guid in self.app.world.online and not self.app.world.in_group(guid)
            and eligible(self.ports.persona(guid), self.app.cfg.romance.dial)):
            return False
        if body:
            try:
                if guid not in self.realm.bodies.movable('realm_day', (guid,)):
                    return False
            except (BodyEvidenceUnavailable, ValueError):
                return False
        return True

    def fresh(self, place, *, near=False):
        if self.observed is None:
            return False
        ev, received, context = self.observed
        return (ev.place == place and ev.ready and (not near or ev.near)
                and context == self.realm.action_context() and 0 <= self.now()-received <= OBSERVATION_S)

    def valid(self, ident, states, *, near=False):
        row = self.row(ident)
        return bool(row and row['state'] in states and row['expires'] > self.now()
            and row['human'] == self.realm.action_context().human and self.available(row['guid'],body=False)
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
        if row['emote_id']:
            self.spawn('stand:'+row['id'],lambda:self.stand(row))

    async def stand(self, row):
        cmd = RealmEmote(uuid.uuid4().hex,row['guid'],'stand',social=True,seat_id=row['emote_id'])
        direct = self.realm.part('direct')
        if direct.prepare(cmd,owner='speech',ref='emote:bond-end:'+row['id'],
                          context=self.realm.action_context(),expects_job=False):
            try:
                await self.ports.send(cmd)
            finally:
                direct.abandon_unsent(cmd)

    def invalidate(self):
        self.query = self.observed = None
        self.work.clear()
        if self.recovered and (row := self.row()):
            self.close(row)

    def spawn(self, key, call):
        if key in self.work:
            return False
        token = object()
        self.work[key] = token
        def release(_=None):
            if self.work.get(key) is token:
                self.work.pop(key,None)
        async def run():
            try:
                await call()
            finally:
                release()
        task = self.realm.spawn(run)
        if task is None:
            release()
            return False
        task.add_done_callback(release)  # Also runs if cancelled before run() starts.
        return True

    async def tick(self):
        try:
            await self._tick()
        except Exception as exc:
            log('bond_tick_failed', error=type(exc).__name__)
            try:
                if row := self.row(): self.close(row,'unknown')
            except Exception:
                log('bond_close_failed')

    def can_offer(self, guid):
        e, now = self.app.engine, self.now()
        login = e._login_at
        if not isinstance(login,(int,float)) or not math.isfinite(login):
            return False
        persona = self.ports.persona(guid)
        if not eligible(persona,self.app.cfg.romance.dial):
            return False
        wait = 600 + (10-min(10,dials_of(persona).level('forward')))*30 + (10-self.app.cfg.romance.dial)*30
        return (now-login >= wait and self.app.cfg.talk.chattiness > 0 and e._may_whisper(now)
            and now-e._his_talk_at() >= 60 and guid not in e._whisper_threads(now)
            and e.whispers.refusal(now) is None)

    async def _tick(self):
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
            if row['expires'] <= self.now() or not self.available(row['guid'],body=False) or self.offered_context != self.realm.action_context():
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
        if (self.work or not self.ports.human_online() or self.ports.human_grouped()
                or not self.realm.action_context().presence_known
                or self.now()-self.last_probe < PROBE_S
                or self.unsupported_epoch == self.realm._transport().epoch):
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
        for member in sorted(self.ports.cast(), key=lambda m: (last.get(m.guid, float('-inf')),
                self.body_checked.get(m.guid,float('-inf')),m.guid)):
            guid = member.guid
            if not self.available(guid,body=False) or not self.can_offer(guid):
                continue
            if self.conn.execute('SELECT 1 FROM bond_invite WHERE day=? AND guid=? AND human=?',
                                 (day, guid, self.app.human_guid)).fetchone():
                continue
            # At most one ledger refresh per invitation tick, including a held body.
            self.body_checked[guid] = self.now()
            if self.available(guid):
                await self.probe(places[0].name, ('offer', guid, day))
            return

    async def probe(self, place, purpose):
        epoch = self.realm._transport().epoch
        if self.now()-self.last_probe < PROBE_S or 'probe' in self.work or self.unsupported_epoch == epoch:
            return
        async def send():
            cmd = RealmVisit(uuid.uuid4().hex, place)
            self.last_probe = self.now()
            self.query = (cmd, purpose, self.now(), self.realm.action_context(), epoch)
            await self.ports.send(cmd)
        self.spawn('probe',send)

    def probe_ack(self, cmd, ack):
        if not ack.ok and ack.error.startswith('unknown type: realm_visit') and self.query and self.query[0].id == cmd.id:
            self.unsupported_epoch = self.query[4]
            self.query = None
            log('bond_probe_unsupported')

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
                or ev.human_guid != context.human or not 0 <= self.now()-sent <= OBSERVATION_S
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
        if not self.available(guid) or not self.can_offer(guid) or not self.fresh(place) or day != self.app.clock.local().date().isoformat():
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
            return self.valid(ident, ('prepared',)) and self.available(row['guid']) and self.can_offer(row['guid'])
        def recorded(line_id, ack):
            if ack.ok and self.valid(ident, ('prepared',)):
                with self.app.store.transaction():
                    self.conn.execute("UPDATE bond_invite SET state='offered',offered=? WHERE id=?", (self.now(),ident))
        try:
            text = self.invitation(row['guid'],row['place'])
            await self.ports.say(row['guid'], 'whisper', text, to_guid=row['human'],
                                 before_transport=before, on_recorded=recorded)
        finally:
            if (current := self.row(ident)) and current['state'] == 'prepared':
                self.close(current, 'unknown')

    def invitation(self, guid, place):
        label = {'org_inn':'the inn in Orgrimmar','dal_inn':'the inn in Dalaran'}.get(place,'the nearby inn')
        dials = dials_of(self.ports.persona(guid))
        if dials.level('talkative') <= 3:
            return f"Sit with me at {label}? Say yes or meet me there."
        if dials.level('humour') >= 7:
            return f"Fancy claiming a quiet corner at {label} with me? Say yes or meet me there; no rush."
        if dials.level('forward') >= 7:
            return f"I'd like a little time with you at {label}. Say yes or meet me there if you fancy it."
        return random.Random(guid).choice((
            f"Fancy sitting with me at {label}? Say yes or meet me there; no rush.",
            f"Would you like a quiet moment with me at {label}? Say yes or meet me there.",
            f"Come keep me company at {label}? Say yes or meet me there when you like."))

    def accept(self, row):
        with self.app.store.transaction():
            self.conn.execute("UPDATE bond_invite SET state='accepted',expires=? WHERE id=? AND state='offered'",
                              (self.now()+VISIT_S, row['id']))

    def move(self, row):
        ident, guid = row['id'], row['guid']
        owner = 'bond:'+ident
        try:
            claimed = self.realm.bodies.claim(owner, [guid], rank='realm_day', until=row['expires'])
        except (BodyEvidenceUnavailable, ValueError):
            log('bond_claim_refused',guid=guid)
            self.close(row)
            return
        if guid not in claimed:
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
        except (ActionRejected, ActionCapacity, ActionUnavailable, ValueError):
            log('bond_move_refused',guid=guid)
            self.close(self.row(ident), 'unknown' if self.realm.queue.owns_command(cmd) else 'closed')

    def before_send(self, cmd):
        if type(cmd) is not RealmEmote or not cmd.social or cmd.emote != 'sit':
            return
        row = self.conn.execute('SELECT * FROM bond_invite WHERE emote_id=?',(cmd.id,)).fetchone()
        if not row or not self.valid(row['id'],('seated',),near=True) or not self.owns_body(row):
            self.realm.part('direct').abandon_unsent(cmd)
            raise DirectRefused('bond_unavailable')

    async def sit(self, row):
        ident = row['id']
        if not self.owns_body(row) or not self.valid(ident, ('seated',), near=True) or row['emote_id']:
            return
        cmd = RealmEmote(uuid.uuid4().hex, row['guid'], 'sit', row['human'], social=True,
                         lease_seconds=max(1,min(900,int(row['expires']-self.now()))))
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
