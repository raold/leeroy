"""Who calls his group's raid when he names nobody: a code-built rule, never the model.

The default lead is the officer in his group with the most raid nights led (guild_raid rows the
mind recorded with her as lead, offline raids aside), then the most raid nights attended
(guild_attendance 'raided'), then an officer whose title says raid lead, then the lowest guid.
A group with no officer in it has no default lead.
"""

from collections.abc import Iterable


def nights(conn, guids: Iterable[int]) -> dict[int, tuple[int, int]]:
    """(nights led, nights attended) for each guid, from mind.db's guild tables."""
    guids = [g for g in guids if type(g) is int]
    out = {g: (0, 0) for g in guids}
    if not guids:
        return out
    marks = ",".join("?" * len(guids))
    led = dict(conn.execute(f"SELECT lead_guid,COUNT(*) FROM guild_raid WHERE offline=0 "
                            f"AND lead_guid IN ({marks}) GROUP BY lead_guid", guids))
    attended = dict(conn.execute(f"SELECT guid,COUNT(DISTINCT raid_id) FROM guild_attendance "
                                 f"WHERE status='raided' AND guid IN ({marks}) GROUP BY guid", guids))
    return {g: (int(led.get(g, 0)), int(attended.get(g, 0))) for g in guids}


def default_lead(conn, officers: Iterable[int], titled: Iterable[int] = ()) -> int | None:
    """The default lead among `officers` (his group's officers, online, with a persona);
    `titled` are those whose officer title says raid lead (only a tie-break)."""
    officers = sorted(set(officers))
    if not officers:
        return None
    titled = set(titled)
    counts = nights(conn, officers)
    return max(officers, key=lambda g: (*counts[g], g in titled, -g))
