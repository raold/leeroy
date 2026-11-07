"""Which lines are private, which are restricted, and the guild Discord's channel strings
(ruling R7). The talk pipeline, memory and the night all ask here, never with string tests of
their own. Standard library only; the layout is read only when a Discord channel is asked
about, so storage and memory can import this without an import cycle.

- A private line is between two people: a WoW whisper or a Discord DM (`dm:<guid>`). Memory
  and the night treat a DM exactly as a whisper.
- A restricted line is on a Discord channel only part of the guild reads (an officers'
  channel), or on a `discord:` channel the layout does not have: only its audience knows it,
  nobody passes it on, and it never counts as public.
- Every other line is public, as before."""
CHANNEL_PREFIX = "discord:"
DM_PREFIX = "dm:"
WHISPER = "whisper"


def is_discord(channel) -> bool:
    return isinstance(channel, str) and channel.startswith((CHANNEL_PREFIX, DM_PREFIX))


def channel_id(channel: str) -> str:
    """The Discord store's channel: the layout id of `discord:<id>`; a `dm:<guid>` as it is."""
    return channel.removeprefix(CHANNEL_PREFIX)


def is_private(channel) -> bool:
    """A line between two people: a whisper, or a Discord DM."""
    return isinstance(channel, str) and (channel == WHISPER or channel.startswith(DM_PREFIX))


def is_restricted(channel) -> bool:
    """A line on a Discord channel only its audience reads: an officers' channel, or one the
    layout does not have (fail closed)."""
    if not isinstance(channel, str) or not channel.startswith(CHANNEL_PREFIX):
        return False
    from .layout import channel as layout_channel
    c = layout_channel(channel_id(channel))
    return c is None or c.officers_only


def is_public(channel) -> bool:
    """Neither private nor restricted: what the whole guild may read."""
    return not is_private(channel) and not is_restricted(channel)


def private_sql(column: str = "channel") -> str:
    """is_private as an SQL condition on `column` (a fixed column name, never input)."""
    return f"({column} = '{WHISPER}' OR substr({column}, 1, {len(DM_PREFIX)}) = '{DM_PREFIX}')"
