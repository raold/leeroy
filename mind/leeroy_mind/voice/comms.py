"""Who is on comms tonight, and where a member's line goes: spoken in their room, typed, or
unheard in a room he is not in. Comms answers the pipeline's questions from the seats and the
service's last word; with the service down it answers as if voice did not exist, so everyone
types as in phase 1."""
import logging
import random
from typing import Callable

from leeroy_mind.logs import log as event
from leeroy_mind.personas.schema import Persona
from leeroy_mind.talk.turns import Turn
from leeroy_mind.voice import route, wire
from leeroy_mind.voice.client import VoiceDown
from leeroy_mind.voice.rooms import PRIVATE

VOICE = "voice:"
SPOKEN_HINT = ("You're talking out loud on vent: say it the way you'd say it, with no chat "
               "abbreviations or emoticons.")
GAME_CHANNELS = frozenset({"guild", "party", "raid", "raid_warning", "say"})
RAID_CHANNELS = frozenset({"raid", "raid_warning"})
SPEAKS_KEPT = 256  # the turn ids whose speak ids are kept, for `after`
HOLD_POLL_S = 0.05  # how often a held turn looks at his key
PTT_GRACE_S = 0.3  # a held turn waits this much longer once he lets go
AGE_OUT_S = 60.0  # a line unanswered this long after its expiry is given up on


def roll_on_comms(night: str, personas: dict[int, Persona], *, raider_share: float,
                  social_share: float, is_officer: Callable[[int], bool],
                  has_voice: Callable[[int], bool]) -> set[int]:
    """Officers are always on; raiders with chance `raider_share`, socials `social_share`; each
    member's draw is seeded by guid and night, so the same night rolls the same set. A member
    without a voice spec is never on."""
    on = set()
    for guid, p in personas.items():
        if not has_voice(guid):
            continue
        if is_officer(guid) or p.play.guild_role == "officer":
            on.add(guid)
            continue
        share = raider_share if p.play.guild_role == "raider" else social_share
        if random.Random(f"{guid}:{night}:comms").random() < share:
            on.add(guid)
    return on


def typed(turn: Turn, meta) -> tuple[str, int, int | None]:
    """Where a turn goes when it can't be spoken. Back to the channel it came on: raid, party
    and say stay there. One that arrived on a voice channel has none of its own, so it types in
    guild, the Officers room in /o (N6): a reply to his spoken line still reaches him. Only a
    voice channel moves: a line stored as a whisper is never sent back to a public channel, or
    a private room's talk would go out in guild."""
    if not turn.channel.startswith(VOICE):
        return turn.channel, turn.to_guid, turn.addressee
    if meta.typed_on is not None:
        return *meta.typed_on, turn.addressee
    return ("officer" if turn.channel == f"{VOICE}officers" else "guild"), 0, turn.addressee


