"""The guild's Discord as the talk pipeline's third transport, beside the bridge's `say` and the
voice service. A turn whose channel is `discord:<layout id>` or `dm:<guid>` is composed and typed
as any other; its line is stored here as a `discord_message` row and never goes to the game.

`is_discord` (discord.channels) is the one test the pipeline keys on: anything with either
prefix counts, a malformed one too, so such a turn can only ever be stored here or refused
(store_line), never fall through to a Say."""
from dataclasses import dataclass

from . import layout
from . import store as dstore
from .channels import CHANNEL_PREFIX, DM_PREFIX, channel_id, is_discord

__all__ = ("CHANNEL_PREFIX", "DM_PREFIX", "LINES", "DiscordTarget", "channel_id", "is_discord",
           "store_line")

LINES = 3  # a member's message holds up to this many typed lines, joined by "\n"


@dataclass(frozen=True, slots=True)
class DiscordTarget:
    """Where a member's answer goes in Discord: the message it replies to and the thread it is
    posted in (None: neither)."""
    reply_to: int | None
    thread: int | None


def store_line(store, turn, meta, text: str, now: float, *, audience) -> int:
    """Store a member's line as her message (discord.store.post, origin "mind", in reply to and
    in the thread of meta.discord, with the turn's id) and return its id. `audience` is who
    may read the channel (WorldView.audience), and a speaker outside it is refused (ruling R8):
    in a DM anyone but its own member (not_peer), in an officers' channel a non-officer
    (officers_only), elsewhere anyone it lacks (not_in_audience). A channel the layout does not
    have is refused too (bad_channel), as is anything the store refuses (DiscordRefused)."""
    cid = channel_id(turn.channel)
    peer = layout.dm_peer(cid)
    if peer is not None:
        if peer != turn.speaker:
            raise dstore.DiscordRefused("not_peer")
    else:
        ch = layout.channel(cid)
        if ch is None:
            raise dstore.DiscordRefused("bad_channel")
        if turn.speaker not in audience:
            raise dstore.DiscordRefused("officers_only" if ch.officers_only
                                        else "not_in_audience")
    target = meta.discord
    return dstore.post(store, channel=cid, author=turn.speaker, origin="mind", text=text,
                       reply_to=target.reply_to if target is not None else None,
                       thread=target.thread if target is not None else None,
                       turn=meta.turn_id, now=now)
