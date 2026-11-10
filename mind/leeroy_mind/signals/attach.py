"""Small observation hooks; all persistence happens on Capture's owned worker."""
import logging
import re

from leeroy_mind.protocol import AfkEvent, Boss, Group, Hello, Login, Logout, TextEmote, Zone
from leeroy_mind.talk.emote_names import FRIENDLY_EMOTES, ROMANTIC_EMOTES, RUDE_EMOTES
from leeroy_mind.tuning.dials import applies
from leeroy_mind.tuning.effective import dials_of
from leeroy_mind.tuning.groups import matches

from .capture import Capture, Observation
from .config import load_signals_config
from .store import REPLY_LEN_SCALE

log = logging.getLogger(__name__)
NEGATIVE_NUDGE = re.compile(r"\b(?:stop|avoid|less|quit|don't|do not|no more|tone down)\b", re.I)


class Signals:
    def __init__(self, app, path):
        self.app = app
        self.capture = Capture(path, app.clock)
        self.fights = set()
        self.group = None
        self.owner_location = None

    def offer(self, kind, ids=(), values=()):
        return self.capture.offer(Observation(kind, self.app.clock.now(), self.app.human_guid,
                                              tuple(ids), tuple(values)))

    def sent(self, line_id, chat, *, addressed, heard):
        world = self.app.world
        self.offer('sent', (line_id,), (addressed, world.in_group(chat.from_guid) and
                world.in_group(self.app.human_guid), heard and self._owner_visible(chat)))

    def _owner_visible(self, chat):
        world, owner = self.app.world, self.app.human_guid
        channel = chat.channel
        if channel.startswith('voice:'):
            comms = self.app.engine.comms
            return comms is not None and channel == 'voice:' + (comms.human_room() or '')
        if not owner or not world.human_online:
            return False
        if channel == 'guild':
            return True  # cast audiences intentionally omit the owner
        if channel == 'whisper':
            return chat.to_guid == owner
        if channel == 'officer':
            return world.is_officer(owner)
        grouped = world.in_group(owner) and world.in_group(chat.from_guid)
        if channel in ('party', 'raid', 'raid_warning'):
            return grouped
        if channel in ('say', 'yell') and grouped and self.owner_location is not None:
            map_id, zone, at = self.owner_location
            return (chat.zone > 0 and (map_id, zone) == (chat.map, chat.zone)
                    and zone == world.human_zone and 0 <= self.app.clock.now() - at <= 120)
        return False  # unsupported external channels need their own audience adapter

    def owner_line(self, line_id, length, *, command=False, chat=None):
        if chat is not None and chat.channel in ('guild', 'officer', 'party', 'raid', 'raid_warning', 'whisper', 'say', 'yell'):
            self.owner_location = (chat.map, chat.zone, self.app.clock.now())
        self.offer('owner', (line_id,), (min(length / REPLY_LEN_SCALE, 1), command))

    def played(self, msg):
        if msg.state == 'dropped' and msg.reason == 'muted':
            self.offer('muted', (msg.id,))

    def combat_observed(self, in_combat):
        """A future authoritative observer may supply bool; None means unknown. The
        current bridge's boss events prove combat, but never general outdoor rest."""
        if in_combat is not None and type(in_combat) is not bool:
            raise ValueError('invalid_combat_observation')
        self.offer('combat', values=(in_combat,))

    async def event(self, ev):
        app, owner = self.app, self.app.human_guid
        if app.is_backlog(ev):
            return
        if isinstance(ev, Hello):
            self.owner_location = None
            self.fights.clear()
            self.combat_observed(None)
            self.offer('presence', values=(bool(ev.human_guid),))
        elif isinstance(ev, (Login, Logout)) and ev.guid == owner:
            self.owner_location = None
            self.offer('presence', values=(isinstance(ev, Login),))
        elif isinstance(ev, AfkEvent) and ev.guid == owner:
            self.offer('afk', values=(ev.on,))
        elif isinstance(ev, Zone) and ev.guid == owner:
            self.owner_location = None  # this packet has no map; do not reuse an old map
            self.offer('activity')  # native publishes zone changes, not every movement
        elif isinstance(ev, Group):
            current = (ev.leader_guid, ev.raid, tuple(sorted(ev.members)))
            if self.group != current:
                self.group = current
                self.offer('group')
        elif isinstance(ev, TextEmote) and ev.guid == owner and ev.target_guid != owner:
            for kind, names in (('emote_friendly', FRIENDLY_EMOTES), ('emote_romantic', ROMANTIC_EMOTES),
                                ('emote_rude', RUDE_EMOTES)):
                if ev.emote in names:
                    self.offer('emote', (ev.target_guid,), (kind,))
                    break
        elif isinstance(ev, Boss):
            key = ev.map, ev.instance_id, ev.boss_id
            involved = owner in ev.members or any(app.world.in_group(g) for g in ev.members)
            if ev.state == 'engaged' and involved:
                self.fights.add(key)
            elif ev.state in ('killed', 'wiped'):
                self.fights.discard(key)
            self.combat_observed(True if self.fights else None)

    async def connection(self, up):
        if not up:
            self.owner_location = None
            self.offer('presence', values=(False,))

    def _members(self, args, pic, *, nudge=False):
        scope = args['target_kind'] if nudge else args['scope']
        if scope == 'member':
            return (int(args['target']),)
        group = self.app.personas.tuning.groups.get(args['target']) if scope == 'group' else None
        if group is None:
            return ()
        return tuple(g for g, p in pic.personas.items() if g != pic.human and g in pic.cast
                     and (group.builtin or matches(group.where, p, pic.cast[g])))

    def tuning_values(self, args, pic):
        if args.get('dial') not in ('talkative', 'forward', 'drama'):
            return {}
        return {g: dials_of(p).level(args['dial']) for g in self._members(args, pic)
                if (p := self.app.personas.get(g)) is not None and applies(args['dial'], p)}

    def tuned(self, before, args):
        lowered = tuple(g for g, value in before.items()
                        if (p := self.app.personas.get(g)) is not None
                        and dials_of(p).level(args['dial']) < value)
        if lowered:
            self.offer('dial_down', lowered)

    def nudged(self, args, pic):
        # A positive instruction is not negative feedback. This deliberately narrow cue
        # rule leaves ambiguous intentions unclassified; it never queues the words.
        if NEGATIVE_NUDGE.search(args['text']):
            self.offer('nudged_against', self._members(args, pic, nudge=True), (args['scope'],))

    async def feedback(self, args):
        return await self.capture.request(Observation('feedback', self.app.clock.now(), self.app.human_guid,
            (args['line_id'],), (args['value'], tuple(args['reasons']))))


def attach_signals(app):
    cfg = load_signals_config(app.data_dir / 'mind.toml')
    if not cfg.enabled:
        return None
    path = app.store.conn.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        log.warning('signals_unavailable: reason=no_database_file')
        return None
    signals = Signals(app, path)
    app.engine.signals = signals
    for name in ('hello', 'login', 'logout', 'afk', 'zone', 'text_emote', 'boss', 'group'):
        app.register_event_handler(name, signals.event)
    app.register_connection_hook(signals.connection)
    app.register_background(signals.capture.run)
    app.register_shutdown_drain(signals.capture.close)
    from .stage import stage_signals
    app.night_runner.add_stage('signals', stage_signals, after='threads', model=None)
    log.info('signals_on')
    return signals
