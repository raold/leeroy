"""The Discord tab's read-only question to mind.db: the guild's server (categories, channels, DMs),
its members with their Discord presence, and one channel's messages with reactions, threads and
pins. It is his view, so every channel is listed, the officers' ones with `officers_only` as a
display flag. Its members are the mind's Discord audience: the cast at CAST_MIN_LEVEL or more,
never him. Presence is computed as the mind computes it (discord.presence with the same seed,
the member's regional clock and the configured trial rank). A table an older mind.db lacks is
named in `missing` and reads as empty."""
import json
from hashlib import sha256
from datetime import datetime

from leeroy_mind.discord import layout, presence, store
from leeroy_mind.personas.schema import persona_from_dict
from leeroy_mind.personas.tiers import is_cast
from leeroy_mind.protocol import Member
from leeroy_mind.wow import CAST_MIN_LEVEL

from . import views
from .feed import human_guid

DISCORD_TABLES = ("discord_message", "discord_thread", "discord_reaction")
LIMIT = 100
WINDOW_BYTES = 180_000


def _encoded(value) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _bounded_window(out: dict, *, after: int) -> dict:
    """Retain whole records under the wire budget, saying which shelves were limited.

    The roster/layout is measured separately. Each shelf has a share so large pins
    cannot evict every current message, and quotes only accompany retained replies.
    """
    rows = {key: out[key] for key in ("messages", "quotes", "pins", "threads")}
    out.update({key: [] for key in rows})
    out["limited"] = {key: False for key in rows}
    remaining = max(0, WINDOW_BYTES - len(_encoded(out)) - 128)
    for key, share in (("messages", .6), ("quotes", .15), ("pins", .15), ("threads", .1)):
        candidates = rows[key]
        if key == "quotes":
            parents = {m["reply_to"] for m in out["messages"] if m["reply_to"]}
            candidates = [q for q in candidates if q["id"] in parents]
        newest = key == "messages" and not after
        ordered = reversed(candidates) if newest else iter(candidates)
        kept, used, budget = [], 0, int(remaining * share)
        for row in ordered:
            size = len(_encoded(row)) + 1
            if len(kept) >= LIMIT or used + size > budget:
                break
            kept.append(row); used += size
        out[key] = list(reversed(kept)) if newest else kept
        out["limited"][key] = len(kept) < len(candidates)
    return out


def _personas(conn) -> dict:
    out = {}
    for r in views.rows(conn, "SELECT guid, json FROM persona_core"):
        if not is_cast(conn,r["guid"]):
            continue
        try:
            out[r["guid"]] = persona_from_dict(json.loads(r["json"]))
        except Exception:  # a stored persona the code cannot read is a member without one
            continue
    return out


def _members(conn, owner_now: datetime, trial_rank: int, human: int, *, server=False) -> list[dict]:
    """The cast as the mind's Discord audience has it (WorldView.audience): level
    CAST_MIN_LEVEL or more, and not him (the stored roster keeps his own row)."""
    personas = _personas(conn)
    from leeroy_mind.server.state import read_play
    playing = read_play(conn, owner_now.timestamp()) if server else frozenset()
    out = []
    for r in views.rows(conn, "SELECT guid, name, race, class, gender, level, rank, online, map, "
                              "zone, spec_tab FROM cast_member WHERE guid != ? AND level >= ? "
                              "ORDER BY name, guid", (human, CAST_MIN_LEVEL)):
        if not is_cast(conn,r["guid"]):
            continue
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
                body_online=bool(r["online"]), body_zone=r["zone"], rng_seed=presence.RNG_SEED,
                in_his_group=r['guid'] in playing)
        try:
            ckey = layout.class_key(r["class"])
        except KeyError:
            ckey = ""
        out.append({"guid": r["guid"], "name": r["name"], "class_key": ckey,
                    "role": layout.role_of(member, p, trial_rank=trial_rank),
                    "persona": p is not None,
                    "presence": {"state": state.state, "playing": state.playing,
                                 "zone": state.zone}})
        if server:
            out[-1]['status'] = presence.status_text(p,
                presence.local_time(owner_now, p.identity.region), state,
                rng_seed=presence.RNG_SEED) if p is not None else None
    return out


