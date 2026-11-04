"""The canon's grants (plan Task 16): his OK on the history, the grant plan, its step, the grants.

The canon is the guild's history extension: three realm firsts it took (REALM_FIRSTS, to him
alone, since playerbots' guard refuses them to the cast) and four legends (LEGENDS, to the
members the history names, and to him when it names him). They reach the game only through
native's quiet grant_achievement (no achievement event, so no gz and no "I earned" memory), and
only after two of his OKs:

- ``realm canon history-ok <seed> <canon>`` writes realm/approved/history-<seed sha>-<canon
  sha>.json: his OK on the extension's seed (P1b's memories seed format) and its canon sidecar
  ({"grants": {...}, "kills": [...]}), bound to both files' SHA-256 and where they are. History
  is OK only while the newest such approval still matches both files, every moment the sidecar
  names exists (a realm first's in the seed, a kill's in the seed or the seeded history), and
  the store holds at least three of his active owner memories owner:<moment>:* for each realm
  first's moment.
- ``realm plan canon`` writes the canon plan: the holders of all seven ids, the recipients, his
  GM level and RBAC permission 8, the canon kills. ``realm approve`` binds his OK to it, and the
  Steps part applies it once. It is refused for good (realm_canon_refused reason=...) without
  history_ok, then when the plan's hashes are not the approved history's; otherwise it stores
  realm_canon_kill, asks Race.seed_canon, and plans one realm_canon row per grant (blocked
  instead for a holder outside the guild, and for his realm firsts with rbac_8).

The Canon part then sends each planned grant through the direct ledger (owner "canon", ref
canon:<achievement>:<guid>:<tries>), one at a time and never through the body ledger, so his
raid's hold never delays one: the cast's while he is offline, one a second; his from 30 s after
his login, outside his raid's fights, the first alone and then one every 2 s. The gate
(history_ok, the applied plan, its hashes) is checked again before every send. Native's answer
settles the row: ok and has_it are granted; gm_mode, busy:* and realm_off leave it planned
(tries + 1) and his whole queue waits 600 s within his session (a cast member's row alone
waits); offline waits for his next login (a cast member's row 600 s); refused, or any other
refusal, is final until a new approved plan. An unknown send is never resent: its ref stays
taken, and the row is tried with a fresh ref at his next login (a cast member's after 600 s),
which is safe because native answers has_it for a grant it already made. After the first real
ok of his session the historian (else the standing raid lead) whispers him once.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid

from leeroy_mind.memory.owner_seed import _FIELDS as SEED_FIELDS, FICTION_NOW
from leeroy_mind.protocol import Ack, event_type
from leeroy_mind.talk.turns import TurnKind

from .armory import GUILD_ID
from .direct import busy
from .ports import armory_from_config
from .protocol import GrantAchievement
from .steps import StepError

log = logging.getLogger(__name__)

REALM_FIRSTS = {3117: "Death's Demise", 4078: "Grand Crusader", 4576: "Fall of the Lich King"}
LEGENDS = frozenset({2186, 3007, 1723, 1786})
GRANTABLE = frozenset(REALM_FIRSTS) | LEGENDS  # native's and action_policy's own whitelist
OWNER = STEP = "canon"
SECTION = "realm_canon"
WHISPER = "updated the guild records, check your achievements"
REALM_FIRST_NOTE = "playerbots' guard refuses realm firsts to the cast: they go to him only"
GM_NOTE = "his account has a GM level: while he plays in GM mode his grants wait until he leaves it"
LOGIN_DELAY_S = 30   # his first grant, after his login
HIS_GAP_S = 2        # his next, after an answer
CAST_GAP_S = 1       # the cast's, one a second while he is offline
RETRY_S = 600        # a deferred grant, within his session
UNSENT_RETRY_S = 60  # a grant that never reached the wire
MIN_MEMORIES = 3
_MAX_BYTES = 16 << 20
_U32 = 0xFFFFFFFF
_SHA = re.compile(r"[0-9a-f]{64}")
_APPROVAL = re.compile(r"history-([0-9a-f]{64})-([0-9a-f]{64})\.json")
_MOMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_OWNER_KEY = re.compile(r"owner:([A-Za-z0-9][A-Za-z0-9_-]{0,31}):[0-9]+")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_KILL_KEYS = frozenset({"map", "boss_id", "difficulty", "moment", "day"})
_LEGEND_KEYS = frozenset({"moment", "cast", "human"})
_FIGHTS = frozenset({"combat", "wipe_silence", "wipe_debrief"})
# Refusals that did nothing and may pass: native's own, busy:* (the ledger keeps it as busy_*)
# and the ledger's fallback for a reason it cannot store.
_DEFER = frozenset({"gm_mode", "realm_off", "native_refused"})
_UNSENT = frozenset({"pretransport_refused", "context_unavailable", "unsent", "recovered_prepared"})


class CanonError(StepError):
    """A refused history file, recipients file or plan. Fixed text and ids, never a path."""


# -- the history gate ----------------------------------------------------------------------------

@dataclass(frozen=True)
class History:
    """Whether his history approval holds, and what the approved files say."""
    ok: bool
    reason: str = ""
    seed_sha256: str | None = None
    canon_sha256: str | None = None
    grants: dict[int, str] = field(default_factory=dict)  # realm first -> its moment
    kills: tuple[dict, ...] = ()


def _strict(value):
    raise ValueError("non-finite number")


def _json(data: bytes, what: str):
    try:
        return json.loads(data.decode("utf-8"), parse_constant=_strict)
    except (UnicodeError, ValueError):
        raise CanonError(f"the {what} is not JSON") from None


def _read(path, what: str) -> bytes:
    try:
        path = Path(path)
        if path.stat().st_size > _MAX_BYTES:
            raise CanonError(f"the {what} is too large")
        return path.read_bytes()
    except OSError:
        raise CanonError(f"the {what} cannot be read") from None


def _moment(value) -> bool:
    return type(value) is str and _MOMENT.fullmatch(value) is not None


def _int(value, low=0, high=_U32) -> bool:
    return type(value) is int and low <= value <= high


def parse_seed(data: bytes) -> frozenset[str]:
    """The moments of a history seed in P1b's `memories seed` format: a list of entries with
    its own fields only (I12), no sidecar key."""
    entries = _json(data, "history seed")
    if type(entries) is not list or not entries:
        raise CanonError("the history seed is not a list of entries")
    moments = set()
    for entry in entries:
        if type(entry) is not dict or not set(entry) <= SEED_FIELDS or not _moment(entry.get("moment")):
            raise CanonError("a history seed entry is not in the memories seed format")
        moments.add(entry["moment"])
    return frozenset(moments)


def _kill(row) -> dict:
    if (type(row) is not dict or set(row) != _KILL_KEYS or not _int(row["map"], 1)
            or not _int(row["boss_id"]) or not _int(row["difficulty"], 0, 3) or not _moment(row["moment"])
            or type(row["day"]) is not str or _DAY.fullmatch(row["day"]) is None):
        raise CanonError("a canon kill is not {map, boss_id, difficulty, moment, day}")
    try:
        day = date.fromisoformat(row["day"])
    except ValueError:
        raise CanonError("a canon kill's day is not a date") from None
    if day > date.fromisoformat(FICTION_NOW):
        raise CanonError("a canon kill's day is after the fiction's today")
    return {key: row[key] for key in ("map", "boss_id", "difficulty", "moment", "day")}


def _kills(rows) -> tuple[dict, ...]:
    if type(rows) is not list:
        raise CanonError("the canon kills are not a list")
    kills = [_kill(row) for row in rows]
    keys = [(k["map"], k["boss_id"], k["difficulty"]) for k in kills]
    if len(set(keys)) != len(keys):
        raise CanonError("a canon kill is listed twice")
    return tuple(kills)


def parse_canon(data: bytes) -> tuple[dict[int, str], tuple[dict, ...]]:
    """The sidecar's realm-first moments and kills (PR11, I14)."""
    doc = _json(data, "canon sidecar")
    if type(doc) is not dict or set(doc) != {"grants", "kills"}:
        raise CanonError("the canon sidecar is not {grants, kills}")
    grants = doc["grants"]
    if (type(grants) is not dict or set(grants) != {str(a) for a in REALM_FIRSTS}
            or not all(_moment(moment) for moment in grants.values())):
        raise CanonError("the canon sidecar's grants must map each realm first to its moment")
    return {int(a): moment for a, moment in grants.items()}, _kills(doc["kills"])