class Comms:
    def __init__(self, ext):
        self.ext = ext
        self.ptt_down = False  # his key, as the service last said
        self._grace_until = 0.0  # held turns wait until then after he lets go
        self._speaks: dict[str, str] = {}  # turn id -> speak id, of the lines sent
        self._unanswered: dict[str, float] = {}  # speak id -> when it is given up on

    # -- the service and the seats -----------------------------------------------------------

    def available(self) -> bool:
        """Greeted, its last status ok (M22: the output open and the engine ready, the mic
        apart) and his session online. Read fresh each time: the service reports ok False
        while he is off because its output is closed, which is no verdict on the service. It
        latches nothing, and nothing sent to the service waits for this: `session` goes out
        regardless, and the status that follows it turns this True."""
        client, world = self.ext.client, self.ext.app.world
        status = client.status
        return bool(client.greeted and status is not None and status.ok and world.human_online)

    def room_of(self, guid: int) -> str | None:
        """None when off comms, or when nobody is on comms: voice down."""
        return self.ext.rooms.where.get(guid) if self.available() else None

    def human_room(self) -> str | None:
        return self.ext.rooms.human_room()

    def hearable(self, guid: int) -> bool:
        """He can hear them where they are: off comms (typing) or in his room."""
        room = self.room_of(guid)
        return room is None or room == self.human_room()

    def unheard(self, channel: str, meta) -> bool:
        """A line he has no way of hearing (I8, N3): only one on a voice channel that is
        elsewhere or in a room he is not in. A typed line and a whisper are always heard."""
        if not channel.startswith(VOICE):
            return False
        return meta.elsewhere or channel.removeprefix(VOICE) != self.human_room()

    def audience(self, channel: str) -> frozenset[int]:
        """The cast sitting in a `voice:` channel's room."""
        room = channel.removeprefix(VOICE)
        return self.ext.rooms.occupants(room) & self.ext.app.world.cast_guids()

    def _private_with_him(self, room: str | None) -> bool:
        return room in PRIVATE and room == self.human_room()

    # -- where a turn goes -------------------------------------------------------------------

    def place(self, turn: Turn, meta) -> tuple[str, int, int | None]:
        """The turn's (channel, to_guid, addressee). Settled items 4 and 5: a member on comms
        speaks in their room, an answer to a line he typed is typed, and a member in a private
        room with him is stored as a whisper between the two, also when the turn was on a
        room's channel: every whisper rule then applies. A raid turn is the exception: it
        stays on the raid channel, typed where the raid sees it. A turn from a game channel
        stays on it while its speaker sits in a room he is not in, or a raid call would be
        heard by nobody and typed nowhere; talk that belongs in a room (elsewhere, or one that
        arrived on its channel) stays there."""
        channel, room = turn.channel, self.room_of(turn.speaker)
        kept = channel, turn.to_guid, turn.addressee
        on_voice = channel.startswith(VOICE)
        if room is None:
            return typed(turn, meta)
        if channel == "whisper" or (not on_voice and self._answers_typed(turn)):
            return kept
        if self._private_with_him(room):
            if (meta.typed_on[0] if meta.typed_on else channel) in RAID_CHANNELS:
                return typed(turn, meta)
            human = self.ext.app.world.human_guid
            return "whisper", human, human
        if (room != self.human_room() and not meta.elsewhere
                and (not on_voice or meta.typed_on is not None)):
            return typed(turn, meta)
        if on_voice:
            return f"{VOICE}{room}", turn.to_guid, turn.addressee
        if channel == "officer":
            return (f"{VOICE}officers", 0, turn.addressee) if room == "officers" else kept
        return (f"{VOICE}{room}", 0, turn.addressee) if channel in GAME_CHANNELS else kept

    def _answers_typed(self, turn: Turn) -> bool:
        if not turn.trigger_line:
            return False
        rows = self.ext.app.store.chat_lines([turn.trigger_line])
        return bool(rows) and not rows[0].channel.startswith(VOICE)

    def voiced(self, turn: Turn, meta) -> bool:
        """Whether a placed turn is spoken, for the model's turns and templates alike (C1):
        every voice channel, and a whisper to him from someone on comms, unless it answers his
        typed whisper; in a private room with him even that. An invite ask only while he has
        the voice UI open, where he can answer it."""
        if turn.channel.startswith(VOICE):
            return True
        human = self.ext.app.world.human_guid
        if turn.channel != "whisper" or not human or turn.to_guid != human:
            return False
        room = self.room_of(turn.speaker)
        if room is None:
            return False
        if meta.why.startswith("invite") and not self.ext.client.status.ui:
            return False
        return self._private_with_him(room) or not meta.whisper_reply

    def _room(self, turn: Turn) -> str:
        """A voiced line's room, or "whisper" (speak.channel, voice_line.room)."""
        if turn.channel.startswith(VOICE):
            return turn.channel.removeprefix(VOICE)
        room = self.room_of(turn.speaker)
        return room if self._private_with_him(room) else "whisper"

    def tier(self, turn: Turn, channel: str) -> str:
        app = self.ext.app
        world, group = app.world, app.world.group
        return route.tier_of(turn, room=channel.removeprefix(VOICE), lead=app.raid.lead(),
                             officer=world.is_officer(turn.speaker),
                             in_raid=bool(group is not None and group.raid
                                          and world.in_group(turn.speaker)),
                             human=world.human_guid)

    def key(self, down: bool) -> None:
        """His key, as the service says it. Letting go starts the grace."""
        if self.ptt_down and not down:
            self._grace_until = self.ext.app.clock.now() + PTT_GRACE_S
        self.ptt_down = down

    def release(self) -> None:
        """A press the service can no longer end, grace included, holds nothing (NP5)."""
        self.ptt_down, self._grace_until = False, 0.0

    def _talking(self) -> bool:
        return self.ptt_down or self.ext.app.clock.now() < self._grace_until

    async def hold(self, turn: Turn, channel: str) -> None:
        """Settled item 13: nobody composes an answer to his last line while he is still
        talking. A would-be p2-p4 turn of the model's waits for his key and PTT_GRACE_S after
        it, and no longer than its deadline; a template, a p0 and a p1 never do. A service
        gone ends the wait."""
        if turn.template is not None or self.tier(turn, channel) in ("p0", "p1"):
            return
        clock = self.ext.app.clock
        while self._talking() and self.available() and clock.now() <= turn.deadline:
            await clock.sleep(HOLD_POLL_S)

    # -- a line ------------------------------------------------------------------------------

    def _speak_id(self, meta) -> str:
        return f"{self.ext.run_id}.{meta.turn_id}"

    def _speak(self, turn: Turn, meta, text: str) -> wire.Speak:
        app, room = self.ext.app, self._room(turn)
        p = app.personas.get(turn.speaker)
        return route.speak_for(
            turn, meta, text, speak_id=self._speak_id(meta), room=room,
            tier=self.tier(turn, turn.channel),
            attitude=p.romance.stance if meta.romance and p is not None else "none",
            state=app.engine._altered_of(turn.speaker).value,
            after=self._speaks.get(meta.after, ""), now=app.clock.now(),
            human=app.world.human_guid)

    def deliver(self, turn: Turn, meta, text: str) -> bool:
        """Hand a line to the service, before the chat log takes it, so one that never gets
        there leaves no sign of having been said and the pipeline types it. False then, with
        its voice_line row dropped (line id 0: it has no line in a room). A room he is not in
        takes nothing: True for talk that belongs there, which log_line marks unheard, False
        for a turn from a game channel whose room he left a moment ago, which goes back to that
        channel. Never raises, and logs by name alone: `text` stays out of the log."""
        try:
            if self.unheard(turn.channel, meta):
                return bool(meta.elsewhere or meta.typed_on is None)
            speak = self._speak(turn, meta, text)
            self.ext.client.send(speak)
        except VoiceDown:
            reason = "voice_down"
        except Exception as exc:  # WireError, a bug: the line still goes out typed
            reason = type(exc).__name__
            event("voice_deliver_failed", level=logging.WARNING, reason=reason)
        else:
            self._speaks[meta.turn_id] = speak.id
            while len(self._speaks) > SPEAKS_KEPT:
                del self._speaks[next(iter(self._speaks))]
            self._unanswered[speak.id] = speak.expires_ms / 1000 + AGE_OUT_S
            return True
        try:
            self.ext.store.add_line(self._speak_id(meta), 0, self._room(turn), "dropped",
                                    self.ext.app.clock.now(), reason=reason)
        except Exception as exc:
            event("voice_deliver_failed", level=logging.WARNING, reason=type(exc).__name__)
        return False

    def log_line(self, turn: Turn, meta, line_id: int) -> None:
        """The line is in the chat log: its voice_line row, sent or unheard."""
        state = "unheard" if self.unheard(turn.channel, meta) else "sent"
        self.ext.store.add_line(self._speak_id(meta), line_id, self._room(turn), state,
                                self.ext.app.clock.now())

    def played(self, msg: wire.Played) -> None:
        self._unanswered.pop(msg.id, None)
        self.ext.store.set_line(msg.id, msg.state, msg.reason or None, self.ext.app.clock.now())

    def age_out(self) -> None:
        """The tick: a line the service never answered for (it refuses a bad speak with an
        error, no played, and a line in the air dies with the connection) stops reading as
        sent, so nothing takes it for a line he heard."""
        now = self.ext.app.clock.now()
        for speak_id, at in list(self._unanswered.items()):
            if now >= at:
                del self._unanswered[speak_id]
                self.ext.store.set_line(speak_id, "dropped", "no_ack", now)
