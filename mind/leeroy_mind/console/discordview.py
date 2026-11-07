"""The Discord tab's read-only question to mind.db: the guild's server (categories, channels, DMs),
its members with their Discord presence, and one channel's messages with reactions, threads and
pins. It is his view, so every channel is listed, the officers' ones with `officers_only` as a
display flag. Presence is computed as the mind computes it (discord.presence with the same seed,
the member's regional clock and the configured trial rank). A table an older mind.db lacks is
named in `missing` and reads as empty."""
import json
from datetime import datetime

from leeroy_mind.discord import layout, presence, store
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.protocol import Member

from . import views
from .feed import human_guid

DISCORD_TABLES = ("discord_message", "discord_thread", "discord_reaction")
LIMIT = 100


def _personas(conn) -> dict:
    out = {}
    for r in views.rows(conn, "SELECT guid, json FROM persona_core"):
        try:
            out[r["guid"]] = persona_from_dict(json.loads(r["json"]))
        except Exception:  # a stored persona the code cannot read is a member without one
            continue
    return out


def _members(conn, owner_now: datetime, trial_rank: int) -> list[dict]:
    personas = _personas(conn)
    out = []
    for r in views.rows(conn, "SELECT guid, name, race, class, gender, level, rank, online, map, "
                              "zone, spec_tab FROM cast_member ORDER BY name, guid"):
        p = personas.get(r["guid"])
        member = Member(guid=r["guid"], name=r["name"], race=r["race"], class_=r["class"],
                        gender=r["gender"], level=r["level"], rank=r["rank"],
                        online=bool(r["online"]), map=r["map"], zone=r["zone"],
                        spec_tab=r["spec_tab"])
        if p is None:
            state = presence.Presence("offline", False, r["zone"])
        else:
            state = presence.presence_of(
                p, local=presence.local_time(owner_now, p.identity.region),
                body_online=bool(r["online"]), body_zone=r["zone"], rng_seed=presence.RNG_SEED)
        try:
            ckey = layout.class_key(r["class"])
        except KeyError:
            ckey = ""
        out.append({"guid": r["guid"], "name": r["name"], "class_key": ckey,
                    "role": layout.role_of(member, p, trial_rank=trial_rank),
                    "persona": p is not None,
                    "presence": {"state": state.state, "playing": state.playing,
                                 "zone": state.zone}})
    return out


def with_reactions(conn, rows: list[dict]) -> list[dict]:
    react = store.reactions(conn, [m["id"] for m in rows])
    return [{**m, "reactions": react.get(m["id"], [])} for m in rows]


def window(conn, channel: str) -> list[dict]:
    """The latest messages of a channel with their reactions: what a page has loaded."""
    return with_reactions(conn, store.messages(conn, channel, limit=LIMIT))


def fingerprint(messages: list[dict]) -> str:
    """A loaded window as one comparable text: any edit, delete, pin or reaction changes it."""
    return json.dumps(messages, sort_keys=True, ensure_ascii=True)


def last_by_channel(conn) -> dict[str, int]:
    return {r[0]: r[1] for r in views.rows(
        conn, "SELECT channel, max(id) FROM discord_message GROUP BY channel")}


def discord_view(conn, *, owner_now: datetime, trial_rank: int, channel: str | None = None,
                 after: int = 0, thread: int | None = None) -> dict:
    have = views.tables(conn)
    missing = [t for t in DISCORD_TABLES if t not in have]
    last = last_by_channel(conn) if "discord_message" in have else {}
    names = views.names(conn)
    human = human_guid(conn)
    hname = views.rows(conn, "SELECT value FROM meta WHERE key = 'human_name'")
    members = _members(conn, owner_now, trial_rank)
    out = {
        "categories": [
            {"key": k, "name": layout.CATEGORY_NAMES[k],
             "channels": [{"id": c.id, "name": c.name, "topic": c.topic,
                           "officers_only": c.officers_only, "last_id": last.get(c.id, 0)}
                          for c in layout.CHANNELS if c.category == k]}
            for k in layout.CATEGORIES],
        "dms": sorted(({"guid": m["guid"], "name": m["name"],
                        "last_id": last.get(f"dm:{m['guid']}", 0)}
                       for m in members if m["persona"]),
                      key=lambda d: (-d["last_id"], d["name"], d["guid"])),
        "members": [{k: v for k, v in m.items() if k != "persona"} for m in members],
        "human": {"guid": human, "name": (hname[0][0] if hname and isinstance(hname[0][0], str)
                                          else names.get(human, ""))},
        "missing": missing,
    }
    if channel is not None:
        known = layout.channel(channel) is not None or layout.dm_peer(channel) is not None
        ok = known and not missing
        out["channel"] = channel
        out["messages"] = with_reactions(conn, store.messages(
            conn, channel, after=after, limit=LIMIT, thread=thread)) if ok else []
        out["threads"] = store.threads(conn, channel) if ok else []
        out["pins"] = store.pins(conn, channel) if ok else []
    return out