def _owner_moments(conn) -> Counter:
    """How many of his active owner memories each moment has (keys owner:<moment>:<guid>)."""
    counts = Counter()
    for (key,) in conn.execute("SELECT key FROM memory WHERE source='owner' AND status='active' "
                               "AND key GLOB 'owner:*'"):
        match = _OWNER_KEY.fullmatch(key) if type(key) is str else None
        if match is not None:
            counts[match.group(1)] += 1
    return counts


def _approvals(realm_dir: Path) -> list[tuple[float, str, dict]]:
    directory = Path(realm_dir) / "approved"
    found = []
    if not directory.is_dir():
        return found
    for path in directory.glob("history-*.json"):
        match = _APPROVAL.fullmatch(path.name)
        if match is None:
            continue
        try:
            doc = _json(_read(path, "history approval"), "history approval")
        except CanonError:
            continue
        if (type(doc) is dict and doc.get("seed_sha256") == match.group(1)
                and doc.get("canon_sha256") == match.group(2) and type(doc.get("seed")) is str
                and type(doc.get("canon")) is str and type(doc.get("approved_at")) in (int, float)
                and math.isfinite(doc["approved_at"])):
            found.append((float(doc["approved_at"]), path.name, doc))
    return sorted(found)


def check_history(realm_dir, conn) -> History:
    """His history approval against the files and the store, read only.

    The newest approval counts: it must still match both files byte for byte, the sidecar's
    grants and kills must name moments that exist, and each realm first's moment needs
    MIN_MEMORIES of his seeded memories."""
    approvals = _approvals(realm_dir)
    if not approvals:
        return History(False, "no_history_approval")
    doc = approvals[-1][2]
    try:
        seed_data, canon_data = _read(doc["seed"], "history seed"), _read(doc["canon"], "canon sidecar")
    except CanonError:
        return History(False, "history_unreadable")
    seed_sha, canon_sha = (hashlib.sha256(seed_data).hexdigest(), hashlib.sha256(canon_data).hexdigest())
    if (seed_sha, canon_sha) != (doc["seed_sha256"], doc["canon_sha256"]):
        return History(False, "history_changed")
    try:
        seeded, (grants, kills) = parse_seed(seed_data), parse_canon(canon_data)
    except CanonError:
        return History(False, "history_invalid")
    stored = _owner_moments(conn)
    if (not set(grants.values()) <= seeded
            or not {k["moment"] for k in kills} <= seeded | {m for m, n in stored.items() if n}):
        return History(False, "moments_missing")
    if any(stored[moment] < MIN_MEMORIES for moment in set(grants.values())):
        return History(False, "memories_missing")
    return History(True, "", seed_sha, canon_sha, grants, kills)


