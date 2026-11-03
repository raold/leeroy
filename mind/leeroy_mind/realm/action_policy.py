"""Exact outgoing Realm registry and native field validation.

Bounds mirror Lib/LeeroyRealmCommands.cpp; native roster-bound arena
admission is deliberately deferred until Task24 can prove the atomic full roster.
Payloads are immutable canonical business JSON, retaining source array order.
"""
from dataclasses import dataclass, fields
import json
import re

from leeroy_mind.guild.protocol import BotDo
from . import protocol as p

U32 = 0xFFFFFFFF
U64 = 0xFFFFFFFFFFFFFFFF


def integer(value, lo=1, hi=U32):
    if type(value) is not int or not lo <= value <= hi:
        raise ValueError("invalid action integer")
    return value


def text(value, lo=0, hi=256):
    if type(value) is not str or "\0" in value:
        raise ValueError("invalid action text")
    try:
        valid = lo <= len(value.encode("utf-8")) <= hi
    except UnicodeError:
        valid = False
    if not valid:
        raise ValueError("invalid action text")
    return value


def identity(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise ValueError("invalid action identity")
    return value


def members(value, lo=1, hi=150):
    if type(value) not in (tuple, list) or not lo <= len(value) <= hi:
        raise ValueError("invalid action membership")
    result = tuple(integer(g) for g in value)
    if len(set(result)) != len(result):
        raise ValueError("duplicate action membership")
    return result


def token(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{32}-[0-9a-f]{16}", value) is None:
        raise ValueError("invalid action token")
    return value


def document(value, *, limit=65536):
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("action document too large")
    return encoded


def _wire(value):
    if hasattr(value, "__dataclass_fields__"):
        return {f.name.rstrip("_"): _wire(getattr(value, f.name)) for f in fields(value)
                if not (f.metadata.get("omit_none") and getattr(value, f.name) is None)}
    if type(value) in (tuple, list):
        return [_wire(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class Classified:
    kind: str
    policy: str
    payload: str
    reserved_manifest: tuple[int, ...]


def classify(cmd, *, child=False):
    kind = next((k for k, cls in p.COMMANDS.items() if type(cmd) is cls), None)
    if type(cmd) is BotDo:
        kind = "bot_do"
    if kind is None:
        raise ValueError("unsupported action type")
    identity(cmd.id)
    if kind == "arena_queue" or kind == "arena_team" and cmd.action != "create":
        raise ValueError("arena requires Task24 atomic full-roster admission")
    scope, policy = (), "ack"
    def n(field, lo=1, hi=U32): return integer(getattr(cmd, field), lo, hi)
    def s(field, lo=0, hi=256): return text(getattr(cmd, field), lo, hi)
    def e(field, choices):
        value = s(field, 1, 64)
        if value not in choices:
            raise ValueError("invalid action choice")
        return value
    singleton = {"realm_move", "rpg_status", "craft", "equip", "set_profession", "transmog_set",
                 "calendar_signup", "bank_deposit", "bank_withdraw"}
    if kind in singleton:
        scope, policy = (n("guid"),), "ordinary"
    if kind == "realm_actors":
        gs = members(cmd.guids, 0, 16)
        if not set(members(cmd.gankers, 0, 16)) <= set(gs):
            raise ValueError("gankers outside actors")
        members(cmd.guilds, 0, 3)
    elif kind == "realm_say":
        n("guid"); channel = e("channel", {"trade", "general", "lfg", "recruitment", "say", "yell", "whisper"})
        s("text", 1, 255); n("to_guid", 1 if channel == "whisper" else 0)
        if channel != "whisper" and cmd.to_guid != 0: raise ValueError("invalid speech target")
    elif kind == "realm_emote":
        n("guid"); s("emote", 1, 64); n("target_guid", 0)
    elif kind == "realm_move":
        if re.fullmatch(r"[a-z0-9_]{1,64}", s("place", 1, 64)) is None: raise ValueError("invalid place")
        n("slot", 0, 119)
    elif kind == "rpg_status":
        e("status", {"idle", "rest", "wander npc", "wander random", "go grind", "go camp", "do quest", "do gather", "travel flight"})
        n("quest_id", 0)
        if cmd.quest_id and cmd.status != "do quest": raise ValueError("invalid quest status")
    elif kind in {"leave_pvp", "rearm", "wg_join", "guild_grow", "bot_do"}:
        maximum = 40 if kind in {"wg_join", "bot_do"} else 30 if kind == "guild_grow" else 150
        scope, policy = members(cmd.guids, 1, maximum), "wg" if kind == "wg_join" else "defaults" if kind == "bot_do" else "ordinary"
        if kind == "leave_pvp": s("pvp_owner", 0, 49)
        if kind == "leave_pvp" and cmd.pvp_owner:
            token(cmd.pvp_owner)
            if not child: raise ValueError("token cleanup requires request_cleanup")
            policy = "cleanup"
        if kind == "guild_grow": n("guild_id")
        if kind == "bot_do":
            s("action", 1, 64); s("param", 1, 128); s("training_run", 0, 128)
            if cmd.action != "nc" or cmd.param != "-bg,-lfg" or type(cmd.target_guid) is not int or cmd.target_guid != 0 or cmd.training_run != "":
                raise ValueError("unsupported BotDo shape")
    elif kind == "arena_team":
        e("action", {"create"})
        # Recheck dataclasses without relying on their construction-time validation.
        p.ArenaTeam(**{f.name: getattr(cmd, f.name) for f in fields(cmd)})
        scope, policy = (n("captain_guid"),), "ordinary"
    elif kind == "duel":
        scope, policy = members((n("guid"), n("target_guid")), 2, 2), "ordinary"
    elif kind == "craft": n("spell_id"); n("count", 1, 5)
    elif kind == "item_count":
        n("guid"); members(cmd.item_ids, 1, 20); policy = "result"
    elif kind == "equip": n("item_id", 5976, 5976)
    elif kind == "set_profession":
        if n("skill_id") not in {164, 165, 171, 182, 186, 197, 202, 333, 393, 755, 773, 185, 356, 129}: raise ValueError("invalid profession")
    elif kind == "transmog_set": n("slot", 0, 18); n("entry", 0)
    elif kind == "calendar_post":
        scope, policy = (n("creator_guid"),), "result"
        s("title", 1, 64); s("description", 0, 255); e("event_type", {"raid", "dungeon", "pvp", "meeting", "other"})
        n("at", 1, 0x7FFFFFFFFFFFFFFF); n("dungeon_id", -1, 0x7FFFFFFF)
        if type(cmd.guild_event) is not bool or type(cmd.invite_human) is not bool: raise ValueError("invalid calendar boolean")
    elif kind == "calendar_signup": n("event_id", 1, U64); e("status", {"signed_up", "tentative", "declined", "accepted"})
    elif kind == "calendar_update":
        n("event_id", 1, U64); s("title", 0, 64); s("description", 0, 255); n("at", 0, 0x7FFFFFFFFFFFFFFF)
    elif kind == "mail_send":
        scope, policy = (n("from_guid"),), "ordinary"
        s("subject", 1, 64); s("body", 0, 512); n("money", 0, 1000000); n("delay_s", 0, 3600)
        if type(cmd.items) not in (tuple, list) or len(cmd.items) > 4: raise ValueError("invalid mail items")
        for item in cmd.items:
            if type(item) is not p.MailItem: raise ValueError("invalid mail item")
            integer(item.item_id); integer(item.count, 1, 20)
            if text(item.from_, 1, 64) not in {"new", "bags"}: raise ValueError("invalid mail source")
    elif kind == "bank_tab_buy":
        scope = (n("officer_guid"),); n("tab", 0, 5); s("name", 1, 16); s("icon", 0, 32)
    elif kind == "bank_money": n("amount", 1, 60000)
    elif kind in {"bank_deposit", "bank_withdraw", "bank_stock"}:
        if kind == "bank_stock": scope, policy = (n("officer_guid"),), "ordinary"
        n("item_id"); n("count", 0 if kind == "bank_deposit" else 1, 1000); n("tab", 0, 5)
    elif kind == "grant_achievement":
        scope = (n("guid"),)
        if n("achievement_id") not in {3117, 4078, 4576, 2186, 3007, 1723, 1786}: raise ValueError("invalid achievement")
    elif kind == "guild_rename": n("guild_id"); s("name", 1, 24)
    elif kind == "guild_admit": scope = (n("guid"),); n("rank", 2, 9)
    elif kind == "guild_note": scope = (n("guid"),); s("officer_note", 0, 31)
    elif kind == "announce":
        # Frozen dataclasses can still be mutated through object.__setattr__.
        # Recheck every exact proof field at the journaling boundary.
        p.Announce(**{f.name: getattr(cmd, f.name) for f in fields(cmd)})
    elif kind == "get_realm_pvp":
        if not child: raise ValueError("query requires journaled origin")
        p.GetRealmPvp(cmd.id, cmd.pvp_owner, cmd.origin_id); policy = "query"
    return Classified(kind, policy, document({"type": kind, **_wire(cmd)}), tuple(sorted(scope)))


def decode(payload):
    """Validate a persisted payload by reconstructing its exact registered type."""
    raw = json.loads(payload)
    if type(raw) is not dict: raise ValueError("invalid action payload")
    raw = dict(raw); kind = raw.pop("type")
    cls = BotDo if kind == "bot_do" else p.COMMANDS[kind]
    if "items" in raw:
        raw["items"] = tuple(p.MailItem(i["item_id"], i["count"], i["from"]) for i in raw["items"])
    for key in ("guids", "gankers", "guilds", "item_ids"):
        if key in raw: raw[key] = tuple(raw[key])
    cmd = cls(**raw)
    result = classify(cmd, child=True)
    if result.payload != payload: raise ValueError("noncanonical action payload")
    return cmd, result
