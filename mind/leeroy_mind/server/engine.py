"""Small, cancellable Discord conversations; all speech uses the ordinary pipeline."""
import asyncio
from dataclasses import dataclass, field
import logging
from pathlib import Path
import random
import re
import uuid

from leeroy_mind.discord import layout
from leeroy_mind.discord.channels import channel_id, is_group
from leeroy_mind.discord.talk import answer_delay, GONE
from leeroy_mind.discord.presence import presence_of, local_time, RNG_SEED
from leeroy_mind.discord.transport import DiscordTarget
from leeroy_mind.llm.scheduler import Priority
from leeroy_mind.talk.budget import _HOLD
from leeroy_mind.talk.guards import Guards
from leeroy_mind.talk.pipeline import TurnMeta, TurnWithdrawn
from leeroy_mind.talk.turns import Turn, TurnKind
from .clock import in_window
from . import records, rhythm, runtime, seeds
from .state import pulled_into_play

log = logging.getLogger(__name__)
TICK_S = 5.0
DAY_KEY = records.DAY_KEY
DRY_TURNS = 2
TIES = {'friend':2., 'crush':2., 'fond':1.5, 'neutral':1., 'rival':.6, 'feud':0.}


class ConversationGuards(Guards):
    """An admitted conversation owns its chain; it never spends the game's pace."""
    def __init__(self, maximum, rng):
        super().__init__(per_person_s=3, guild_per_min=6, social_ceiling=6, rng=rng)
        self.maximum = maximum

    def _roll(self):
        return self.maximum


@dataclass
class Conversation:
    id: str
    channel: str
    seed: seeds.Seed
    cast: list[int]
    started: float
    maximum: int
    messages: list[int] = field(default_factory=list)
    reason: str = ''
    task: asyncio.Task | None = None
    teaser: int | None = None
    teased: bool = False
    in_flight: int | None = None
    in_flight_turn: Turn | None = None
    readers: frozenset[int] = frozenset()