def with_reactions(conn, rows: list[dict]) -> list[dict]:
    react = store.reactions(conn, [m["id"] for m in rows])
    photos = (store.attachments(conn, [m['id'] for m in rows])
              if 'discord_attachment' in views.tables(conn) else {})
    return [{**m, "reactions": react.get(m["id"], []),
             'image': ({'w': photos[m['id']]['width'], 'h': photos[m['id']]['height'],
                        'kind': photos[m['id']]['kind']} if m['id'] in photos else None)} for m in rows]


def window(conn, channel: str, thread: int | None = None) -> list[dict]:
    """The latest messages of a channel with their reactions: what a page has loaded."""
    return with_reactions(conn, store.messages(conn, channel, limit=LIMIT, thread=thread))


def watched(conn, channel: str, thread: int | None = None) -> str:
    """A loaded feed plus its pin/thread shelves; removals have no timestamp to hint at."""
    return fingerprint({"messages": window(conn, channel, thread),
                        "pins": store.pins(conn, channel, limit=LIMIT + 1),
                        "threads": store.threads(conn, channel, limit=LIMIT + 1)})


def fingerprint(messages) -> str:
    """A fixed-size digest: edits and removals still invalidate the loaded window."""
    return sha256(json.dumps(messages, sort_keys=True, ensure_ascii=True).encode("ascii")).hexdigest()


def last_by_channel(conn) -> dict[str, int]:
    return {r[0]: r[1] for r in views.rows(
        conn, "SELECT channel, max(id) FROM discord_message GROUP BY channel")}


def discord_view(conn, *, owner_now: datetime, trial_rank: int, channel: str | None = None,
                 after: int = 0, thread: int | None = None, read_marks: dict | None = None,
                 server: bool = False) -> dict:
    have = views.tables(conn)
    missing = [t for t in DISCORD_TABLES if t not in have]
    last = last_by_channel(conn) if "discord_message" in have else {}
    unread = {r[0]: r[1] for r in views.rows(conn,
        "SELECT m.channel, count(*) FROM discord_message m JOIN json_each(?) r ON m.channel = r.key "
        "WHERE m.id > r.value AND m.origin = 'mind' AND m.deleted IS NULL AND m.thread IS NULL "
        "GROUP BY m.channel", (json.dumps(read_marks),))} if read_marks and not missing else {}
    names = views.names(conn)
    human = human_guid(conn)
    hname = views.rows(conn, "SELECT value FROM meta WHERE key = 'human_name'")
    members = _members(conn, owner_now, trial_rank, human, server=server)
    out = {
        "categories": [
            {"key": k, "name": layout.CATEGORY_NAMES[k],
             "channels": [{"id": c.id, "name": c.name, "topic": c.topic,
                           "officers_only": c.officers_only, "last_id": last.get(c.id, 0),
                           "unread": unread.get(c.id, 0)}
                          for c in layout.CHANNELS if c.category == k]}
            for k in layout.CATEGORIES],
        "dms": sorted(({"guid": m["guid"], "name": m["name"],
                        "last_id": last.get(f"dm:{m['guid']}", 0),
                        "unread": unread.get(f"dm:{m['guid']}", 0)}
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
        out["threads"] = store.threads(conn, channel, limit=LIMIT + 1) if ok else []
        out["pins"] = store.pins(conn, channel, limit=LIMIT + 1) if ok else []
        parents = sorted({m["reply_to"] for m in out["messages"] if m["reply_to"]})
        out["quotes"] = [{"id": r[0], "author": r[1], "text": "" if r[3] is not None else r[2],
                          "deleted": r[3]} for r in views.rows(conn,
            "SELECT id, author, text, deleted FROM discord_message WHERE channel = ? "
            "AND id IN (SELECT value FROM json_each(?))", (channel, json.dumps(parents)))] if parents else []
        return _bounded_window(out, after=after)
    return out
