"""The owner's own line, typed in the guild console and said in game as his (native owner_say,
mod-leeroy's src/Guild/OwnerSay.cpp): the command, the text rule both sides apply, and the
service behind the control endpoint's say op.

The text rule is native's, in its order (the shared fixture owner_say_text.jsonl): clean first
(clean_say_text), then refuse, never cut: nothing left (empty_text), over 255 UTF-8 bytes
(too_long), a leading "." or "!" (command_prefix). The mind alone also sees a string that does
not encode as UTF-8 (a lone surrogate, which only its JSON parser lets through): bad_text.

OwnerSpeech.handle checks what the mind knows (stopping, the bridge, his session, his group, the
whisper target), sends one OwnerSay and turns the answer into the words the console shows.
Native checks again and has the last word (his guild, his group, the limiter). A refusal the
game itself makes after an ok (he is muted, lacks the officer rank) is his own in-game notice:
the console reads it as sent. Logs carry the channel and the outcome, never the text or a name."""
from dataclasses import dataclass

from leeroy_mind.bridge import AckTimeout, BridgeError, NotConnected
from leeroy_mind.logs import log as log_event
from leeroy_mind.protocol import MAX_TEXT_BYTES, clean_say_text, register_command

OWNER_SAY = "owner_say"
UNKNOWN_TYPE = "unknown type"  # native's Err::UnknownType, a server from before owner_say
MAX_CODE_SHOWN = 60

STOPPING = "The mind is stopping."
NOT_IN_GAME = "You're not in game."
BRIDGE_DOWN = "The game bridge is down."
NO_ANSWER = "The game didn't answer in time. Check WoW before sending it again."
BRIDGE_LOST = ("The game bridge went down before the game answered. Check WoW before sending "
               "it again.")
REBUILD = "The game server needs the rebuild that adds console replies."
OFFLINE = "{name} is offline."
WORDS = {
    # native's refusal codes (LeeroyOwnerSay.cpp, Guild/OwnerSay.cpp)
    "human_offline": "You're not in the world right now (loading or offline).",
    "not_in_guild": "You're not in the guild.",
    "not_in_group": "You're not in a group.",
    "not_in_raid": "You're not in a raid.",
    "not_cast": "That member isn't in the guild's cast.",
    "self": "You can't whisper yourself.",
    "rate_limited": "Too fast: wait a moment before the next line.",
    "too_long": "That's too long for WoW chat (255 bytes at most).",
    "empty_text": "There's nothing to send.",
    "command_prefix": "Commands can't be sent from the console.",
    "disabled": "mod-leeroy is off on the game server.",
    # the mind's own
    "bad_text": "That line has a character WoW chat can't carry.",
}


@dataclass(frozen=True, slots=True)
class OwnerSay:
    """Native owner_say: a line said as him on guild, officer, party, raid, or whispered to
    to_guid (a cast member, non-zero exactly for a whisper)."""
    id: str
    channel: str
    text: str
    to_guid: int = 0


def register() -> None:
    """Register owner_say with the bridge protocol; safe to call again."""
    register_command(OWNER_SAY, OwnerSay)


def text_verdict(text: str) -> tuple[str, str]:
    """("ok", what goes to the game) or (a refusal code, "")."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "bad_text", ""
    cleaned = clean_say_text(text)
    if not cleaned:
        return "empty_text", ""
    if len(cleaned.encode("utf-8")) > MAX_TEXT_BYTES:
        return "too_long", ""
    if cleaned.startswith((".", "!")):
        return "command_prefix", ""
    return "ok", cleaned


class OwnerSpeech:
    """The control endpoint's say op (endpoint.say_args has checked and cleaned its arguments:
    {"channel": guild | officer | group | whisper, "text"[, "to"]})."""

    def __init__(self, app):
        self.app = app
        register()

    async def handle(self, args: dict) -> dict:
        outcome, error = "failed", None
        try:
            outcome, error = await self._say(args)
        finally:
            log_event("owner_say", channel=args["channel"], outcome=outcome)
        return {"ok": True, "message": "sent"} if error is None else {"ok": False, "error": error}

    async def _say(self, args: dict) -> tuple[str, str | None]:
        """("sent", None), or (the refusal's log token, the words the console shows)."""
        app, world = self.app, self.app.world
        if app.stopping:
            return "stopping", STOPPING
        # Before his session: the app marks him offline when the bridge drops, and nobody
        # can tell then whether he is in game
        if not app.bridge.greeted:
            return "bridge_down", BRIDGE_DOWN
        if app.session_id is None or not world.human_online:
            return "not_in_game", NOT_IN_GAME
        channel, to = args["channel"], 0
        if channel == "group":
            if world.group is None or not world.in_group(app.human_guid):
                return "not_in_group", WORDS["not_in_group"]
            channel = "raid" if world.group.raid else "party"
        elif channel == "whisper":
            to = args["to"]
            if to == app.human_guid:
                return "self", WORDS["self"]
            if to not in world.cast_guids():
                return "not_cast", WORDS["not_cast"]
            if to not in world.online:
                return "target_offline", self._offline(to)
        try:
            ack = await app.bridge.send(OwnerSay(id="", channel=channel, text=args["text"],
                                                 to_guid=to))
        except NotConnected:
            return "bridge_down", BRIDGE_DOWN
        except AckTimeout:
            return "timeout", NO_ANSWER
        except BridgeError:  # the connection dropped with the line on its way
            return "bridge_lost", BRIDGE_LOST
        if ack.ok:
            return "sent", None
        code = ack.error
        if code.startswith(UNKNOWN_TYPE):
            return "unknown_type", REBUILD
        if code == "target_offline":
            return code, self._offline(to)
        if code in WORDS:
            return code, WORDS[code]
        return "refused", f"The game refused it ({code[:MAX_CODE_SHOWN]})."

    def _offline(self, guid: int) -> str:
        member = self.app.world.members.get(guid)
        return OFFLINE.format(name=member.name if member is not None and member.name
                              else "That member")
