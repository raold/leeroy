"""Room moves and short visits. Every move updates seats, presence and the service."""
from dataclasses import dataclass

from leeroy_mind.clock import night_of
from leeroy_mind.talk.budget import DayPhase, day_phase
from leeroy_mind.talk.states import substance_of
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.voice import wire
from leeroy_mind.voice.rooms import BY_ID, CLASS_ROOM, HOME, PRIVATE, RAID_ROOMS
from leeroy_mind.voice.together import RoomTogether

HUDDLE_EVERY_S = 3600
HUDDLE_S = (300, 600)
HEALER_HUDDLE_S = (30, 60)
SMOKE_P = 0.15
IRL_P = 0.05
DRIFT_P = 0.03
DRIFT_S = (300, 900)
PENALTY_P = 0.15
PENALTY_S = (120, 240)
INVITE_WAIT_S = 180
RETURN_S = 60
INVITE_ASK = "You'd like a word alone: ask {name} to hop into {room} with you, in a few words."
TICK_S = 10
PERIOD_S = 600
_BUSY = {"combat", "wipe_silence", "wipe_debrief"}
# These seats belong to short-lived policies, whose timers/locks cannot survive a crash.
_TRANSIENT = {*PRIVATE, "officers", "healers", "penalty_box", "smoke_room", "irl",
              "lore", "achievements", "pvp", *CLASS_ROOM.values()}


@dataclass
class Visit:
    room: str
    previous: str
    until: float
    kind: str


@dataclass
class Invitation:
    guid: int
    room: str
    previous: str
    since: float
    joined: bool = False
    left: float | None = None


