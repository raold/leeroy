"""Which lines are private, which are restricted, and the guild Discord's channel strings
(ruling R7). The talk pipeline, memory and the night all ask here, never with string tests of
their own. Standard library only; the layout is read only when a Discord channel is asked
about, so storage and memory can import this without an import cycle.

- A private line is between two people: a WoW whisper or a Discord DM (`dm:<guid>`). Memory
  and the night treat a DM exactly as a whisper, and the two are one conversation: what was
  said in either reaches a private turn between the same two in either (one mind per member,
  2026-11-12).
- A restricted line is one only part of the guild reads: the officers' (officer chat and the
  officers' voice room in the game, an officers' Discord channel), or a `discord:` channel the
  layout does not have: only its audience knows it, nobody passes it on, and it never counts
  as public. The officers' lines in either medium are one audience (is_officer_channel).
- Every other line is public, as before."""
import re

CHANNEL_PREFIX = "discord:"
DM_PREFIX = "dm:"
GROUP_PREFIX = "gdm:"
WHISPER = "whisper"
# The officers' channels in the game: officer chat and their voice room
OFFICER_GAME = frozenset({"officer", "voice:officers"})
_COLUMN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")


def is_discord(channel) -> bool:
    return isinstance(channel, str) and channel.startswith((CHANNEL_PREFIX, DM_PREFIX, GROUP_PREFIX))


def is_group(channel) -> bool:
    return isinstance(channel, str) and channel.startswith(GROUP_PREFIX)


def same_medium(a, b) -> bool:
    """Two channels of one world: both the guild's Discord or both the game. A line in one never
    answers a line in the other, and Discord never counts as play in the game (ruling R11)."""
    return is_discord(a) == is_discord(b)


def channel_id(channel: str) -> str:
    """The Discord store's channel: the layout id of `discord:<id>`; a `dm:<guid>` as it is."""
    return channel.removeprefix(CHANNEL_PREFIX)


def is_private(channel) -> bool:
    """A line between two people: a whisper, or a Discord DM."""
    return isinstance(channel, str) and (channel == WHISPER or channel.startswith(DM_PREFIX))


def is_local_channel(channel) -> bool:
    """Game rooms whose historical readers must contain the current readers."""
    return isinstance(channel, str) and (channel in ('party', 'raid', 'raid_warning', 'say', 'yell')
                                         or channel.startswith('voice:'))


def is_restricted(channel) -> bool:
    """A line only its audience reads: the officers' (is_officer_channel), or one on a Discord
    channel the layout does not have (fail closed)."""
    if is_group(channel) or channel in OFFICER_GAME:
        return True
    if not isinstance(channel, str) or not channel.startswith(CHANNEL_PREFIX):
        return False
    from .layout import channel as layout_channel
    c = layout_channel(channel_id(channel))
    return c is None or c.officers_only


def is_officer_channel(channel) -> bool:
    """One of the officers' channels, in either medium: officer chat or the officers' voice room
    in the game, or an officers' Discord channel the layout has. A Discord channel the layout
    lacks is restricted but no officers' channel: it stays with itself."""
    if channel in OFFICER_GAME:
        return True
    if not isinstance(channel, str) or not channel.startswith(CHANNEL_PREFIX):
        return False
    from .layout import channel as layout_channel
    c = layout_channel(channel_id(channel))
    return c is not None and c.officers_only


def restricted_sql(column: str = "channel") -> str:
    """The rows that may be restricted (is_restricted still decides each): the game's officers'
    channels and every Discord channel, as an SQL condition on `column`, a fixed column name
    (as private_sql)."""
    column = _column(column)
    games = ", ".join(f"'{c}'" for c in sorted(OFFICER_GAME))
    return (f"({column} IN ({games}) "
            f"OR substr({column}, 1, {len(CHANNEL_PREFIX)}) = '{CHANNEL_PREFIX}' OR {group_sql(column)})")


def is_public(channel) -> bool:
    """Neither private nor restricted: what the whole guild may read."""
    return not is_private(channel) and not is_restricted(channel)


def _column(column: str) -> str:
    if not _COLUMN.fullmatch(column):
        raise ValueError(f"not a column name: {column!r}")
    return column


def private_sql(column: str = "channel") -> str:
    """is_private as an SQL condition on `column`, a fixed column name (never input): anything
    else is refused (ValueError)."""
    column = _column(column)
    return f"({column} = '{WHISPER}' OR substr({column}, 1, {len(DM_PREFIX)}) = '{DM_PREFIX}')"


def discord_sql(column: str = "channel") -> str:
    """is_discord as an SQL condition on `column`, a fixed column name, as private_sql."""
    column = _column(column)
    return (f"(substr({column}, 1, {len(CHANNEL_PREFIX)}) = '{CHANNEL_PREFIX}' "
            f"OR substr({column}, 1, {len(DM_PREFIX)}) = '{DM_PREFIX}' OR {group_sql(column)})")


def group_sql(column: str = "channel") -> str:
    return f"(substr({_column(column)}, 1, {len(GROUP_PREFIX)}) = '{GROUP_PREFIX}')"
