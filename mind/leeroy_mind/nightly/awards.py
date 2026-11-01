"""Six approved season awards, counted from events rather than invented by a model."""
from collections import Counter
from datetime import datetime
import json

from leeroy_mind.nightly.reactions import LAUGH_RE, REPLY_S, WHISPER_BACK_S, first_after, reactions
from leeroy_mind.nightly.settings import is_season_end, season_bounds, season_of
from leeroy_mind.nightly.store import step_finished_at
from leeroy_mind.personas.screens import has_word
from leeroy_mind.storage.rows import MemoryRow
from leeroy_mind.wow import RAID_MAPS, is_raid_fight

STAGE = "stories"
# Insertion order fixes the award:{season}:{n} key even when a category has no winner.
AWARDS = {
    "Most Likely to Stand in Fire": "deaths",
    "Most Whispered": "whispers",
    "Loot Goblin": "loot",
    "Class Clown": "laughs",
    "Night Owl": "late_lines",
    "Never Missed a Pull": "pulls",
}


def _payload(text):
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _event_guid(row, payload, eligible):
    guid = payload.get("guid", row["guid"])
    if type(guid) is not int or guid not in eligible:
        return None
    return guid if row["guid"] is None or row["guid"] == guid else None


def _laughed_before_end(store, reaction, end, human, names):
    """Keep Task12's attribution, while a post-season answer cannot change this season."""
    if reaction.ts + WHISPER_BACK_S < end:
        return True
    row = store.conn.execute("SELECT * FROM chat_line WHERE id=?", (reaction.line_id,)).fetchone()
    for answer in store.conn.execute(
            "SELECT * FROM chat_line WHERE origin='human' AND from_guid=? AND ts<? "
            "AND (ts>? OR (ts=? AND id>?)) AND ts<=? ORDER BY ts,id",
            (human, end, row["ts"], row["ts"], row["id"], row["ts"] + WHISPER_BACK_S)):
        if not LAUGH_RE.search(answer["text"]):
            continue
        whisper = answer["channel"] == "whisper" and answer["to_guid"] == reaction.speaker
        same = (answer["channel"] == row["channel"] and answer["channel"] != "whisper"
                and answer["ts"] <= row["ts"] + REPLY_S)
        if whisper or (same and (has_word(answer["text"], names[reaction.speaker]) or (
                not any(has_word(answer["text"], name) for guid, name in names.items()
                        if guid not in {reaction.speaker, human}) and first_after(store, row, answer)))):
            return True
    return False


def _counts(ctx, bounds, eligible):
    store = ctx.store
    counts = {key: Counter() for key in AWARDS.values()}
    participants = set()
    for row in store.conn.execute("SELECT type,guid,payload FROM game_event WHERE ts>=? AND ts<? "
                                  "AND type IN ('death','loot_won','boss') ORDER BY ts,id", bounds):
        payload = _payload(row["payload"])
        guid = _event_guid(row, payload, eligible)
        if (row["type"] == "death" and guid is not None and type(payload.get("map")) is int
                and payload["map"] in RAID_MAPS):
            counts["deaths"][guid] += 1
        elif row["type"] == "loot_won" and guid is not None:
            counts["loot"][guid] += 1
        elif row["type"] == "boss":
            map_id, boss_id = payload.get("map"), payload.get("boss_id")
            members = payload.get("members")
            if (type(map_id) is not int or type(boss_id) is not int or not isinstance(members, list)
                    or not is_raid_fight(map_id, boss_id)):
                continue
            present = {guid for guid in members if type(guid) is int and guid in eligible}
            participants.update(present)
            counts["pulls"].update(present)
    for row in store.conn.execute("SELECT to_guid,count(*) AS n FROM chat_line WHERE ts>=? AND ts<? "
            "AND channel='whisper' AND from_guid=? GROUP BY to_guid", (*bounds, ctx.human)):
        if row["to_guid"] in eligible:
            counts["whispers"][row["to_guid"]] += row["n"]
    for row in store.conn.execute("SELECT guid,ts FROM utterance WHERE ts>=? AND ts<? AND dropped IS NULL", bounds):
        if row["guid"] in eligible and 1 <= datetime.fromtimestamp(row["ts"], ctx.tz).hour < 6:
            counts["late_lines"][row["guid"]] += 1
    names = {guid: ctx.name(guid) for guid in eligible | {ctx.human}}
    for reaction in reactions(store, bounds, ctx.human, names):
        if (reaction.speaker in eligible and reaction.reaction == "laugh"
                and _laughed_before_end(store, reaction, bounds[1], ctx.human, names)):
            counts["laughs"][reaction.speaker] += 1
    return counts, participants


def _memory(ctx, owner, winner, title, key, ident, now):
    first = owner == winner
    text = f"Won '{title}' this season." if first else f"{ctx.name(winner)} won '{title}' this season."
    ctx.writer.put(MemoryRow(id=None, owner=owner, kind="event", text=text,
        t_event=int(now), t_created=int(now), t_accessed=int(now), access_n=0,
        actors=() if first else (winner,), place=None, boss_id=None, raid_id=None,
        importance=6 if first else 4, valence=0.3, source="game",
        evidence=(f"award:{ident}",), key=key))


async def stage_awards(ctx):
    if not is_season_end(ctx.night, ctx.settings.season_weeks):
        return
    season = season_of(ctx.night, ctx.settings.season_weeks)
    unit = f"awards:{season}"
    if ctx.done(STAGE, unit) or step_finished_at(ctx.store, STAGE, unit) is not None:
        return
    bounds = season_bounds(season, ctx.settings.season_weeks, ctx.tz)

    def write(store):
        eligible = set(ctx.members(include_away=True)) - {0, ctx.human}
        counts, participants = _counts(ctx, bounds, eligible)
        now = ctx.clock.now()
        for n, (title, counter) in enumerate(AWARDS.items()):
            earned = [(value, guid) for guid, value in counts[counter].items() if value > 0]
            if not earned:
                continue
            value, winner = min(earned, key=lambda pair: (-pair[0], pair[1]))
            store.conn.execute("INSERT INTO award(season,title,guid,value) VALUES (?,?,?,?) "
                "ON CONFLICT(season,title) DO NOTHING", (season, title, winner, value))
            award = store.conn.execute("SELECT * FROM award WHERE season=? AND title=?", (season, title)).fetchone()
            if award["guid"] != winner or award["value"] != value:
                raise ValueError("Existing season award disagrees with its source counts")
            key = f"award:{season}:{n}"
            _memory(ctx, winner, winner, title, key, award["id"], now)
            for owner in sorted(participants - {winner}):
                _memory(ctx, owner, winner, title, key, award["id"], now)
    ctx.commit(STAGE, unit, write)
