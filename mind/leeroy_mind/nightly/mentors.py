"""Weekly mentor pairings: a veteran, a newer player, and shared memories of helping."""
from leeroy_mind.memory.writer import clean_memory_text
from leeroy_mind.nightly.settings import iso_week
from leeroy_mind.nightly.store import step_finished_at
from leeroy_mind.nightly.storyteller import arc_key
from leeroy_mind.storage.rows import MemoryRow

STAGE = "mentors"
DAY = 86400.0


def is_veteran(p) -> bool:
    return (p.game.years_played >= 5 and p.play.skill in {"solid", "clutch"}
            and p.play.guild_role in {"officer", "raider"})


def is_mentee(p) -> bool:
    return p.play.skill == "sloppy" or p.game.years_played <= 3


def active_pair(ctx, a, b):
    """The active pairing in either direction, for scene weights and settings."""
    if a == b or ctx.human in {a, b}:
        return None
    now = ctx.clock.now()
    with ctx.store.transaction():
        return ctx.store.conn.execute("SELECT * FROM mentor WHERE ended IS NULL "
            "AND started<=? AND ends>? AND ((mentor=? AND mentee=?) OR (mentor=? AND mentee=?)) "
            "ORDER BY id LIMIT 1", (now, now, a, b, b, a)).fetchone()


def _memory(ctx, owner, other, text, ref, now):
    return MemoryRow(id=None, owner=owner, kind="reflection",
        text=clean_memory_text(text, ctx.ident), t_event=int(now), t_created=int(now),
        t_accessed=int(now), access_n=0, actors=(other,), place=None, boss_id=None,
        raid_id=None, importance=5, valence=0, source="reflection", evidence=(ref,))


async def stage_mentors(ctx):
    """Once per ISO week across all nights, expire pairs and fill the open places."""
    week = iso_week(ctx.night)
    if step_finished_at(ctx.store, STAGE, week) is not None:
        return

    def write(store):
        # The unit key is per night in night_step, so retain a cross-night guard in
        # the same transaction as the pairings, even for a concurrent retry.
        if step_finished_at(store, STAGE, week) is not None:
            return
        now = ctx.clock.now()
        expired = store.conn.execute("SELECT * FROM mentor WHERE ended IS NULL AND ends<=?",
                                      (now,)).fetchall()
        for pair in expired:
            store.conn.execute("UPDATE mentor SET ended=? WHERE id=?", (now, pair["id"]))
            store.conn.execute("UPDATE story_arc SET status='resolved',last_beat=? "
                "WHERE kind='mentor' AND key=?", (now, arc_key("mentor", pair["mentor"], pair["mentee"])))

        active = store.conn.execute("SELECT * FROM mentor WHERE ended IS NULL AND started<=? AND ends>?",
                                     (now, now)).fetchall()
        room = max(0, ctx.settings.mentors_max - len(active))
        if not room:
            return
        members = ctx.members()
        people = {guid: ctx.persona(guid) for guid in members}
        roster = store.cast()
        veterans = [guid for guid in members if is_veteran(people[guid])]
        # A future reservation is not active yet but still owns the unique mentee
        # slot. Do not collide with it when repairing/importing an existing ledger.
        assigned = {r[0] for r in store.conn.execute("SELECT mentee FROM mentor WHERE ended IS NULL")}
        rng = ctx.rng(STAGE, week)
        for mentee in members:
            if not room:
                break
            if mentee in assigned or not is_mentee(people[mentee]):
                continue
            same_class = [g for g in veterans if g != mentee
                          and roster[g].class_ == roster[mentee].class_]
            candidates = same_class or [g for g in veterans if g != mentee
                and people[g].play.raid_role == people[mentee].play.raid_role]
            if not candidates:
                continue
            mentor = min(candidates, key=lambda g: (-store.get_relationship(g, mentee).affinity, g))
            ident = store.conn.execute("INSERT INTO mentor(mentor,mentee,started,ends) VALUES (?,?,?,?)",
                (mentor, mentee, now, now + rng.uniform(28, 56) * DAY)).lastrowid
            store.conn.execute("INSERT INTO story_arc(kind,key,opened,last_beat) VALUES ('mentor',?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET status='open',tension=0,opened=excluded.opened,"
                "last_beat=excluded.last_beat", (arc_key("mentor", mentor, mentee), now, now))
            ref = f"mentor:{ident}"
            ctx.writer.put(_memory(ctx, mentor, mentee, f"Took {ctx.name(mentee)} under my wing.", ref, now))
            ctx.writer.put(_memory(ctx, mentee, mentor, f"{ctx.name(mentor)} is showing me the ropes.", ref, now))
            assigned.add(mentee)
            room -= 1

    ctx.commit(STAGE, week, write)
