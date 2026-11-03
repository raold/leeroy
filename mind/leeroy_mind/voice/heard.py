"""His speech. A transcript from the voice service becomes his line in the room he spoke in: a
Chat handed to the bridge's dispatcher, so it is handled in order with game chat and takes the
same path as a typed line, through the app's _on_chat to the reply planning. In a private room
with a member it is a whisper between the two, and every whisper rule applies.

A few exact short phrases also reach game chat as commands, said as him (I9, settled item 9),
and never from a private room. Nothing else ever does: a sentence that holds the word is a line
in the room. Words that are a command are stored and answered by nobody, grouped or not (M27,
app._on_chat).

Nothing here raises into the talk loop, and nothing here logs his words."""
import logging
import unicodedata

from leeroy_mind.clock import now_ms
from leeroy_mind.logs import log as event
from leeroy_mind.protocol import Chat, HumanSay
from leeroy_mind.voice import wire
from leeroy_mind.voice.comms import VOICE
from leeroy_mind.voice.rooms import BY_ID, PRIVATE

# dungeon-clear's commands work only during a run (outside one the tank refuses aloud), so they
# go first then; playerbots parses the other map at any time
DC_COMMANDS = {"pull": "dc pause resume", "go": "dc pause resume",
               "hold": "dc pause pause", "stop": "dc pause pause"}
BOT_COMMANDS = {"follow": "follow", "follow me": "follow", "stay": "stay", "stay here": "stay",
                "attack": "attack", "pull": "pull", "hold": "stay", "stop": "stay"}
# What Parakeet writes for "pull" on 3 takes of 4. "pool" is a real raid word, so it counts only
# as a whole utterance while a command applies (Task 9's carry).
SOUNDALIKES = frozenset({"poll", "pool"})
ECHO_S = 10.0  # how long the game has to report a command it was given
INJECTED_KEPT = 32  # a line the app ignored outright never comes back to `stored`


def _phrase(text: str) -> str:
    """His words as the maps key them: lower case, no punctuation, one space between words. A
    question is no order, as it is no typed bot command either: "" for "pull?"."""
    if "?" in text:
        return ""
    bare = "".join(c for c in text.lower() if unicodedata.category(c)[0] != "P")
    return " ".join(bare.split())


def is_command(text: str) -> bool:
    phrase = _phrase(text)
    return phrase in DC_COMMANDS or phrase in BOT_COMMANDS


def command_for(text: str, *, dc_run: bool) -> str | None:
    phrase = _phrase(text)
    return (DC_COMMANDS.get(phrase) if dc_run else None) or BOT_COMMANDS.get(phrase)


def _meant(text: str) -> str:
    return "pull" if _phrase(text) in SOUNDALIKES else text