class Movement:
    def __init__(self, ext, *, rng):
        self.ext, self.rng = ext, rng
        self.together = RoomTogether(ext)
        self.visits: dict[int, Visit] = {}
        self.afk_returns: dict[int, str] = {}
        self.invites: dict[int, Invitation] = {}
        self._offline_members: set[int] = set()
        self._drag_lead = 0
        self._penalty_night = None
        self._phase = str(ext.app.raid.phase(ext.app.clock.now()))
        self._pace(ext.app.clock.now())

    def _pace(self, now):
        self._next_tick = now
        self._next_period = now + PERIOD_S
        self._next_huddle = now + HUDDLE_EVERY_S
        self._next_invite = now + PERIOD_S

    def reset(self, now):
        self.together.close_all(now)
        for invitation in self.invites.values():
            self.ext.rooms.unlock(invitation.room)
        self.visits.clear()
        self.afk_returns.clear()
        self.invites.clear()
        self._offline_members.clear()
        self._drag_lead = 0
        self._phase = str(self.ext.app.raid.phase(now))
        self._pace(now)

    def prepare_shutdown(self, now):
        """End owned visits before the extension saves seats; never save a lost timer/lock."""
        self.together.close_all(now)
        for guid in list(self.visits):
            self._end_visit(guid)
        for guid in list(self.invites):
            self._end_invite(guid, now)
        for guid, previous in list(self.afk_returns.items()):
            if self.ext.rooms.where.get(guid) == "afk":
                self.move(guid, self._return_room(guid, previous))
        # Private locks are intentionally transient, including when he stayed in the room.
        for guid, room in list(self.ext.rooms.where.items()):
            if room in PRIVATE:
                self.move(guid, HOME)
        self.reset(now)

    def restored_seats(self, seats, *, crashed):
        """Normalize orphaned policy seats before reopening presence after a restart.

        A clean shutdown already returned owned visits, so deliberate seats survive it.
        After a crash no timer or private lock remains: cast policy seats return home and
        normal group reconciliation can pull them back into raid. His ordinary rooms stay.
        """
        app = self.ext.app
        restored = {}
        for guid, (room, by) in seats.items():
            afk = app.world.human_afk if guid == app.human_guid else guid in app.world.afk
            stale_afk = room == "afk" and not afk
            orphaned = crashed and (room in PRIVATE or (guid != app.human_guid and room in _TRANSIENT))
            restored[guid] = (HOME, 0) if stale_afk or orphaned else (room, by)
        return restored

    def on_hello(self):
        """The bridge's new snapshot clears AFK flags even when the session is unchanged."""
        ext, app = self.ext, self.ext.app
        for guid, room in list(ext.rooms.where.items()):
            afk = app.world.human_afk if guid == app.human_guid else guid in app.world.afk
            if room == "afk" and not afk:
                previous = self.afk_returns.pop(guid, HOME)
                self.move(guid, self._return_room(guid, previous))
        self.tick_rooms(app.clock.now())

    def on_connection(self, up):
        now = self.ext.app.clock.now()
        if up:
            self.together.tick(now)
        else:
            self.together.close_all(now)

    def on_logout(self, guid):
        now = self.ext.app.clock.now()
        if guid != self.ext.app.human_guid and guid in self.ext.on_comms:
            self._offline_members.add(guid)
        self.together.before_move(guid, now)
        if guid == self.ext.app.human_guid:
            self.reset(now)
        elif guid in self.invites:
            self._end_invite(guid, now, return_member=False)
        self.visits.pop(guid, None)
        self.afk_returns.pop(guid, None)
        self.ext.store.leave(guid, now)
        self.ext.rooms.leave(guid)
        self.ext.on_comms.discard(guid)
        if guid != self.ext.app.human_guid:
            self.ext.send(wire.Moved(guid, ""))

    def on_login(self, guid):
        ext, app = self.ext, self.ext.app
        if (guid not in self._offline_members or guid not in app.world.online
                or not app.world.human_online or app.session_id is None):
            return
        self._offline_members.remove(guid)
        ext.on_comms.add(guid)
        room = "raid" if app.world.in_group(app.human_guid) and app.world.in_group(guid) else HOME
        by = app.raid.lead() or 0 if room == "raid" else 0
        ext.rooms.move(guid, room, by)
        ext.store.enter(guid, room, by, app.clock.now(), app.session_id)
        ext.send(wire.Moved(guid, room, by))
        self.together.tick(app.clock.now())

    def move(self, guid, room, by=0):
        ext, now = self.ext, self.ext.app.clock.now()
        if guid not in ext.rooms.where or ext.rooms.where[guid] == room or ext.rooms.can_enter(guid, room):
            return False
        self.together.before_move(guid, now)
        ext.rooms.move(guid, room, by)
        ext.store.enter(guid, room, by, now, ext.app.session_id)
        ext.send(wire.Moved(guid, room, by))
        self.together.tick(now)
        return True

    def _return_room(self, guid, previous):
        app = self.ext.app
        if previous in RAID_ROOMS and not app.world.in_group(guid):
            return HOME
        if previous in PRIVATE and not (self.ext.rooms.occupants(previous) - {guid}):
            return HOME
        return previous if self.ext.rooms.can_enter(guid, previous) is None else HOME

    def on_group(self, ev):
        ext, app = self.ext, self.ext.app
        group = set(ev.members) | ({ev.leader_guid} if ev.leader_guid else set())
        if app.human_guid in group and group & ext.on_comms:
            lead = app.raid.lead() or (ev.leader_guid if ev.leader_guid in app.world.cast_guids() else 0)
            self._drag_lead = lead
            wanted = (group & ext.on_comms) | {app.human_guid}
            for guid in sorted(wanted):
                if ext.rooms.where.get(guid) not in (*PRIVATE, "afk"):
                    if self.move(guid, "raid", lead):
                        self.visits.pop(guid, None)
            for guid in set(ext.rooms.occupants("raid")) - wanted:
                self.move(guid, HOME, lead)
        else:
            for guid in sorted(ext.rooms.occupants("raid") | ext.rooms.occupants("raid2")):
                if self.move(guid, HOME, self._drag_lead):
                    self.visits.pop(guid, None)
        self.together.tick(app.clock.now())

    def on_afk(self, ev):
        ext = self.ext
        if ev.guid not in ext.rooms.where:
            return
        if ev.on:
            if ext.rooms.where[ev.guid] != "afk":
                visit = self.visits.pop(ev.guid, None)
                invitation = self.invites.get(ev.guid)
                previous = invitation.previous if invitation else (visit.previous if visit else ext.rooms.where[ev.guid])
                if invitation:
                    self._end_invite(ev.guid, ext.app.clock.now(), return_member=False)
                self.afk_returns[ev.guid] = previous
                self.move(ev.guid, "afk")
        elif ext.rooms.where[ev.guid] == "afk":
            room = self.afk_returns.pop(ev.guid, HOME)
            self.move(ev.guid, self._return_room(ev.guid, room))

    def _members(self):
        ext, app = self.ext, self.ext.app
        return sorted(g for g in ext.on_comms & app.world.online
                      if g in ext.rooms.where and g not in app.world.afk
                      and ext.rooms.where[g] not in (*PRIVATE, "afk")
                      and g not in self.visits and g not in self.invites)

    def _visit(self, guid, room, duration, kind, now, by=0):
        previous = self.ext.rooms.where[guid]
        if self.move(guid, room, by):
            self.visits[guid] = Visit(room, previous, now + duration, kind)

    def _end_visit(self, guid, *, combat=False):
        visit = self.visits.pop(guid)
        if self.ext.rooms.where.get(guid) != visit.room:
            return  # a deliberate room move is never undone by an old timer
        room = "raid" if combat and self.ext.app.world.in_group(guid) else self._return_room(guid, visit.previous)
        self.move(guid, room, self._drag_lead if room == "raid" else 0)

    def _end_invite(self, guid, now, *, return_member=True):
        invitation = self.invites.pop(guid)
        self.ext.rooms.unlock(invitation.room)
        self.ext.send(self.ext.rooms.channels_message())
        if return_member and self.ext.rooms.where.get(guid) == invitation.room:
            self.move(guid, self._return_room(guid, invitation.previous))

    def tick_rooms(self, now):
        ext = self.ext
        for guid, invitation in list(self.invites.items()):
            if ext.rooms.where.get(guid) != invitation.room:
                self._end_invite(guid, now, return_member=False)
                continue
            if not invitation.joined and now >= invitation.since + INVITE_WAIT_S:
                self._end_invite(guid, now)
                continue
            if ext.rooms.human_room() == invitation.room:
                invitation.joined, invitation.left = True, None
            elif invitation.joined:
                if invitation.left is None:
                    invitation.left = now
                    ext.rooms.unlock(invitation.room)
                    ext.send(ext.rooms.channels_message())
                if now >= invitation.left + RETURN_S:
                    self._end_invite(guid, now)
        self.together.tick(now)

    def tick(self, now):
        ext, app = self.ext, self.ext.app
        phase = str(app.raid.phase(now))
        for guid, visit in list(self.visits.items()):
            pulled = phase == "combat" and visit.kind in {"officers", "healers"}
            if pulled or now >= visit.until:
                self._end_visit(guid, combat=pulled)
        self.tick_rooms(now)
        ended_debrief = self._phase == "wipe_debrief" and phase != "wipe_debrief"
        self._phase = phase
        if not ext.comms.available():
            return
        if ended_debrief and phase not in _BUSY:
            for guid in self._members():
                p = app.personas.get(guid)
                if app.world.in_group(guid) and p and p.play.raid_role == "healer":
                    self._visit(guid, "healers", self.rng.uniform(*HEALER_HUDDLE_S), "healers", now)
        if now < self._next_tick:
            return
        self._next_tick = now + TICK_S
        if phase in _BUSY:
            return
        if now >= self._next_huddle:
            officers = [g for g in self._members() if g in ext._officers() and g != app.raid.lead()]
            if len(officers) >= 2:
                chosen = self.rng.sample(officers, self.rng.randint(2, min(4, len(officers))))
                duration = self.rng.uniform(*HUDDLE_S)
                for guid in chosen:
                    self._visit(guid, "officers", duration, "officers", now)
                self._next_huddle = now + HUDDLE_EVERY_S
        night = night_of(app.clock.local())
        officers = [g for g in self._members() if g in ext._officers()]
        victims = [g for g in self._members() if g not in ext._officers() and ext.rooms.where[g] in {HOME, "raid"}]
        if night != self._penalty_night and officers and victims:
            self._penalty_night = night
            if self.rng.random() < PENALTY_P:
                self._visit(self.rng.choice(victims), "penalty_box", self.rng.uniform(*PENALTY_S),
                            "penalty", now, self.rng.choice(officers))
        if now < self._next_period:
            return
        self._next_period = now + PERIOD_S
        local = app.clock.local()
        late = day_phase(local) in {DayPhase.LATE, DayPhase.DEAD}
        for guid in self._members():
            if app.world.in_group(guid):
                continue
            p = app.personas.get(guid)
            if p is None:
                continue
            if late and substance_of(p, app.engine._altered_of(guid), local) in {"tobacco", "weed", "alcohol"}:
                if self.rng.random() < SMOKE_P:
                    self._visit(guid, "smoke_room", self.rng.uniform(600, 1200), "smoke", now)
                    continue
            if late and p.real_life.night_owl and self.rng.random() < IRL_P:
                self._visit(guid, "irl", self.rng.uniform(600, 1200), "irl", now)
                continue
            if phase == "none" and self.rng.random() < DRIFT_P:
                member = app.world.members.get(guid)
                room = {"lore": "lore", "collector": "achievements", "competitor": "pvp"}.get(p.game.thinks_as)
                if p.game.thinks_as == "theorycrafter" and member is not None:
                    room = CLASS_ROOM.get(member.class_)
                if room:
                    self._visit(guid, room, self.rng.uniform(*DRIFT_S), "drift", now)

    async def invite_tick(self, now):
        if now < self._next_invite:
            return
        self._next_invite = now + PERIOD_S
        ext, app = self.ext, self.ext.app
        room = ext.rooms.free_private()
        if (not ext.comms.available() or room is None or ext.rooms.human_room() in (*PRIVATE, "afk", None)
                or app.world.human_afk or str(app.raid.phase(now)) in _BUSY or self.invites):
            return
        candidates = {g: (app.personas.get(g), app.store.get_relationship(g, app.human_guid))
                      for g in self._members() if app.personas.get(g) is not None}
        guid = app.engine.whispers.maybe(now, "idle", candidates)
        if guid not in candidates:
            return
        previous = ext.rooms.where[guid]
        ext.rooms.lock(room, {guid, app.human_guid})
        if not self.move(guid, room):
            ext.rooms.unlock(room)
            return
        self.invites[guid] = Invitation(guid, room, previous, now)
        ext.send(ext.rooms.channels_message())
        ext.send(wire.Invite(guid, room))
        name = app.engine.human.called if hasattr(app.engine, "human") else "them"
        await app.engine.speak(Turn(kind=TurnKind.WHISPER, speaker=guid, channel="whisper",
                                   to_guid=app.human_guid, addressee=app.human_guid,
                                   instruction=INVITE_ASK.format(name=name, room=BY_ID[room].name),
                                   deadline=now + INVITE_WAIT_S), why="invite:private")
