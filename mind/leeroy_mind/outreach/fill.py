"""Six deterministic nightly sources for a member's shared list."""
from datetime import datetime, time, timedelta
from types import SimpleNamespace

from leeroy_mind.talk.romance import forward, romance_lift

from . import store as items
from .sources import (BIRTHDAY_ABOUT, DAY, MIN_AGE, MISS_YOU_ABOUT, about, his_day,
                      member_local, quiet_pair, source_alive)


def fill_member(store, guid, *, now, local, human, persona, romance_dial=5, ident=None, library=None):
    """No model, random draw, hot configuration read, or game activity. Caller owns the unit."""
    if persona.guid != guid or guid == human:
        return 0
    added = 0

    def put(kind, ref, text, audience, weight, expires):
        nonlocal added
        draft = SimpleNamespace(guid=guid, kind=kind, ref=ref, about=about(text), audience=audience)
        if expires <= now or not source_alive(store, draft, now=now, human=human, local=local,
                                               persona=persona, ident=ident, library=library):
            return
        result = items.add(store, guid=guid, kind=kind, ref=ref, about=draft.about,
                           audience=audience, weight=weight, created=now, not_before=now, expires=expires)
        added += result is not None

    with store.transaction():
        items.expire(store, now)
        for item in items.open_items(store, guid):
            if not source_alive(store, item, now=now, human=human, local=local, persona=persona,
                                ident=ident, library=library):
                items.drop(store, item.id, "superseded")
        for row in store.conn.execute("SELECT b.id,b.text FROM life_beat b JOIN life_thread t "
            "ON t.id=b.thread_id WHERE t.guid=? AND b.ts>=? AND b.ts<=? ORDER BY b.ts DESC,b.id DESC",
            (guid, now - DAY, now)):
            put("life", f"life:{row[0]}", row[1], "pair", 3, now + 2 * DAY)
        for row in store.conn.execute("SELECT id,text,expires FROM member_claim WHERE guid=? AND kind='plan' "
            "AND superseded_by IS NULL AND last_seen<=? AND (expires IS NULL OR expires>?) ORDER BY id",
            (guid, now, now)):
            put("plan", f"plan:{row[0]}", row[1], "pair", 2, min(row[2] or now + 7 * DAY, now + 7 * DAY))
        for row in store.conn.execute("SELECT key,text FROM memory WHERE owner=? AND status='active' "
            "AND superseded_by IS NULL AND substr(key,1,9)='followup:' AND t_created<=? ORDER BY t_created DESC,id DESC",
            (guid, now)):
            put("followup", row[0], row[1], "pair", 4, now + 3 * DAY)
        for row in store.conn.execute("SELECT id FROM game_event WHERE type IN ('boss','loot_won') "
            "AND ts>=? AND ts<=? ORDER BY ts DESC,id DESC", (now - DAY, now)):
            if text := his_day(store, row[0], guid, human, library=library, ident=ident):
                put("his_day", f"his_day:{row[0]}", text, "guild", 2, now + DAY)
        at = member_local(local, persona)
        if persona.real_life.birthday == at.strftime("%m-%d"):
            midnight = datetime.combine(at.date() + timedelta(days=1), time.min, tzinfo=at.tzinfo)
            put("birthday", f"birthday:{at.date()}", BIRTHDAY_ABOUT, "guild", 5, midnight.timestamp())
        if (persona.identity.age >= MIN_AGE and persona.identity.player_gender == "female"
                and persona.romance.stance in ("drawn", "lust") and forward(persona) > 0
                and quiet_pair(store, guid, human, now=now)):
            dial = persona.romance.dial if persona.romance.dial is not None else romance_dial
            weight = min(10, max(.1, romance_lift(dial) * forward(persona)))
            put("miss_you", f"miss_you:{at.date()}", MISS_YOU_ABOUT, "pair", weight, now + DAY)
    return added