def history_ok(data_dir, store) -> bool:
    conn = store.conn if hasattr(store, "conn") else store
    return check_history(Path(data_dir) / "realm", conn).ok


def approve_history(realm_dir, seed, canon, *, now: float) -> Path:
    """His OK on the history extension: realm/approved/history-<seed sha>-<canon sha>.json, with
    both files' paths so the gate can hash them again. Both files must be well formed."""
    if type(now) not in (int, float) or not math.isfinite(now):
        raise CanonError("invalid approval time")
    seed, canon = Path(seed).resolve(), Path(canon).resolve()
    seed_data, canon_data = _read(seed, "history seed"), _read(canon, "canon sidecar")
    parse_seed(seed_data)
    parse_canon(canon_data)
    seed_sha, canon_sha = hashlib.sha256(seed_data).hexdigest(), hashlib.sha256(canon_data).hexdigest()
    target = Path(realm_dir) / "approved" / f"history-{seed_sha}-{canon_sha}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"seed": str(seed), "canon": str(canon), "seed_sha256": seed_sha,
                       "canon_sha256": canon_sha, "approved_at": float(now)}, sort_keys=True) + "\n"
    tmp = target.with_name(f".{target.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


# -- the plan ------------------------------------------------------------------------------------

def read_recipients(doc, *, human: int, cast: frozenset[int]) -> dict[int, tuple[str, tuple[int, ...], bool]]:
    """The legend recipients file (M37): {"<legend>": {"moment", "cast": [guids], "human"}}."""
    if type(doc) is not dict:
        raise CanonError("the legend recipients file is not an object")
    legends = {}
    for key, value in doc.items():
        if type(key) is not str or not key.isdecimal() or len(key) > 10:
            raise CanonError("a legend's achievement id is not a number")
        achievement = int(key)
        if achievement not in LEGENDS:
            raise CanonError(f"achievement {achievement} is not one of the canon's legends")
        if type(value) is not dict or set(value) != _LEGEND_KEYS:
            raise CanonError(f"legend {achievement} needs exactly moment, cast and human")
        if not _moment(value["moment"]):
            raise CanonError(f"legend {achievement}: its moment must be an id such as M28")
        if type(value["human"]) is not bool:
            raise CanonError(f"legend {achievement}: human must be true or false")
        if type(value["cast"]) is not list:
            raise CanonError(f"legend {achievement}: cast must be a list of guids")
        members = []
        for guid in value["cast"]:
            if not _int(guid, 1):
                raise CanonError(f"legend {achievement}: cast must be a list of guids")
            if guid == human:
                raise CanonError(f'legend {achievement}: guid {guid} is him; name him with "human": true')
            if guid not in cast:
                raise CanonError(f"legend {achievement}: guid {guid} is not in the cast")
            if guid in members:
                raise CanonError(f"legend {achievement}: guid {guid} is listed twice")
            members.append(guid)
        legends[achievement] = (value["moment"], tuple(sorted(members)), value["human"])
    return legends


def _order(achievement: int) -> tuple[int, int]:
    """Realm firsts in the history's order (M35-M37), then the legends by id."""
    firsts = tuple(REALM_FIRSTS)
    return (firsts.index(achievement), 0) if achievement in REALM_FIRSTS else (len(firsts), achievement)


def plan_canon(armory, recipients, history: History, *, human: int, cast) -> dict:
    """The canon plan (read only): every id's holders, its recipients and their grants, his
    account's GM level and RBAC permission 8, and the canon kills with counts per raid."""
    if type(history) is not History or not history.ok:
        raise CanonError("the history is not approved; run realm canon history-ok first")
    if not _int(human, 1):
        raise CanonError("his character is unknown")
    cast = frozenset(cast) - {human}
    legends = read_recipients(recipients, human=human, cast=cast)
    ids = sorted(GRANTABLE)
    holders = armory.achievement_holders(ids)
    gm_level = armory.account_gm_level(human)
    rbac_8 = armory.account_has_permission(human, 8)
    if not _int(gm_level, 0, 255) or type(rbac_8) is not bool or type(holders) is not dict:
        raise CanonError("his account could not be read")
    achievements, grants = {}, []
    for achievement in sorted(GRANTABLE, key=_order):
        held = sorted(holders.get(achievement, ()))
        if achievement in REALM_FIRSTS:
            entry = {"kind": "realm_first", "name": REALM_FIRSTS[achievement],
                     "moment": history.grants[achievement], "note": REALM_FIRST_NOTE}
            to = [human]
        else:
            moment, members, him = legends.get(achievement, ("", (), False))
            entry = {"kind": "legend", "moment": moment}
            to = [*members, human] if him else list(members)
        blocked = "holder_outside_guild" if any(guild != GUILD_ID for _, _, guild in held) else None
        entry.update(holders=[{"guid": g, "name": name, "guild": guild} for g, name, guild in held],
                     recipients=to, blocked=blocked)
        achievements[str(achievement)] = entry
        for guid in to:
            reason = blocked or ("rbac_8" if rbac_8 and guid == human and achievement in REALM_FIRSTS else "")
            grants.append({"achievement": achievement, "guid": guid,
                           "state": "blocked" if reason else "planned", "reason": reason})
    notes = [REALM_FIRST_NOTE]
    if gm_level > 0:
        notes.append(GM_NOTE)
    counts = Counter(str(kill["map"]) for kill in history.kills)
    return {"version": 1, "guild": GUILD_ID, "human": human,
            "seed_sha256": history.seed_sha256, "canon_sha256": history.canon_sha256,
            "account": {"gm_level": gm_level, "rbac_8": rbac_8}, "achievements": achievements,
            "grants": grants, "kills": [dict(kill) for kill in history.kills],
            "kill_counts": dict(sorted(counts.items())), "notes": notes}


@dataclass(frozen=True)
class _Plan:
    human: int
    seed_sha256: str
    canon_sha256: str
    grants: tuple[tuple[int, int, str, str], ...]
    kills: tuple[dict, ...]


def _parse_plan(payload) -> _Plan:
    """An approved plan's payload, checked again before anything is stored from it."""
    if (type(payload) is not dict or payload.get("version") != 1 or payload.get("guild") != GUILD_ID
            or not _int(payload.get("human"), 1) or type(payload.get("seed_sha256")) is not str
            or type(payload.get("canon_sha256")) is not str or not _SHA.fullmatch(payload["seed_sha256"])
            or not _SHA.fullmatch(payload["canon_sha256"]) or type(payload.get("grants")) is not list):
        raise CanonError("not a canon plan")
    human, grants, seen = payload["human"], [], set()
    for grant in payload["grants"]:
        if (type(grant) is not dict or set(grant) != {"achievement", "guid", "state", "reason"}
                or grant["achievement"] not in GRANTABLE or type(grant["achievement"]) is not int
                or not _int(grant["guid"], 1) or grant["state"] not in ("planned", "blocked")
                or type(grant["reason"]) is not str or len(grant["reason"]) > 64
                or (grant["state"] == "blocked") != bool(grant["reason"])
                or (grant["achievement"] in REALM_FIRSTS and grant["guid"] != human)
                or (grant["achievement"], grant["guid"]) in seen):
            raise CanonError("not a canon plan")
        seen.add((grant["achievement"], grant["guid"]))
        grants.append((grant["achievement"], grant["guid"], grant["state"], grant["reason"]))
    return _Plan(human, payload["seed_sha256"], payload["canon_sha256"], tuple(grants),
                 _kills(payload.get("kills")))


# -- the CLI -------------------------------------------------------------------------------------

def history_ok_cli(ctx) -> int:
    """realm canon history-ok <seed file> <canon file>: his OK on the history extension."""
    if len(ctx.argv) != 2:
        print("realm: canon history-ok takes <seed file> <canon file>")
        return 1
    target = approve_history(ctx.root / "realm", ctx.argv[0], ctx.argv[1], now=ctx.clock.now())
    approval = json.loads(target.read_text(encoding="utf-8"))
    try:
        with closing(ctx.connect()) as conn:
            history = check_history(ctx.root / "realm", conn)
        ok, reason = history.ok, history.reason
    except sqlite3.Error:
        ok, reason = None, "store_unreadable"
    print(json.dumps({"approved": target.name, "seed_sha256": approval["seed_sha256"],
                      "canon_sha256": approval["canon_sha256"], "history_ok": ok, "reason": reason},
                     sort_keys=True, indent=2))
    return 0


def _recipients_path(ctx) -> Path | None:
    argv = list(ctx.argv)
    if not argv:
        return ctx.root / "realm" / "legend-recipients.json"
    if len(argv) == 2 and argv[0] == "--recipients":
        return Path(argv[1])
    if len(argv) == 1 and argv[0].startswith("--recipients="):
        return Path(argv[0].split("=", 1)[1])
    return None


def _his_guid(conn) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='human_guid'").fetchone()
    stored = row[0] if row is not None else None
    if type(stored) is str and stored.isascii() and stored.isdecimal() and 0 < int(stored) <= _U32:
        return int(stored)
    masters = [g for (g,) in conn.execute("SELECT guid FROM cast_member WHERE rank=0 ORDER BY guid")]
    if not masters:
        raise CanonError("his character is unknown")
    return masters[0]


def plan_cli(ctx) -> int:
    """realm plan canon [--recipients <file>]: reads mind.db and the armory, writes the plan."""
    path = _recipients_path(ctx)
    if path is None:
        print("realm: plan canon takes only --recipients <file>")
        return 1
    with closing(ctx.connect()) as conn:
        history = check_history(ctx.root / "realm", conn)
        if not history.ok:
            print(f"realm: canon plan refused: the history is not approved ({history.reason})")
            return 1
        human = _his_guid(conn)
        cast = frozenset(g for (g,) in conn.execute("SELECT guid FROM cast_member WHERE level>=80"))
    recipients = _json(_read(path, "legend recipients file"), "legend recipients file")
    armory = armory_from_config(ctx.root / "mind.toml")
    if armory is None:
        print("realm: canon plan refused: the armory is unavailable")
        return 1
    payload = plan_canon(armory, recipients, history, human=human, cast=cast)
    written = ctx.write_plan(STEP, payload)
    doc = json.loads(written.read_text(encoding="utf-8"))
    print(json.dumps({"plan": written.name, "sha256": doc["sha256"], "payload": payload},
                     sort_keys=True, indent=2))
    return 0


# -- the weekly report ---------------------------------------------------------------------------

def report_section(ctx, week) -> dict:
    """The weekly report's canon section: the week's deferrals and refusals, and what is blocked."""
    from leeroy_mind.nightly.settings import week_bounds
    start, end = week_bounds(week, ctx.tz)
    with ctx.store.transaction():
        rows = ctx.store.conn.execute(
            "SELECT achievement,guid,state,reason,tries,ts FROM realm_canon "
            "ORDER BY achievement,guid").fetchall()

    def item(row):
        return {"achievement": row[0], "guid": row[1], "reason": row[3], "tries": row[4]}
    within = [row for row in rows if start <= row[5] < end]
    return {"status": "complete",
            "deferred": [item(r) for r in within if r[2] == "planned" and r[4] > 0],
            "refused": [item(r) for r in within if r[2] == "refused"],
            "blocked": [item(r) for r in rows if r[2] == "blocked"],
            "granted": sum(r[2] == "granted" for r in within),
            "planned": sum(r[2] == "planned" for r in rows)}


# -- the part ------------------------------------------------------------------------------------

def _ref(achievement: int, guid: int, tries: int) -> str:
    return f"canon:{achievement}:{guid}:{tries}"


def _reason(text) -> str | None:
    return text if type(text) is str and 0 < len(text) <= 64 and text.isprintable() else None


def _outcome(state, reason) -> str:
    """What an attempt's ledger state (and native's reason) means for its row."""
    if state in (None, "abandoned"):
        return "unsent"
    if state in ("prepared", "dispatched"):
        return "pending"
    if state == "acked":
        return "ok"
    if state == "unknown":
        return "unknown"
    reason = reason or ""
    if reason == "has_it":
        return "has_it"
    if reason in _DEFER or busy(reason):
        return "deferred"
    if reason == "offline":
        return "offline"
    if reason in _UNSENT:
        return "unsent"
    return "refused"


class Canon:
    """The canon's realm part: its step's apply, and the grants on the Realm's own tick."""

    name = "canon"

    def __init__(self, realm):
        self.realm = realm
        self._session = None      # his current session's start, None while he is offline
        self._session_n = 0
        self._his_next = 0.0      # his queue's next send, no sooner
        self._cast_next = 0.0     # the cast's
        self._wait = {}           # (achievement, guid) -> a cast member's row waits until
        self._skip = {}           # (achievement, guid) -> his row sits out that session
        self._task = None         # the one grant in flight
        self._whispered = 0       # the session whose whisper went out
        self._whispering = None
        self._noted = set()

    # -- the step --

    async def apply(self, payload, progress):
        """Step "canon": refused for good, or kills, Race.seed_canon and the planned rows."""
        reason, plan = self._admit(payload)
        if reason is not None:
            log.warning("realm_canon_refused reason=%s", reason)
            return {"refused": "realm_canon_refused", "reason": reason}
        store, now = self.realm.app.store, self.realm.app.clock.now()
        with store.transaction():
            conn = store.conn
            for kill in plan.kills:
                conn.execute(
                    "INSERT INTO realm_canon_kill(map,boss_id,difficulty,moment,day) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(map,boss_id,difficulty) DO UPDATE SET moment=excluded.moment,day=excluded.day",
                    (kill["map"], kill["boss_id"], kill["difficulty"], kill["moment"], kill["day"]))
            for achievement, guid, state, why in plan.grants:
                # A granted row is never touched; his new OK re-plans the rest.
                conn.execute(
                    "INSERT INTO realm_canon(achievement,guid,reason,ts,tries,state) VALUES (?,?,?,?,0,?) "
                    "ON CONFLICT(achievement,guid) DO UPDATE SET state=excluded.state,"
                    "reason=excluded.reason,ts=excluded.ts WHERE realm_canon.state<>'granted'",
                    (achievement, guid, why, now, state))
        seeded = await self._seed_race()
        log.info("realm_canon_applied kills=%d grants=%d", len(plan.kills), len(plan.grants))
        return {"seed_sha256": plan.seed_sha256, "canon_sha256": plan.canon_sha256, "human": plan.human,
                "kills": len(plan.kills), "planned": sum(g[2] == "planned" for g in plan.grants),
                "blocked": sum(g[2] == "blocked" for g in plan.grants), "race_seeded": seeded}

    def _admit(self, payload):
        """The order of refusals: no history_ok, then hashes that are not the approved
        history's (his OK on the plan is the step's own approval). Then the plan itself."""
        history = check_history(self.realm.ports.realm_dir(), self.realm.ports.conn)
        if not history.ok:
            return "history_not_ok", None
        try:
            plan = _parse_plan(payload)
        except CanonError:
            return "invalid_plan", None
        if (plan.seed_sha256, plan.canon_sha256) != (history.seed_sha256, history.canon_sha256):
            return "history_hash_changed", None
        if plan.kills != history.kills:
            return "invalid_plan", None
        if plan.human != self.realm.action_context().human:
            return "human_changed", None
        return None, plan

    async def _seed_race(self):
        race = self.realm.part("race")
        seed = getattr(race, "seed_canon", None)
        if not callable(seed):
            return None
        try:
            result = seed()
            if inspect.isawaitable(result):
                await result
        except Exception as error:  # The race's fault is not the canon's; its rows are stored.
            log.warning("realm_canon_race_seed_failed error=%s", type(error).__name__)
            return False
        return True

    def _bound(self):
        """The hashes of the newest applied canon step, or None (none, or it was refused)."""
        row = self.realm.ports.conn.execute(
            "SELECT result FROM realm_step WHERE name=? AND applied IS NOT NULL "
            "ORDER BY applied DESC, rowid DESC LIMIT 1", (STEP,)).fetchone()
        try:
            result = json.loads(row[0]) if row is not None else None
        except ValueError:
            return None
        if type(result) is not dict or "refused" in result:
            return None
        seed, canon = result.get("seed_sha256"), result.get("canon_sha256")
        if type(seed) is not str or type(canon) is not str or not _SHA.fullmatch(seed) or not _SHA.fullmatch(canon):
            return None
        return seed, canon

    def _gate(self) -> str | None:
        """Why no grant may go now, or None: history_ok, an applied plan, the same hashes."""
        history = check_history(self.realm.ports.realm_dir(), self.realm.ports.conn)
        if not history.ok:
            return "history_not_ok"
        bound = self._bound()
        if bound is None:
            return "no_grant_approval"
        if bound != (history.seed_sha256, history.canon_sha256):
            return "history_hash_changed"
        return None

    # -- the realm's calls --

    async def on_event(self, ev):
        try:
            name = event_type(ev)
            if name in ("login", "logout") and ev.human and ev.guid == self.realm.ports.human().guid:
                if name == "login":
                    self._begin(self.realm.app.clock.now())
                else:
                    self._session = None
        except Exception as error:  # Never the Realm's failure.
            self._note("realm_canon_event_failed", type(error).__name__)

    async def tick(self):
        try:
            self._tick()
        except Exception as error:  # Never the Realm loop's failure.
            self._note("realm_canon_tick_failed", type(error).__name__)

    def _begin(self, now):
        self._session, self._session_n = now, self._session_n + 1
        self._his_next = now + LOGIN_DELAY_S

    def _note(self, event, detail=""):
        """A warning once until it changes; never per tick."""
        if (event, detail) not in self._noted:
            self._noted.add((event, detail))
            log.warning("%s", f"{event} {detail}".rstrip())

    def _tick(self):
        realm, now = self.realm, self.realm.app.clock.now()
        online = realm.ports.human_online() is True
        if not online:
            self._session = None
        elif self._session is None:
            self._begin(now)  # Online with no login seen (the mind came up while he played).
        if self._task is not None:
            return
        context = realm.action_context()
        if not (context.enabled and context.connected and context.presence_known):
            return
        ledger = realm.part("direct")
        if ledger is None:
            self._note("realm_canon_no_direct_ledger")
            return
        if online and (now < self._his_next or self._fighting()):
            return
        if not online and now < self._cast_next:
            return
        row = self._next(ledger, his=online, human=context.human, now=now)
        if row is None:
            return
        refusal = self._gate()
        if refusal is not None:
            self._note("realm_canon_refused", f"reason={refusal}")
            return
        self._noted.difference_update({n for n in self._noted if n[0] == "realm_canon_refused"})
        task = realm.spawn(lambda: self._grant(row, context, his=online, started=now))
        if task is not None:
            self._task = task
            task.add_done_callback(self._sent)

    def _sent(self, task):
        if self._task is task:
            self._task = None
        if not task.cancelled() and task.exception() is not None:
            log.warning("realm_grant_failed error=%s", type(task.exception()).__name__)

    def _fighting(self) -> bool:
        """His raid's fights (combat, the wipe's silence and debrief); unreadable counts."""
        try:
            phase = self.realm.ports.raid_phase()
        except Exception:
            return True
        return str(getattr(phase, "value", phase)) in _FIGHTS

    def _next(self, ledger, *, his, human, now):
        """The next planned row of his queue or the cast's that may be sent now. A row whose
        last attempt settled without it (a cancelled send, an earlier mind) is settled first,
        and that is this tick's work."""
        rows = self.realm.ports.conn.execute(
            "SELECT achievement,guid,tries FROM realm_canon WHERE state='planned' AND "
            + ("guid=?" if his else "guid<>?") + " ORDER BY achievement,guid", (human,)).fetchall()
        for achievement, guid, tries in sorted(rows, key=lambda r: (_order(r[0]), r[1])):
            key = (achievement, guid)
            if (achievement not in GRANTABLE or not _int(guid, 1) or not _int(tries, 0, 1 << 30)
                    or (achievement in REALM_FIRSTS and guid != human)):
                self._note("realm_canon_row_invalid", f"achievement={achievement}")
                continue
            if his and self._skip.get(key) == self._session_n:
                continue
            if not his and self._wait.get(key, 0.0) > now:
                continue
            state = ledger.state(OWNER, _ref(achievement, guid, tries))
            outcome = _outcome(*(state[:2] if state is not None else (None, None)))
            if outcome == "pending":
                continue
            row = (achievement, guid, tries)
            if outcome == "unsent":
                return row
            self._settle(row, outcome, state[1])
            self._pace(row, outcome, his=his, now=now, started=now, own=False, human=human)
            return None
        return None

    async def _grant(self, row, context, *, his, started):
        """One grant: prepared under its ref, sent, and its row settled from the ledger."""
        achievement, guid, tries = row
        ledger, ref = self.realm.part("direct"), _ref(*row)
        cmd = GrantAchievement(uuid.uuid4().hex, guid, achievement)
        if ledger.prepare(cmd, owner=OWNER, ref=ref, context=context, expects_job=False) is None:
            return
        ack = None
        try:
            ack = await self.realm.ports.send(cmd)
        except Exception as error:  # The ledger row says what became of it; never resent.
            log.debug("realm_grant_send_failed error=%s", type(error).__name__)
        finally:
            ledger.abandon_unsent(cmd)  # Only a row that never reached its barrier.
        state = ledger.state(OWNER, ref)
        if state is None:
            return
        reason = (_reason(ack.error) if type(ack) is Ack and ack.ok is False else None) or state[1]
        outcome = _outcome(state[0], reason)
        self._settle(row, outcome, reason)
        self._pace(row, outcome, his=his, now=self.realm.app.clock.now(), started=started, own=True,
                   human=context.human)

    def _settle(self, row, outcome, reason):
        achievement, guid, tries = row
        if outcome in ("ok", "has_it"):
            state, why, tries_after = "granted", "" if outcome == "ok" else "has_it", tries
            log.info("realm_grant_ok achievement=%d guid=%d answer=%s", achievement, guid, outcome)
        elif outcome == "refused":
            state, why, tries_after = "refused", _reason(reason) or "refused", tries
            log.warning("realm_grant_refused achievement=%d guid=%d reason=%s", achievement, guid, why)
        elif outcome in ("deferred", "offline", "unknown"):
            state, why, tries_after = "planned", "unknown" if outcome == "unknown" else (
                _reason(reason) or "native_refused"), tries + 1
            log.info("realm_grant_deferred reason=%s achievement=%d guid=%d tries=%d",
                     why, achievement, guid, tries_after)
        else:
            return
        store = self.realm.app.store
        with store.transaction():
            store.conn.execute(
                "UPDATE realm_canon SET state=?,reason=?,tries=?,ts=? WHERE achievement=? AND guid=? "
                "AND state='planned' AND tries=?",
                (state, why, tries_after, self.realm.app.clock.now(), achievement, guid, tries))

    def _pace(self, row, outcome, *, his, now, started, own, human):
        key = (row[0], row[1])
        if his:
            if outcome == "deferred":
                self._his_next = now + RETRY_S       # gm_mode and busy hold him as a whole
            elif outcome == "offline":
                self._his_next = math.inf            # until his next login
            elif outcome == "unsent":
                self._his_next = now + UNSENT_RETRY_S
            else:
                if outcome == "unknown" and own:
                    self._skip[key] = self._session_n
                self._his_next = now + HIS_GAP_S
            if outcome == "ok":
                self._whisper(human)
        else:
            if outcome in ("deferred", "offline", "unknown"):
                self._wait[key] = now + RETRY_S
            self._cast_next = now + UNSENT_RETRY_S if outcome == "unsent" else started + CAST_GAP_S

    # -- the whisper --

    def _whisperer(self, human):
        ports = self.realm.ports
        try:
            historians = sorted(g for g in ports.by_title("historian") if g != human)
        except Exception:
            historians = []
        if historians:
            return historians[0]
        guard = getattr(self.realm, "guard", None)
        try:
            return guard.standing_lead(human) if guard is not None else None
        except Exception:
            return None

    def _whisper(self, human):
        """Once a session, after its first real ok: the historian's (else the lead's) line."""
        if self._whispered == self._session_n or self._whispering is not None or self._session is None:
            return
        speaker = self._whisperer(human)
        if speaker is None:
            self._note("realm_canon_no_whisperer")
            return
        session, ports, app = self._session_n, self.realm.ports, self.realm.app
        reached = False

        def barrier():  # One typed line, at most once, only while his session and the model hold.
            nonlocal reached
            if reached or self._session_n != session or getattr(app, "model_ready", False) is not True \
                    or ports.human_online() is not True:
                return False
            reached = True
            return True

        async def whisper():
            if await ports.say(speaker, "whisper", WHISPER, to_guid=human, kind=TurnKind.WHISPER,
                               before_transport=barrier):
                self._whispered = session

        def done(task):
            if self._whispering is task:
                self._whispering = None
        task = self.realm.spawn(whisper)
        if task is not None:
            self._whispering = task
            task.add_done_callback(done)


def attach(realm):
    """The canon part, its step and (with the Realm enabled) its weekly report section. No I/O."""
    part = Canon(realm)
    steps = realm.part("steps")
    if steps is not None:
        steps.register(STEP, part.apply)
    else:
        log.warning("realm_canon_no_steps")
    if realm.enabled:
        from leeroy_mind.eval import register_section
        register_section(SECTION, report_section)
    return part, ("login", "logout")