class ServerEngine:
    def __init__(self, app, config, rng: random.Random):
        self.app, self.config, self.rng = app, config, rng
        self.next_start = app.clock.now()
        self._live: dict[str, Conversation] = {}
        self._closed = False
        self._tick_lock = asyncio.Lock()
        self._swept = ''
        self._guard_day = ''
        self._claims_day = ''
        self._human_until = {}
        app.engine.add_instruction_hook(self._instruction)

    def _blocked(self, now):
        app = self.app
        if self._closed or app.stopping or not self.config.enabled:
            return 'stopping'
        if in_window(app.clock.local(), self.config.quiet_window):
            return 'window'
        if not app.server.lease.ready():
            return 'lease'
        if not app.engine.live_review.config.enabled:
            return 'guards'
        if app.engine.raid.phase(now) in _HOLD or app.engine._in_boss_fight(now):
            return 'raid_hold'
        if app.engine._login_quiet(now):
            return 'login_quiet'
        return None

    async def run(self):
        while not self._closed:
            try:
                await self.tick(self.app.clock.now())
            except Exception as error:
                log.warning('server_tick_failed code=%s', type(error).__name__)
            await self.app.clock.sleep(TICK_S)

    async def tick(self, now):
        async with self._tick_lock:
            day = self.app.clock.local().date().isoformat()
            if self.config.enabled and not self._closed and day != self._swept:
                records.prune(self.app.store, day, now)
                self._swept = day
            if (self.config.enabled and not self._closed and self._claims_day != day
                    and not self.app.engine.live_review.claims_enabled):
                self._claims_day = day
                log.warning('server_needs_claims')
            blocked = self._blocked(now)
            if blocked:
                for conversation in tuple(self._live.values()):
                    self._end(conversation, blocked)
                if blocked == 'guards' and self._guard_day != day:
                    self._guard_day = day
                    log.warning('server_needs_guards')
                return
            if len(self._live) >= rhythm.MAX_LIVE or now < self.next_start:
                return
            rate = rhythm.starts_per_hour(self.app.clock.local(), per_hour=self.config.conversations_per_hour,
                chattiness=self.app.engine.cfg.talk.chattiness, in_game=self.app.world.human_online,
                in_game_share=self.config.in_game_share)
            if rate <= 0:
                return
            # Reserve the next opportunity before the read yields; no catch-up burst on a slow source.
            self.next_start = now + rhythm.next_gap(rate, self.rng)
            observed = await runtime.snapshot(self.app, now, include_seeds=True)
            if self._blocked(self.app.clock.now()):
                return
            busy = {c.channel for c in self._live.values()}
            speaking = {c.in_flight for c in self._live.values() if c.in_flight is not None}
            speaking.update(t.speaker for t in self.app.engine.planned_turns())
            speaking.update(self.app.engine.active_speakers())
            active_seeds = {c.seed.key for c in self._live.values()}
            free = {g for g,s in observed.states.items() if s.presence.state in ('online','idle')
                    and g not in observed.in_play and g not in observed.away and g not in speaking}
            available = [seed for seed in observed.seeds if seed.key not in active_seeds
                         and (seed.openers is None or free.intersection(seed.openers))
                         and any(c not in busy and
                         now >= self._human_until.get(c, 0) for c in seed.channels)]
            # Remove busy destinations, keeping their original preference order.
            from dataclasses import replace
            available = [replace(seed, channels=tuple(c for c in seed.channels if c not in busy and
                         now >= self._human_until.get(c,0))) for seed in available]
            chosen = rhythm.choose_seed_and_channel(available, local=observed.local,
                officers_free=len(free & observed.officers), rng=self.rng)
            if chosen is None:
                return
            seed, channel = chosen
            cast = rhythm.choose_cast(channel, seed, observed.states, observed.personas,
                officers=observed.officers, human=observed.human, in_play=observed.in_play | speaking,
                ties=lambda a,b:TIES[observed.ties.get((a,b),'neutral')], rng=self.rng,
                classes=observed.classes, away=observed.away,
                tease=lambda a,b: self._tease(a,b),
                group_audience=getattr(observed,'group_audiences',{}).get(channel,frozenset()))
            if len(cast) < 2:
                return
            conversation = Conversation(uuid.uuid4().hex, channel, seed, cast, now, rhythm.length(self.rng))
            if is_group(channel):
                conversation.maximum = self.rng.randint(2,4)
                conversation.readers = observed.group_audiences.get(channel,frozenset())
            if not self._valid(conversation, cast[0]):
                return
            if is_group(channel):
                from leeroy_mind.groups.conversations import reserve
                if not reserve(self.app.store,channel,day):
                    return
            self._live[conversation.id] = conversation
            conversation.task = asyncio.create_task(self._conversation(conversation), name='server-conversation')
            conversation.task.add_done_callback(lambda task,c=conversation:self._finished(c))
            log.info('server_conversation_started id=%s channel=%s seed_kind=%s cast=%s',
                     conversation.id, channel, seed.kind, ','.join(map(str,cast)))

    def _end(self, conversation, reason):
        if not conversation.reason:
            conversation.reason = reason
        if conversation.task is not None and not conversation.task.done():
            conversation.task.cancel()

    def his_post(self, channel):
        channel = channel_id(channel)
        self._human_until[channel] = self.app.clock.now() + rhythm.MIN_GAP_S
        for conversation in tuple(self._live.values()):
            if conversation.channel == channel:
                self._end(conversation, 'his_post')

    def answer_queued(self, guid):
        """Release only this speaker's background work before an answer owed to him."""
        for conversation in tuple(self._live.values()):
            if conversation.in_flight == guid:
                self._end(conversation, 'answer_queued')

    def live(self):
        return [dict(id=c.id, channel=c.channel, seed=c.seed.kind, messages=len(c.messages), started=c.started)
                for c in self._live.values()]

    def _tease(self, teaser, target, *, allowance=False):
        from leeroy_mind.culture.tease import _tease_ok
        return _tease_ok(self.app.store.conn, teaser, target, personas=self.app.personas,
                         human=self.app.human_guid, now=self.app.clock.now(), allowance=allowance)[0]

    def _joke_valid(self, conversation, speaker):
        if conversation.seed.kind == 'loot_close' and len(conversation.seed.about) > 1:
            return self._gate(conversation, speaker, conversation.seed.about[1])
        if conversation.seed.kind != "joke":
            return True
        from leeroy_mind.culture.config import app_enabled
        from leeroy_mind.culture.seeds import _culture_seeds
        if not app_enabled(self.app):
            return False
        people = {g:self.app.personas.get(g) for g in self.app.world.discord_members()
                  if g != self.app.human_guid and self.app.personas.get(g) is not None}
        current = _culture_seeds(self.app.store, now=self.app.clock.now(), personas=people,
                                human=self.app.human_guid, include_recent=conversation.teased,
                                only=int(conversation.seed.key[5:]),
                                library=Path(self.app.data_dir) / 'library.db', ident=self.app.engine.human)
        if not any(s.key == conversation.seed.key and s.fact == conversation.seed.fact
                   and s.about == conversation.seed.about for s in current):
            return False
        return self._gate(conversation, speaker, conversation.seed.about[0])

    def _gate(self, conversation, speaker, target):
        """The kindness gate for the opener and each speaker; the target may answer."""
        teaser = conversation.teaser if conversation.teaser is not None else speaker
        if not target:
            return True
        if teaser == target or not self._tease(teaser, target, allowance=conversation.teased):
            return False
        return speaker == target or self._tease(speaker, target, allowance=conversation.teased)

    def _valid(self, conversation, speaker, parent=None):
        if conversation.reason or self._blocked(self.app.clock.now()) or not self._joke_valid(conversation, speaker):
            return False
        if parent is not None:
            row = self.app.store.conn.execute('SELECT channel,deleted FROM discord_message WHERE id=?',
                                              (parent,)).fetchone()
            if row is None or row[1] is not None or row[0] != conversation.channel:
                conversation.reason = 'deleted_parent'
                return False
        if speaker in pulled_into_play(self.app) or speaker in self.app.engine._away_now():
            return False
        if any(t.speaker == speaker and t is not conversation.in_flight_turn
               for t in self.app.engine.planned_turns()):
            return False
        if speaker in self.app.engine.active_speakers(exclude_task=conversation.task):
            return False
        if any(c is not conversation and c.in_flight == speaker for c in self._live.values()):
            return False
        p = self.app.personas.get(speaker)
        member = self.app.world.discord_members().get(speaker)
        if p is None or member is None or presence_of(p,
                local=local_time(self.app.clock.local(), p.identity.region),
                body_online=speaker in self.app.world.online, body_zone=member.zone,
                rng_seed=RNG_SEED).state not in ('online','idle'):
            return False
        # A rank change during generation cannot leak the officer conversation.
        if is_group(conversation.channel):
            from leeroy_mind.groups.conversations import valid
            return valid(self.app,conversation,speaker)
        return speaker in self.app.world.audience('discord:'+conversation.channel, from_guid=speaker)

    def _instruction(self, turn, meta, previous):
        if not meta.server_conversation:
            return previous
        conversation = self._live.get(meta.server_conversation)
        if conversation is None or not self._valid(conversation, turn.speaker):
            raise TurnWithdrawn('server_ended')
        target = meta.discord
        if target and target.reply_to is not None:
            row = self.app.store.conn.execute('SELECT channel,deleted FROM discord_message WHERE id=?',
                                              (target.reply_to,)).fetchone()
            if row is None or row[1] is not None or row[0] != conversation.channel:
                conversation.reason = 'deleted_parent'
                raise TurnWithdrawn(GONE)
        return previous

    def _record_joke(self, conversation, line, message, now):
        """DB-only spending inside the pipeline's post/chat transaction."""
        app = self.app
        from leeroy_mind.culture.tease import mark_teased, OWNER_KEY
        from leeroy_mind.clock import local_at, night_of
        receipt = f'culture:server-joke:{conversation.id}'
        with app.store.transaction():
            if app.store.get_meta(receipt) is None:
                seeds.mark_used(app.store, conversation.seed, now=now)
                app.store.conn.execute('UPDATE lexicon SET last_used=?,uses_total=uses_total+1 WHERE id=?',
                    (now, int(conversation.seed.key[5:])))
                app.store.set_meta(f'culture:wear-counted:{conversation.seed.key}:{line}', '1')
                target = conversation.seed.about[0]
                if target:
                    mark_teased(app.store, target, now=now)
                    if target == app.human_guid:
                        app.store.set_meta(OWNER_KEY, night_of(local_at(now)))
                app.store.set_meta(receipt, str(message))

    def _record_close(self, conversation, line, message, now):
        """A delivered close-loot thread spends its runner-up's teasing allowance for the day,
        once, inside the pipeline's post/chat transaction, as a joke's does."""
        from leeroy_mind.culture.tease import mark_teased
        receipt = f'culture:server-close:{conversation.id}'
        with self.app.store.transaction():
            if self.app.store.get_meta(receipt) is None:
                mark_teased(self.app.store, conversation.seed.about[1], now=now)
                self.app.store.set_meta(receipt, str(message))

    def _settle_joke(self, conversation, meta):
        """Read the committed receipt after success or shielded cancellation."""
        kind = 'close' if conversation.seed.kind == 'loot_close' else 'joke'
        conversation.teased = self.app.store.get_meta(f'culture:server-{kind}:{conversation.id}') is not None

    async def _conversation(self, conversation):
        app, last, dry = self.app, None, 0
        guards = ConversationGuards(conversation.maximum, self.rng)
        try:
            while len(conversation.messages) < conversation.maximum:
                reason = self._blocked(app.clock.now())
                if reason or conversation.reason:
                    conversation.reason = conversation.reason or reason
                    break
                observed = await runtime.snapshot(app, app.clock.now())
                eligible = [g for g in conversation.cast if g != last and g in observed.personas
                    and g not in observed.in_play and g not in observed.away
                    and observed.states[g].presence.state in ('online','idle')
                    and (last is None or observed.ties.get((g,last)) != 'feud')
                    and self._valid(conversation, g)]
                if not eligible:
                    conversation.reason = 'no_cast'
                    break
                parent, parent_author, addressed = None, None, set()
                if conversation.messages:
                    parent = (conversation.messages[-1] if self.rng.random() < .7 else
                              self.rng.choice(conversation.messages))
                    row = app.store.conn.execute('SELECT author,text,deleted FROM discord_message WHERE id=?',
                                                 (parent,)).fetchone()
                    if row is None or row[2] is not None:
                        conversation.reason = 'deleted_parent'
                        break
                    addressed.add(row[0])
                    parent_author = row[0]
                    for g in eligible:
                        name = observed.personas[g].character_name
                        if re.search(r'(?<!\w)@?'+re.escape(name)+r'\b', row[1], re.I):
                            addressed.add(g)
                    speaker = self.rng.choices(eligible, weights=[3 if g in addressed else 1 for g in eligible], k=1)[0]
                    gap = answer_delay(observed.states[speaker].presence.state, self.rng)
                    if self.rng.random() < .1:
                        gap = self.rng.uniform(600, 2400)
                    await app.clock.sleep(gap)
                else:
                    openers = conversation.seed.openers
                    if openers is not None:
                        eligible = [g for g in eligible if g in openers]
                        if not eligible:
                            conversation.reason = 'no_owner' if conversation.seed.kind == 'life' else 'no_witness'
                            break
                    speaker = conversation.cast[0] if conversation.cast[0] in eligible else eligible[0]
                if not self._valid(conversation, speaker):
                    conversation.reason = conversation.reason or 'scope_changed'
                    break
                definition = layout.channel(conversation.channel)
                if is_group(conversation.channel):
                    from leeroy_mind.groups.conversations import definition as group_definition
                    definition = group_definition(conversation.channel)
                purpose = definition.topic
                if conversation.seed.kind == 'purpose':
                    opener = 'Talk about this topic in your own way: '
                elif conversation.seed.kind in {'raid_before','raid_after','loot','life','calendar','birthday','his_day',
                                                'wow_event','loot_close'}:
                    opener = 'Start a conversation about this real event in your own way: '
                else:
                    opener = 'Start a conversation using only what is known: '
                instruction = (f"You're in #{definition.id}: {purpose}. " +
                    (opener+conversation.seed.fact
                     if parent is None else 'Reply to the chosen message and keep this conversation going naturally.'))
                if (conversation.seed.kind == 'joke' and conversation.seed.about[0]
                        and speaker != conversation.seed.about[0]):
                    target = conversation.seed.about[0]
                    person = observed.personas.get(target)
                    name = app.engine.human.called if target == app.human_guid else person.character_name
                    instruction += f" Tease {name} about it the way friends do; keep it kind."
                if conversation.seed.kind in ('joke', 'loot_close') and conversation.teaser is None:
                    conversation.teaser = speaker
                turn = Turn(TurnKind.OPEN if parent is None else TurnKind.CONTINUE, speaker,
                    conversation.channel if is_group(conversation.channel) else 'discord:'+conversation.channel,
                    addressee=parent_author, instruction=instruction,
                    priority=Priority.BACKGROUND, max_lines=1)
                topic = conversation.seed.fact if parent is None else ''
                meta = TurnMeta('', why='server:'+conversation.seed.kind, topic=topic,
                    photo_seed=(int(conversation.seed.key.removeprefix('photo:'))
                                if parent is None and conversation.seed.kind == 'photo' else None),
                    server_anchor=purpose+' '+topic, server_thread=tuple(conversation.messages),
                    server_conversation=conversation.id, discord=DiscordTarget(parent,None), single_line=True,
                    keep_channel=True, review_required=True, guards=guards,
                    still_valid=lambda c=conversation,g=speaker,p=parent:self._valid(c,g,p))
                if conversation.seed.kind == 'joke':
                    meta.on_discord_record = lambda line,message,now:self._record_joke(conversation,line,message,now)
                elif conversation.seed.kind == 'loot_close' and len(conversation.seed.about) > 1:
                    meta.on_discord_record = lambda line,message,now:self._record_close(conversation,line,message,now)
                opening_photo = meta.photo_seed is not None and dry == 0
                if parent is None and dry == 0 and not opening_photo and conversation.seed.kind != 'joke':
                    seeds.mark_used(app.store, conversation.seed, now=app.clock.now())
                conversation.in_flight, conversation.in_flight_turn = speaker, turn
                try:
                    await app.engine.speak_meta(turn, meta)
                finally:
                    conversation.in_flight = conversation.in_flight_turn = None
                    if conversation.seed.kind in ("joke", "loot_close"):
                        self._settle_joke(conversation, meta)
                added = [r[0] for r in app.store.conn.execute('SELECT id FROM discord_message '
                    'WHERE turn=? AND channel=? AND deleted IS NULL ORDER BY id', (meta.turn_id,conversation.channel))]
                conversation.messages.extend(added)
                if added and conversation.seed.kind == 'group_start':
                    app.store.conn.execute('DELETE FROM meta WHERE key=?',
                        ('groups:first:'+conversation.channel.removeprefix('gdm:'),))
                if opening_photo and (added or meta.photo is not None):
                    # Spent once its photo was offered; one never offered (another photo in
                    # flight) stays for a later conversation.
                    seeds.mark_used(app.store, conversation.seed, now=app.clock.now())
                if parent is None and not added and conversation.seed.openers is not None:
                    conversation.reason = 'opener_dropped'
                    break
                dry = 0 if added else dry+1
                last = speaker
                if dry >= DRY_TURNS:
                    conversation.reason = 'dry'
                    break
            conversation.reason = conversation.reason or 'length'
        except asyncio.CancelledError:
            conversation.reason = conversation.reason or 'cancelled'
            raise
        except Exception as error:
            conversation.reason = 'error'
            log.warning('server_conversation_failed id=%s code=%s', conversation.id, type(error).__name__)
        finally:
            self._finished(conversation)

    def _finished(self, conversation):
        if self._live.pop(conversation.id, None) is None:
            return
        value = dict(id=conversation.id, channel=conversation.channel, seed=conversation.seed.kind,
                     messages=len(conversation.messages), started=conversation.started,
                     reason=conversation.reason or 'cancelled')
        try:
            records.ended(self.app.store, value)
        except Exception as error:
            log.warning('server_receipt_failed id=%s code=%s', conversation.id, type(error).__name__)
        log.info('server_conversation_ended id=%s messages=%d reason=%s',
                 conversation.id, len(conversation.messages), value['reason'])

    async def close(self):
        self._closed = True
        tasks = [c.task for c in self._live.values() if c.task is not None]
        for conversation in tuple(self._live.values()):
            self._end(conversation, 'shutdown')
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Tasks cancelled before their first step never enter the conversation's finally.
        self._live.clear()
