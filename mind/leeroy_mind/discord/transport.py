"""The guild's Discord as the talk pipeline's third transport, beside the bridge's `say` and the
voice service. A turn whose channel is `discord:<layout id>` or `dm:<guid>` is composed and typed
as any other; its line is stored here as a `discord_message` row and never goes to the game.

`is_discord` is the one test the pipeline keys on: anything with either prefix counts, a
malformed one too, so such a turn can only ever be stored here or refused (store_line), never
fall through to a Say."""
from dataclasses import dataclass

from . import layout
from . import store as dstore

CHANNEL_PREFIX = "discord:"
DM_PREFIX = "dm:"
LINES = 3  # a member's message holds up to this many typed lines, joined by "\n"


@dataclass(frozen=True, slots=True)
class DiscordTarget:
    """Where a member's answer goes in Discord: the message it replies to and the thread it is
    posted in (None: neither)."""
    reply_to: int | None
    thread: int | None


def is_discord(channel: str) -> bool:
    return channel.startswith((CHANNEL_PREFIX, DM_PREFIX))


def channel_id(channel: str) -> str:
    """The store's channel: the layout id of `discord:<id>`; a `dm:<guid>` as it is."""
    return channel.removeprefix(CHANNEL_PREFIX)


def store_line(store, turn, meta, text: str, now: float) -> int:
    """Store a member's line as her message (discord.store.post, origin "mind", in reply to and
    in the thread of meta.discord, with the turn's id) and return its id. A channel the layout
    does not have, or a DM that is not the speaker's own with him, is refused (DiscordRefused
    bad_channel, not_peer), as is anything the store refuses."""
    cid = channel_id(turn.channel)
    peer = layout.dm_peer(cid)
    if peer is None and layout.channel(cid) is None:
        raise dstore.DiscordRefused("bad_channel")
    if peer is not None and peer != turn.speaker:
        raise dstore.DiscordRefused("not_peer")
    target = meta.discord
    return dstore.post(store, channel=cid, author=turn.speaker, origin="mind", text=text,
                       reply_to=target.reply_to if target is not None else None,
                       thread=target.thread if target is not None else None,
                       turn=meta.turn_id, now=now)