class HeardHandler:
    def __init__(self, ext):
        self.ext = ext
        self._injected: dict[int, tuple[Chat, wire.Heard, float]] = {}  # id(chat): kept alive
        self._echoes: list[tuple[str, str, float]] = []  # (channel, text, until)

    def _online(self) -> bool:
        app = self.ext.app
        return app.world.human_online and app.session_id is not None

    def _group_channel(self) -> str | None:
        """Where a command goes: his group's channel, None when he is not in one."""
        world = self.ext.app.world
        if world.group is None or not world.in_group(self.ext.app.human_guid):
            return None
        return "raid" if world.group.raid else "party"

    def _partner(self, room: str) -> int:
        """Who he is private with in `room`: 0 when it is no private room or he is alone."""
        return min(self.ext.comms.audience(f"{VOICE}{room}"), default=0) if room in PRIVATE else 0

    # -- what the service sends ----------------------------------------------------------------

    async def on_heard(self, h: wire.Heard) -> None:
        try:
            await self._hear(h)
        except Exception as exc:
            self.ext._failed("heard", exc)

    async def _hear(self, h: wire.Heard) -> None:
        """The room is the one he pressed in, wherever he sits by now: a press off comms comes
        with channel "", and nobody heard it. What he says in a private room gives the game no
        command, as a typed whisper does not, whoever is left in it when the words land."""
        room = h.channel
        if not self._online() or room not in BY_ID or not h.text.strip():
            return
        where = None if room in PRIVATE else self._group_channel()
        text = _meant(h.text) if where else h.text
        self._inject(self._line(room, self._partner(room), text), h)
        command = command_for(text, dc_run=self.ext.dc_run()) if where else None
        if command:
            await self._say(where, command)

    def _line(self, room: str, her: int, text: str) -> Chat:
        """A fresh ts, so it is never read as replayed; the name he chats under, so
        _note_human_name keeps it (M4)."""
        app = self.ext.app
        me = app.world.members.get(app.human_guid)
        return Chat(ts=now_ms(app.clock), channel="whisper" if her else f"{VOICE}{room}",
                    from_guid=app.human_guid,
                    from_name=app.store.get_meta("human_name") or app.engine.human.character_name,
                    to_guid=her, text=text, map=me.map if me else 0, zone=app.world.human_zone)

    def _inject(self, chat: Chat, h: wire.Heard) -> None:
        app = self.ext.app
        self._injected[id(chat)] = chat, h, app.clock.now()
        while len(self._injected) > INJECTED_KEPT:
            del self._injected[next(iter(self._injected))]
        app.bridge.inject(chat)

    async def _say(self, channel: str, command: str) -> None:
        """The bridge's failures and refusals end here (M4). The echo is noted first: the game
        may report the line before its ack is read."""
        app = self.ext.app
        echo = channel, command, app.clock.now() + ECHO_S
        self._echoes.append(echo)
        try:
            ack = await app.bridge.send(HumanSay(id="", channel=channel, text=command))
        except Exception as exc:  # any BridgeError, NotConnected included
            reason = type(exc).__name__
        else:
            if ack.ok:
                return
            reason = ack.error
        if echo in self._echoes:
            self._echoes.remove(echo)
        event("voice_command_failed", level=logging.WARNING, reason=reason)

    def on_ptt(self, p: wire.Ptt) -> None:
        """A press off comms says he is here, and holds nobody: nobody is listening to him."""
        self.ext.app.world.human_last_active = self.ext.app.clock.now()
        self.ext.comms.key(p.down and bool(p.channel))

    async def on_join(self, j: wire.Join) -> None:
        try:
            self._join(j.channel)
        except Exception as exc:
            self.ext._failed("join", exc)

    def _join(self, room: str) -> None:
        ext, app = self.ext, self.ext.app
        rooms, human = ext.rooms, app.human_guid
        refused = rooms.can_enter(human, room) if self._online() else "offline"
        if refused:
            ext.send(wire.Refused(room, refused))
            return
        left = rooms.where.get(human)
        if left == room:
            ext.send(wire.Moved(human, room, rooms._by.get(human, 0)))
            return
        rooms.move(human, room, human)
        ext.send(wire.Moved(human, room, human))
        if left in PRIVATE and rooms.allowed(left) is not None:
            rooms.unlock(left)
            ext.send(rooms.channels_message())
        ext.store.enter(human, room, human, app.clock.now(), app.session_id)

    # -- what the app sees -----------------------------------------------------------------------

    def stored(self, ev: Chat) -> None:
        """The app's row has run for `ev`: if it is a line we injected, its recognition details
        go with the stored line (M4). A row that failed leaves no line from `since` on, so an
        older line with the same words keeps its own."""
        kept = self._injected.pop(id(ev), None)
        if kept is None:
            return
        _, h, since = kept
        store = self.ext.store
        line = store.heard_line(ev.from_guid, ev.channel, ev.text, since)
        if line is None:
            event("voice_heard_unmatched", level=logging.WARNING)
            return
        store.add_heard(line, h.raw, h.confidence, h.speech_ms, h.latency_ms)

    def is_echo(self, ev: Chat) -> bool:
        """The game reporting a command we said for him, as a line he typed. It is no talk, and
        the typed rule alone would not know: `pull` is not among its bot commands. Each command
        answers for one line."""
        now = self.ext.app.clock.now()
        self._echoes = [e for e in self._echoes if e[2] > now]
        for echo in self._echoes:
            if echo[:2] == (ev.channel, ev.text):
                self._echoes.remove(echo)
                return True
        return False
