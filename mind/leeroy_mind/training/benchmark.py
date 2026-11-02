"""Weekly rival observations with durable native-run cleanup ownership.

The schedule adapter supplies current facts; this module does not infer idle
guild/lab state or absence of a body arbiter. Call on_pull after ledger storage.
Damage counters are requested_pre_ai, so actual-DPS percentiles are unavailable.
"""

import asyncio
import json
import logging
import math
import re
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from leeroy_mind.guild.comp import Candidate, NoTankError, select_comp
from leeroy_mind.guild.protocol import Job
from leeroy_mind.guild.raids import split_key
from leeroy_mind.guild.roles import role_of
from leeroy_mind.memory.writer import MemoryCandidate
from leeroy_mind.protocol import Ack, encode_command
from leeroy_mind.wow import BOSS_NAMES, NON_FIGHT_STEPS

from .protocol import GetRivals, GetTrainingCleanup, Pull, Rival, Rivals, StartBenchmark, TrainingCleanup
from .score import is_farm


log = logging.getLogger("leeroy_mind")
_ACTIVE = "tg:benchmark:active"
_OUTBOX = "tg:benchmark:memories"
_CLASSES = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11})
_MAX_PULLS = 256
DPS_UNAVAILABLE = "requested_pre_ai is not verified health damage; actual DPS percentiles unavailable"
# Audited StartRaidJob returns these before assigning active or touching a rival.
_REFUSED = frozenset({"busy", "disabled", "identity_unavailable", "keep_cast_off", "training_off",
    "loot_guard_unavailable", "pin_owner_busy", "human_online", "training_commands_pending",
    "training_journal_pending", "leader_not_tank", "no_difficulty", "no_entrance", "driver_pending",
    "driver_unavailable:no_dungeon_clear", "driver_unavailable:instance_strategies_off"})


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _positive(value):
    return type(value) is int and 0 < value <= 0xffffffff


def _wire_id(value):
    return type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is not None


def _receipt(value):
    try:
        if not isinstance(value, dict) or not isinstance(value.get("members"), (list, tuple)):
            return None
        return TrainingCleanup(**(value | {"members": tuple(value["members"])}))
    except (TypeError, ValueError, KeyError):
        return None


def _native_identity(proof):
    result = {key: value for key, value in asdict(proof).items() if key not in {"id", "ts", "state"}}
    result["members"] = sorted(result["members"])
    return result


def _proof_matches(state, proof):
    return (proof.state != "unknown" and proof.training_run == state["handle"]
        and proof.ts >= int(state["started"] * 1000) and proof.job_id == state["command_id"]
        and proof.leader_guid == state["leader"] and proof.guild_id == state["cast_guild"]
        and proof.human_guid == state["human"] and proof.map_id == state["map"]
        and proof.instance_id == state["instance"] and set(proof.members) == set(state["members"]))


def _saved_complete(state):
    proof = _receipt(state["proof"])
    return (proof is not None and proof.state == "complete" and _proof_matches(state, proof)
        and _native_identity(proof) == state["binding"]
        and state.get("query", {}).get("id") == proof.id and state.get("query_receipt") == state["proof"])


def _copy(value):
    """Detach a durable snapshot, including nested lists and native receipts."""
    return json.loads(json.dumps(value, separators=(",", ":"), allow_nan=False))


def _result(state, rows, at):
    terminal = state["terminal"] or {}
    return dict(week=state["week"], raid_key=state["raid_key"], guild_id=state["guild_id"],
        members=state["members"], bosses=state["bosses"],
        outcome=terminal.get("reason") or terminal.get("state") or "cleanup_complete",
        kills=sum(r["outcome"] == "killed" for r in rows), wipes=sum(r["outcome"] == "wiped" for r in rows), ts=at)


def _comparisons(state, rows, baselines):
    summaries = []
    for boss in state["bosses"]:
        observed = [r for r in rows if r["boss_id"] == boss]
        if not observed:
            continue
        guild = baselines[str(boss)]
        farm = is_farm([r["outcome"] for r in guild])
        durations = [(r["end_ms"] - r["start_ms"]) / 1000 for r in observed if r["outcome"] == "killed"]
        baseline = [(r["end_ms"] - r["start_ms"]) / 1000 for r in guild if r["outcome"] == "killed"]
        summaries.append(dict(boss_id=boss, boss=BOSS_NAMES[(state["map"], boss)],
            rival_kill_seconds=statistics.median(durations) if durations else None,
            rival_wipes=sum(r["outcome"] == "wiped" for r in observed),
            guild_farm_kill_seconds=statistics.median(baseline) if farm and baseline else None,
            pulls=[r["id"] for r in observed], baseline_pulls=[r["id"] for r in guild] if farm else []))
    return summaries


def _sealed_pull(row, state, *, baseline=False):
    """Validate frozen measured facts without consulting later mutable rows."""
    if (not isinstance(row, dict) or type(row.get("id")) is not str or not row["id"]
            or row["complete"] != 1 or row["scoreable"] != 1 or row["assisted"] != 0
            or row["outcome"] not in {"killed", "wiped"}
            or row["map"] != state["map"] or row["size"] != state["size"]
            or row["difficulty"] != _difficulty(state["size"], state["heroic"])
            or row["boss_id"] not in state["bosses"]
            or type(row["start_ms"]) is not int or type(row["end_ms"]) is not int
            or not 0 <= row["start_ms"] < row["end_ms"]):
        return False
    if baseline:
        return (row["kind"] in {"his", "offline"} and _finite(row["received"])
            and row["end_ms"] <= int(state["started"] * 1000) and 0 <= row["received"] <= state["started"])
    return (row["kind"] == "benchmark" and row["instance_id"] == state["instance"]
        and row["id"] in state["pulls"] and row["members"] == len(state["members"])
        and row.get("member_guids") == sorted(state["members"])
        and int(state["started"] * 1000) <= row["start_ms"]
        and row["end_ms"] <= state["proof"]["ts"])


def _valid_rival(row):
    return (isinstance(row, Rival) and _positive(row.guid) and type(row.class_) is int
        and row.class_ in _CLASSES and type(row.spec_tab) is int and row.spec_tab in range(3)
        and type(row.guild_id) is int and 0 <= row.guild_id <= 0xffffffff
        and type(row.team) is int and row.team in (0, 1) and type(row.ilvl) is int and 0 <= row.ilvl <= 400)


def _unique_rivals(rows):
    counts = Counter(r.guid for r in rows if isinstance(r, Rival) and _positive(r.guid))
    return [r for r in rows if _valid_rival(r) and counts[r.guid] == 1]


def pick_guild(rivals: Sequence[Rival], *, cast_guild: int,
               held: Callable[[int], bool] = lambda guid: False) -> tuple[int, list[Rival]]:
    """Choose a real other guild, then fill only from its faction's unguilded pool."""
    if not _positive(cast_guild):
        return 0, []
    rows = [r for r in _unique_rivals(rivals) if r.guild_id != cast_guild and not held(r.guid)]
    groups = {}
    for row in rows:
        if row.guild_id:
            groups.setdefault(row.guild_id, []).append(row)
    groups = {guild: members for guild, members in groups.items() if len({r.team for r in members}) == 1}
    if not groups:
        return 0, []
    guild = min(groups, key=lambda ident: (-len(groups[ident]), ident))
    members = groups[guild]
    team = members[0].team
    return guild, sorted(members + [r for r in rows if r.guild_id == 0 and r.team == team], key=lambda r: r.guid)


def rival_candidates(rivals) -> list[Candidate]:
    # Feral does not prove bear; use the installed talent-role rule without a guessed persona.
    return [Candidate(r.guid, r.name, r.class_, r.spec_tab, role_of(r.class_, r.spec_tab, None),
        role_of(r.class_, r.spec_tab, None) == "tank", "raider" if r.guild_id else "social", "", 0, 0, True)
        for r in sorted(_unique_rivals(rivals), key=lambda row: row.guid)]


def _difficulty(size, heroic):
    return (1 if size == 25 else 0) + (2 if heroic else 0)


def allowed_bosses(ports, raid_key: str) -> tuple[int, ...]:
    raid, size, heroic = split_key(raid_key)
    result = []
    now = ports.clock.now()
    for boss in sorted(b for m, b in BOSS_NAMES if m == raid.map_id and (m, b) not in NON_FIGHT_STEPS):
        try:
            answer = ports.benchmark_allowed(raid.map_id, boss, size, heroic)
        except Exception:
            break
        if answer is None:
            answer = ports.conn.execute("SELECT 1 FROM tg_pull WHERE map=? AND boss_id=? AND size=? "
                "AND difficulty=? AND kind IN ('his','offline') AND outcome='killed' AND complete=1 "
                "AND scoreable=1 AND assisted=0 AND end_ms>start_ms AND end_ms<=? AND received<=? LIMIT 1",
                (raid.map_id, boss, size, _difficulty(size, heroic), int(now * 1000), now)).fetchone() is not None
        if answer is not True:  # Neither truthy malformed values nor a Realm veto fall back to history.
            break
        result.append(boss)
    return tuple(result)


def percentiles(conn, guid) -> dict[str, int]:
    """No actual-DPS ranks can be supported by the currently recorded damage basis."""
    return {}


@dataclass(frozen=True, slots=True)
class BenchmarkSchedule:
    observed_at: float
    human_offline_since: float
    next_offline_window: float
    cast_guild: int
    guild_busy: bool
    lab_busy: bool
    opening: bool
    benchmark_busy: bool
    bodies_present: bool


def _window(clock, cfg):
    local = clock.local()
    if not isinstance(local, datetime) or local.tzinfo is None:
        return None
    start_h, start_m = map(int, cfg.benchmark_window[0].split(":"))
    end_h, end_m = map(int, cfg.benchmark_window[1].split(":"))
    start = local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end <= start:
        if local < end:
            start -= timedelta(days=1)
        else:
            end += timedelta(days=1)
    return local, start, end


class Benchmarks:
    REQUEST_SECONDS = 5.0
    FRESH_SECONDS = 15.0
    POLL_SECONDS = 30.0

    def __init__(self, ports, cfg, *, schedule_provider: Callable[[], BenchmarkSchedule | None] | None = None):
        self.ports, self.cfg, self.conn = ports, cfg, ports.conn
        self.schedule_provider = schedule_provider
        self._task = None
        self._closed = False
        self._next_poll = 0.0
        self._rivals_request = None
        self._corrupt = False
        self._state = self._read(_ACTIVE)
        outbox = self._read(_OUTBOX)
        self._outbox = [] if outbox is None else outbox
        if self._state is not None and not self._valid_state(self._state):
            self._corrupt = True
        if not isinstance(self._outbox, list) or len(self._outbox) > 40:
            self._corrupt = True
            self._outbox = []

    def _read(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0], parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (TypeError, ValueError):
            self._corrupt = True
            return None

    def _put(self, key, value):
        with self.conn:
            self._write(key, value)

    def _write(self, key, value):
        """Caller owns the transaction; never nest transaction-owning writes."""
        self.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, separators=(",", ":"), allow_nan=False)))

    def _save(self):
        self._put(_ACTIVE, self._state)

    @staticmethod
    def _valid_state(state):
        try:
            if (not isinstance(state, dict) or type(state["version"]) is not int or state["version"] != 1
                    or not _wire_id(state["command_id"]) or not state["command_id"].startswith("tg_bench_")
                    or state["owner"] != "benchmark:" + state["command_id"]
                    or not isinstance(state["week"], str) or not re.fullmatch(r"\d{4}-W\d{2}", state["week"])
                    or not isinstance(state["members"], list) or len(state["members"]) not in (10, 25)
                    or any(not _positive(g) for g in state["members"])
                    or len(set(state["members"])) != len(state["members"])
                    or any(not _positive(state[f]) for f in ("leader", "human", "guild_id", "cast_guild"))
                    or state["leader"] not in state["members"] or state["human"] in state["members"]
                    or state["guild_id"] == state["cast_guild"]
                    or any(type(state[f]) is not bool for f in ("dispatched", "bodies_present", "conflict", "heroic"))
                    or not _finite(state["started"]) or state["started"] < 0
                    or type(state["timeout"]) is not int or not 60 <= state["timeout"] <= 180
                    or type(state["map"]) is not int or type(state["size"]) is not int
                    or state["size"] != len(state["members"])
                    or not isinstance(state["bosses"], list) or not 1 <= len(state["bosses"]) <= 20
                    or any(type(b) is not int or (state["map"], b) not in BOSS_NAMES
                        or (state["map"], b) in NON_FIGHT_STEPS for b in state["bosses"])
                    or state["bosses"] != sorted(set(state["bosses"]))
                    or not isinstance(state["pulls"], list) or len(state["pulls"]) > _MAX_PULLS
                    or any(not isinstance(p, str) or not p or len(p) > 512 for p in state["pulls"])
                    or len(set(state["pulls"])) != len(state["pulls"])
                    or not isinstance(state["handle"], str)
                    or (state["handle"] and not _wire_id(state["handle"]))
                    or type(state["instance"]) is not int
                    or (not _positive(state["instance"]) if state["handle"] else state["instance"] != 0)):
                return False
            raid, size, heroic = split_key(state["raid_key"])
            if (raid.map_id, size, heroic) != (state["map"], state["size"], state["heroic"]):
                return False
            command = StartBenchmark(state["command_id"], state["map"], state["size"], state["leader"],
                tuple(g for g in sorted(state["members"]) if g != state["leader"]), tuple(state["bosses"]),
                heroic=state["heroic"], timeout_min=state["timeout"], min_members=state["size"])
            if state["command"] != json.loads(encode_command(command, int(state["started"] * 1000))):
                return False
            roster = [Rival(**row) for row in state["roster"]]
            if (len(roster) != state["size"] or len(_unique_rivals(roster)) != len(roster)
                    or {r.guid for r in roster} != set(state["members"])
                    or {r.guild_id for r in roster} - {0, state["guild_id"]}
                    or len({r.team for r in roster}) != 1):
                return False
            for name in ("last_job", "terminal"):
                row = state[name]
                if row is not None and (not isinstance(row, dict) or type(row.get("ts")) is not int
                        or row["ts"] < int(state["started"] * 1000) or row.get("id") != state["command_id"]
                        or row.get("kind") != "benchmark" or row.get("state") not in
                            ({"done", "failed"} if name == "terminal" else {"progress", "done", "failed"})
                        or not isinstance(row.get("reason"), str)):
                    return False
            if state["ack"] is not None and not isinstance(state["ack"], dict):
                return False
            if not state["dispatched"]:
                # Pre-send cleanup is permitted only without evidence of a
                # possible native admission. A changed flag cannot erase it.
                if (state["handle"] or state["instance"] or state["last_job"] is not None
                        or state["terminal"] is not None or state["binding"] is not None
                        or state["proof"] is not None or state.get("finalization") is not None
                        or state.get("query") is not None or state.get("query_receipt") is not None):
                    return False
                ack = state["ack"]
                if ack is not None and (ack.get("id") != state["command_id"]
                        or ack.get("ok") is not False or ack.get("error") not in _REFUSED):
                    return False
            binding = state["binding"]
            if binding is not None:
                bound = _receipt(binding | {"id": "binding", "ts": int(state["started"] * 1000), "state": "open"})
                if bound is None or not _proof_matches(state, bound) or _native_identity(bound) != binding:
                    return False
            if state["proof"] is not None:
                proof = _receipt(state["proof"])
                if proof is None or not _proof_matches(state, proof) or _native_identity(proof) != binding:
                    return False
                if proof.state == "complete" and (state.get("query", {}).get("id") != proof.id
                        or state.get("query_receipt") != state["proof"]):
                    return False
            if state.get("finalization") is not None and not Benchmarks._valid_finalization(state):
                return False
            return True
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
            return False

    @staticmethod
    def _valid_finalization(state):
        try:
            seal = state["finalization"]
            if (not isinstance(seal, dict) or set(seal) != {"version", "payload", "baselines", "memories"}
                    or type(seal["version"]) is not int or seal["version"] != 1):
                return False
            payload = seal["payload"]
            if not isinstance(payload, dict) or set(payload) != {"result", "comparisons", "receipt",
                    "admitted_pulls", "baselines", "provenance", "percentiles", "percentile_reason", "stopped_at_frontier"}:
                return False
            original = payload["receipt"]
            if (not isinstance(original, dict) or "finalization" in original
                    or not Benchmarks._valid_state(original) or not original["dispatched"]
                    or original["conflict"] or not _saved_complete(original)):
                return False
            for key in ("command_id", "owner", "week", "command", "raid_key", "map", "size", "heroic",
                        "guild_id", "cast_guild", "human", "leader", "members", "roster", "bosses",
                        "started", "timeout", "bodies_present", "handle", "instance", "binding", "proof"):
                if _copy(state[key]) != original[key]:
                    return False
            rows, baselines, result = payload["admitted_pulls"], seal["baselines"], payload["result"]
            if (payload["baselines"] != baselines or not isinstance(rows, list) or len(rows) > _MAX_PULLS
                    or any(not _sealed_pull(r, original) for r in rows)
                    or len({r["id"] for r in rows}) != len(rows)
                    or not isinstance(baselines, dict)
                    or set(baselines) != {str(r["boss_id"]) for r in rows}
                    or any(not isinstance(values, list) or any(not _sealed_pull(r, original, baseline=True)
                        or str(r["boss_id"]) != boss for r in values) for boss, values in baselines.items())
                    or not isinstance(result, dict) or not _finite(result.get("ts"))
                    or result["ts"] < original["started"] or result != _result(original, rows, result["ts"])):
                return False
            if (payload["comparisons"] != _comparisons(original, rows, baselines)
                    or payload["provenance"] != "complete_exact_native_scope_and_explicit_pull_receipts"
                    or payload["percentiles"] != {} or payload["percentile_reason"] != DPS_UNAVAILABLE
                    or type(payload["stopped_at_frontier"]) is not bool
                    or payload["stopped_at_frontier"] != (result["outcome"] == "frontier")):
                return False
            items = seal["memories"]
            text = Benchmarks._memory_text(original, payload["comparisons"], result["outcome"])
            if (not isinstance(items, list) or len(items) > 40 or not text and items
                    or any(not isinstance(item, dict) or set(item) != {"owner", "week", "text", "at", "evidence", "attempts"}
                        or not _positive(item["owner"]) or item["week"] != original["week"]
                        or item["text"] != text or type(item["at"]) is not int or item["at"] != int(result["ts"])
                        or item["evidence"] != ["event:tg:bench:" + original["command_id"]]
                        or type(item["attempts"]) is not int or item["attempts"] != 0 for item in items)
                    or len({item["owner"] for item in items}) != len(items)):
                return False
            return True
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
            return False

    @property
    def active(self):
        return self._corrupt or self._state is not None or self._task is not None and not self._task.done()

    def members(self) -> set[int]:
        # Keep any well-formed reservation manifest visible even when another
        # durable field is corrupt; corruption never grants ownership release.
        members = self._state.get("members") if isinstance(self._state, dict) else None
        return set(members) if (isinstance(members, list) and len(members) <= 40
            and all(_positive(g) for g in members)) else set()

    @property
    def next_due(self):
        window = _window(self.ports.clock, self.cfg)
        if window is None:
            return None
        local, start, end = window
        start += timedelta(days=(self.cfg.benchmark_weekday - start.weekday()) % 7)
        week = self._week(start)
        if start.date() == window[1].date() and local < end and self._read("tg:benchmark:week:" + week) is None:
            return max(start.timestamp(), local.timestamp())
        if start <= local or self._read("tg:benchmark:week:" + week) is not None:
            start += timedelta(days=7)
        return start.timestamp()

    @staticmethod
    def _week(start):
        year, week, _ = start.isocalendar()
        return f"{year:04d}-W{week:02d}"

    def _schedule(self):
        try:
            snapshot = self.schedule_provider() if self.schedule_provider else None
            now = self.ports.clock.now()
            if (not isinstance(snapshot, BenchmarkSchedule) or not _finite(now)
                    or not all(_finite(getattr(snapshot, f)) for f in
                        ("observed_at", "human_offline_since", "next_offline_window"))
                    or not 0 <= now - snapshot.observed_at <= self.FRESH_SECONDS
                    or snapshot.human_offline_since > now - 3600 or snapshot.human_offline_since < 0
                    or snapshot.next_offline_window <= now + 90 * 60
                    or not _positive(snapshot.cast_guild) or not _positive(self.ports.human_guid)
                    or any(type(getattr(snapshot, f)) is not bool for f in
                        ("guild_busy", "lab_busy", "opening", "benchmark_busy", "bodies_present"))
                    or any((snapshot.guild_busy, snapshot.lab_busy, snapshot.opening, snapshot.benchmark_busy))
                    or self.ports.human_online() is not False):
                return None
            window = _window(self.ports.clock, self.cfg)
            if window is None:
                return None
            local, start, end = window
            if start.weekday() != self.cfg.benchmark_weekday or not start <= local < end:
                return None
            return snapshot, self._week(start)
        except Exception:
            return None

    async def tick(self):
        """Schedule one owned bounded operation; never wait on the serial event lane."""
        if self._closed or self._corrupt or self._task is not None and not self._task.done():
            return
        now = self.ports.clock.now()
        if now < self._next_poll:
            return
        self._next_poll = now + self.POLL_SECONDS
        self._task = asyncio.create_task(self._step(), name="training-benchmark")

    async def _step(self):
        try:
            if self._state is not None:
                if not self._state["dispatched"]:
                    self._release_presend()
                else:
                    await self._reconcile()
            elif self._outbox:
                await self._memory()
            elif self.cfg.enabled and self.cfg.benchmark:
                ready = self._schedule()
                if ready and self._read("tg:benchmark:week:" + ready[1]) is None:
                    await self._start(*ready)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The durable dispatch receipt still owns every uncertain body.
            log.exception("benchmark_operation_failed")

    def _week_result(self, week, reason):
        self._put("tg:benchmark:week:" + week, {"status": reason, "ts": self.ports.clock.now()})
        log.info("benchmark_skipped: %s", reason)

    async def _start(self, snapshot, week):
        self._week_result(week, "acquiring")  # A crash/replay cannot start a second weekly attempt.
        raid_key, bosses = None, ()
        for key in self.cfg.benchmark_raids:
            allowed = allowed_bosses(self.ports, key)
            if allowed:
                raid_key, bosses = key, allowed
                break
        if raid_key is None:
            self._week_result(week, "frontier")
            return
        request = GetRivals("tg_rivals_" + uuid4().hex, level=80, max_=400)
        self._rivals_request = (request.id, week)
        self._put("tg:benchmark:rivals:" + week, {"id": request.id, "requested": self.ports.clock.now()})
        try:
            async with asyncio.timeout(self.REQUEST_SECONDS):
                rows = await self.ports.request(request, "rivals", until=lambda ev: True, timeout_s=self.REQUEST_SECONDS)
        except (TimeoutError, ConnectionError, OSError):
            self._week_result(week, "rivals_unavailable")
            return
        finally:
            self._rivals_request = None
        if (len(rows) != 1 or not isinstance(rows[0], Rivals) or rows[0].id != request.id
                or not 1 <= len(rows[0].members) <= 400):
            self._week_result(week, "rivals_unavailable")
            return
        response = rows[0]
        self._put("tg:benchmark:rivals:" + week, asdict(response))
        now = self.ports.clock.now()
        if type(response.ts) is not int or not 0 <= now * 1000 - response.ts <= self.FRESH_SECONDS * 1000:
            self._week_result(week, "rivals_stale")
            return
        fresh = self._schedule()
        if fresh is None or fresh[1] != week or fresh[0].cast_guild != snapshot.cast_guild:
            self._week_result(week, "schedule_changed")
            return
        snapshot = fresh[0]
        cast, away = self.ports.cast(), self.ports.away()
        rivals = [r for r in response.members if isinstance(r, Rival)
            and r.guid not in cast and r.guid != self.ports.human_guid and r.guid not in away]
        guild, pool = pick_guild(rivals, cast_guild=snapshot.cast_guild,
            held=lambda g: self.ports.holder(g) is not None)
        raid, size, heroic = split_key(raid_key)
        try:
            comp = select_comp(size, rival_candidates(pool), must_include=[], human_in_raid=False,
                seed=int(week[:4]) * 100 + int(week[6:]))
        except (NoTankError, ValueError):
            self._week_result(week, "no_tank")
            return
        chosen = {r.guid: r for r in pool if r.guid in comp.members}
        if not guild or len(chosen) != size:
            self._week_result(week, "roster")
            return
        if statistics.fmean(r.ilvl for r in chosen.values()) < self.cfg.benchmark_min_ilvl:
            self._week_result(week, "gear")
            return
        timeout = min(180, int((snapshot.next_offline_window - now) // 60) - 10)
        if timeout < 60:
            self._week_result(week, "window")
            return
        leader = min(g for g in chosen if comp.roles[g] == "tank")
        command = StartBenchmark("tg_bench_" + uuid4().hex, raid.map_id, size, leader,
            tuple(g for g in sorted(chosen) if g != leader), bosses, heroic=heroic,
            timeout_min=timeout, min_members=size)
        self._state = dict(version=1, week=week, command_id=command.id, owner="benchmark:" + command.id,
            raid_key=raid_key, map=raid.map_id, size=size, heroic=heroic, guild_id=guild,
            cast_guild=snapshot.cast_guild, human=self.ports.human_guid, leader=leader,
            members=sorted(chosen), roster=[asdict(chosen[g]) for g in sorted(chosen)], bosses=list(bosses),
            started=now, timeout=timeout, bodies_present=snapshot.bodies_present, dispatched=False,
            command=json.loads(encode_command(command, int(now * 1000))), handle="", instance=0,
            binding=None, terminal=None, last_job=None, conflict=False, pulls=[], proof=None, ack=None)
        self._save()  # Claim/dispatch intent precedes either side effect.
        granted = self.ports.claim(self._state["owner"], chosen, until=now + timeout * 60 + 600)
        self._state["granted"] = sorted(granted)
        self._save()
        fresh = self._schedule()
        if (not self.cfg.enabled or not self.cfg.benchmark or set(granted) != set(chosen)
                or fresh is None or fresh[1] != week
                or fresh[0].cast_guild != snapshot.cast_guild
                or fresh[0].bodies_present != snapshot.bodies_present
                or self.ports.clock.now() + (timeout + 10) * 60 > fresh[0].next_offline_window
                or self.ports.clock.now() * 1000 - response.ts > self.FRESH_SECONDS * 1000
                or self.ports.human_guid != self._state["human"]
                or set(chosen) & (set(self.ports.cast()) | set(self.ports.away()))
                or not self._owns() or allowed_bosses(self.ports, raid_key)[:len(bosses)] != bosses):
            self._week_result(week, "claim_or_schedule_changed")
            self._release_presend()
            return
        self._state["dispatched"] = True
        self._save()
        try:
            async with asyncio.timeout(self.REQUEST_SECONDS):
                ack = await self.ports.send(command)
        except (TimeoutError, ConnectionError, OSError):
            self._state["ack"] = {"status": "unknown"}
            self._save()
            return
        self._state["ack"] = asdict(ack) if isinstance(ack, Ack) else {"status": "invalid"}
        self._save()
        if (isinstance(ack, Ack) and ack.id == command.id and ack.ok is False and ack.error in _REFUSED
                and not self._state["last_job"] and not self._state["handle"]):
            self._state["dispatched"] = False
            self._save()
            self._week_result(week, "refused:" + ack.error)
            self._release_presend()
        else:
            self._put("tg:benchmark:week:" + week, {"status": "started_or_uncertain", "id": command.id})
            log.info("benchmark_started guild=%s raid=%s bosses=%s", guild, raid_key, bosses)
        self._next_poll = 0

    def _owns(self):
        state = self._state
        return all(self.ports.holder(g) == (state["owner"] if state["bodies_present"] else None)
                   for g in state["members"])

    def _release_owned(self):
        state = self._state
        owned = [g for g in state["members"] if self.ports.holder(g) == state["owner"]]
        if owned:
            self.ports.release(state["owner"], owned)
        if any(self.ports.holder(g) == state["owner"] for g in state["members"]):
            raise RuntimeError("benchmark body release unconfirmed")

    def _release_presend(self):
        self._release_owned()
        self._state = None
        self._save()

    def feed_rivals(self, ev):
        """Keep a bounded early response; ports.request owns ACK/collection waits."""
        request = self._rivals_request
        if not self._corrupt and request and isinstance(ev, Rivals) and ev.id == request[0] and len(ev.members) <= 400:
            self._put("tg:benchmark:rivals:" + request[1], asdict(ev))

    async def on_rivals(self, ev):
        self.feed_rivals(ev)

    def feed_job(self, ev):
        """Persist correlated receipts, including during shutdown; never send or wait."""
        state = self._state
        if (self._corrupt or state is None or not state["dispatched"] or not isinstance(ev, Job)
                or ev.id != state["command_id"] or ev.kind != "benchmark"
                or ev.state not in {"progress", "done", "failed"}
                or len(ev.guids) > 40 or len(set(ev.guids)) != len(ev.guids)
                or any(not _positive(g) for g in ev.guids)
                or type(ev.ts) is not int or ev.ts < int(state["started"] * 1000)
                or ev.ts > int(self.ports.clock.now() * 1000) + 15000):
            return
        if state["last_job"] and ev.ts < state["last_job"]["ts"]:
            return
        handle = ev.training_run
        if handle:
            if (set(ev.guids) != set(state["members"]) or len(ev.guids) != len(state["members"])
                    or not _positive(ev.instance_id)):
                return
            if state["handle"] and (state["handle"] != handle or state["instance"] != ev.instance_id):
                state["conflict"] = True
            else:
                state["handle"], state["instance"] = handle, ev.instance_id
        elif state["handle"]:
            return
        state["last_job"] = asdict(ev)
        if ev.state in {"done", "failed"}:
            state["terminal"] = asdict(ev)
        self._save()
        self._next_poll = 0

    async def on_job(self, ev):
        self.feed_job(ev)

    async def on_pull(self, ev):
        """Admit a specific stored receipt while the native scope is known; never adopt a recent SQL suffix."""
        state = self._state
        if (self._corrupt or state is None or state["conflict"] or state.get("finalization") is not None
                or not state["handle"] or not isinstance(ev, Pull)
                or ev.source != "benchmark" or ev.map != state["map"] or ev.instance_id != state["instance"]
                or ev.size != state["size"] or ev.difficulty != _difficulty(state["size"], state["heroic"])
                or ev.boss_id not in state["bosses"] or ev.start_ms < int(state["started"] * 1000)
                or ev.end_ms <= ev.start_ms or ev.end_ms > int(self.ports.clock.now() * 1000)
                or ev.start_ms > int((state["started"] + state["timeout"] * 60) * 1000)):
            return
        row = self.conn.execute("SELECT * FROM tg_pull WHERE id=?", (ev.id,)).fetchone()
        if (row is None or not self._pull_matches(dict(row), state)
                or any(row[key] != getattr(ev, key) for key in
                    ("map", "instance_id", "boss_id", "size", "difficulty", "start_ms", "end_ms", "outcome", "members"))):
            return
        if ev.id not in state["pulls"] and len(state["pulls"]) < _MAX_PULLS:
            state["pulls"].append(ev.id)
            self._save()

    def _pull_matches(self, row, state):
        members = {r[0] for r in self.conn.execute("SELECT guid FROM tg_member WHERE pull=?", (row["id"],))}
        return (row["kind"] == "benchmark" and row["complete"] == row["scoreable"] == 1
            and row["assisted"] == 0 and row["outcome"] in {"killed", "wiped"}
            and row["map"] == state["map"] and row["instance_id"] == state["instance"]
            and row["size"] == state["size"] and row["difficulty"] == _difficulty(state["size"], state["heroic"])
            and row["boss_id"] in state["bosses"] and members == set(state["members"])
            and row["members"] == len(members) and int(state["started"] * 1000) <= row["start_ms"] < row["end_ms"]
            and row["end_ms"] <= int(self.ports.clock.now() * 1000))

    async def _reconcile(self):
        state = self._state
        if self._corrupt or state is None:
            return
        # Complete is a permanent native seal. Resume local finalization from
        # its accepted durable receipt even after cache eviction/server restart.
        if not state["conflict"] and _saved_complete(state):
            self._finish()
            return
        # Renew only our exact surviving holder; a preempted body is never reclaimed.
        owned = [g for g in state["members"] if self.ports.holder(g) == state["owner"]]
        if owned:
            self.ports.claim(state["owner"], owned, until=self.ports.clock.now() + 600)
        if state["conflict"] or not state["handle"]:
            return
        query = GetTrainingCleanup("tg_cleanup_" + uuid4().hex, state["handle"])
        identity = (state["command_id"], state["handle"], state["instance"])
        state["query"] = {"id": query.id, "at": self.ports.clock.now()}
        self._save()
        async with asyncio.timeout(self.REQUEST_SECONDS):
            rows = await self.ports.request(query, "training_cleanup", until=lambda ev: True,
                timeout_s=self.REQUEST_SECONDS)
        # The serial receipt lane continues while the independent query waits.
        # A contradictory Job permanently poisons this ownership; an older
        # correlated response must not override that newer information.
        if (self._corrupt or self._state is not state or state["conflict"]
                or identity != (state["command_id"], state["handle"], state["instance"])):
            return
        if len(rows) != 1 or not isinstance(rows[0], TrainingCleanup):
            return
        proof = rows[0]
        state["query_receipt"] = asdict(proof)
        self._save()
        if (proof.id != query.id or not _proof_matches(state, proof)
                or proof.ts > int(self.ports.clock.now() * 1000) + 15000):
            return
        immutable = _native_identity(proof)
        if state["binding"] is not None and state["binding"] != immutable:
            state["conflict"] = True
            self._save()
            return
        state["binding"] = immutable
        state["proof"] = asdict(proof)
        self._save()
        if proof.state == "complete":
            self._finish()

    def _finish(self):
        state = self._state
        if self._corrupt or state is None or state["conflict"] or not _saved_complete(state):
            return
        if state.get("finalization") is None:
            self._freeze_finalization()
        if not self._valid_finalization(state):
            self._corrupt = True
            return
        seal = state["finalization"]
        self._publish_finalization(seal)
        self._release_owned()  # Only the exact surviving holder, after durable proof and result.
        self._put("tg:benchmark:week:" + state["week"], {"status": "complete", "id": state["command_id"]})
        self._state = None
        self._save()

    def _freeze_finalization(self):
        """Seal one exact result before publication, memory queuing or handback."""
        state = self._state
        rows = [dict(row) for ident in state["pulls"]
            if (row := self.conn.execute("SELECT * FROM tg_pull WHERE id=?", (ident,)).fetchone()) is not None]
        for row in rows:
            row["member_guids"] = [r[0] for r in self.conn.execute(
                "SELECT guid FROM tg_member WHERE pull=? ORDER BY guid", (row["id"],))]
        rows = [row for row in rows if self._pull_matches(row, state) and _sealed_pull(row, state)]
        baselines = {}
        for boss in state["bosses"]:
            if not any(r["boss_id"] == boss for r in rows):
                continue
            guild = [dict(r) for r in self.conn.execute("SELECT * FROM tg_pull WHERE map=? AND boss_id=? "
                "AND size=? AND difficulty=? AND kind IN ('his','offline') AND complete=1 AND scoreable=1 "
                "AND assisted=0 AND outcome IN ('killed','wiped') AND end_ms>start_ms AND end_ms<=? AND received<=? "
                "ORDER BY start_ms,id", (state["map"], boss, state["size"],
                    _difficulty(state["size"], state["heroic"]), int(state["started"] * 1000), state["started"]))]
            baselines[str(boss)] = [r for r in guild if _sealed_pull(r, state, baseline=True)]
        result = _result(state, rows, self.ports.clock.now())
        summaries = _comparisons(state, rows, baselines)
        payload = dict(result=result, comparisons=summaries,
            provenance="complete_exact_native_scope_and_explicit_pull_receipts",
            receipt={key: value for key, value in state.items() if key != "finalization"},
            admitted_pulls=rows, baselines=baselines, percentiles={}, percentile_reason=DPS_UNAVAILABLE,
            stopped_at_frontier=result["outcome"] == "frontier")
        seal = _copy(dict(version=1, payload=payload, baselines=baselines,
            memories=self._memory_items(state, summaries, result["outcome"], result["ts"])))
        state["finalization"] = seal
        self._save()

    def _publish_finalization(self, seal):
        """Publish the first immutable result and its outbox in one transaction."""
        payload, items = seal["payload"], seal["memories"]
        result = payload["result"]
        result_key = "tg:benchmark:result:" + result["week"]
        memory_key = "tg:benchmark:memory_receipt:" + result["week"]
        queued = False
        with self.conn:
            columns = "week raid_key guild_id members bosses outcome kills wipes ts".split()
            self.conn.execute("INSERT INTO tg_benchmark(" + ",".join(columns) + ") VALUES (" +
                ",".join("?" for _ in columns) + ") ON CONFLICT(week) DO NOTHING",
                [json.dumps(result[k]) if k in {"members", "bosses"} else result[k] for k in columns])
            row = self.conn.execute("SELECT * FROM tg_benchmark WHERE week=?", (result["week"],)).fetchone()
            recorded = {k: json.loads(row[k]) if k in {"members", "bosses"} else row[k] for k in columns}
            if recorded != result:
                raise RuntimeError("benchmark final result conflicts with its frozen receipt")
            previous = self._read(result_key)
            if self._corrupt or previous is not None and previous != payload:
                raise RuntimeError("benchmark result metadata conflicts with its frozen receipt")
            self._write(result_key, payload)
            memory = self._read(memory_key)
            if self._corrupt:
                raise RuntimeError("benchmark memory receipt is corrupt")
            if memory is None:
                self._write(_OUTBOX, items)
                text = self._memory_text(payload["receipt"], payload["comparisons"], result["outcome"])
                self._write(memory_key, {"status": "queued", "recipients": [item["owner"] for item in items]}
                    if text else {"status": "no_comparable_receipts"})
                queued = True
        if queued:
            self._outbox = _copy(items)

    @staticmethod
    def _memory_text(state, summaries, outcome):
        text = ""
        if summaries:
            row = summaries[0]
            if row["rival_kill_seconds"] is not None:
                text = f"they killed {row['boss']} in {Benchmarks._duration(row['rival_kill_seconds'])}"
                if row["guild_farm_kill_seconds"] is not None:
                    text += f", our farm median is {Benchmarks._duration(row['guild_farm_kill_seconds'])}"
            elif row["rival_wipes"]:
                text = f"we recorded {row['rival_wipes']} rival wipes on {row['boss']}"
        if outcome == "frontier":
            text = "stopped at the guild's confirmed frontier" + (f"; {text}" if text else "")
        return f"Rival run on {state['raid_key']}: {text}." if text else ""

    def _memory_items(self, state, summaries, outcome, at):
        text = self._memory_text(state, summaries, outcome)
        if not text:
            return []
        recipients = set()
        cast = self.ports.cast()
        lead = self.ports.standing_lead()
        if lead in cast:
            recipients.add(lead)
        for guid in cast:
            persona = self.ports.persona(guid)
            if persona is not None and persona.play.guild_role == "officer":
                recipients.add(guid)
        return [dict(owner=g, week=state["week"], text=text,
            at=int(at), evidence=["event:tg:bench:" + state["command_id"]], attempts=0)
            for g in sorted(recipients)[:40]]

    @staticmethod
    def _duration(seconds):
        value = round(seconds)
        return f"{value // 60}:{value % 60:02d}"

    async def _memory(self):
        item = self._outbox[0]
        candidate = MemoryCandidate(item["owner"], "event", item["text"], item["at"], (), 4, 0.0,
            "game", tuple(item["evidence"]), key="tg:bench:" + item["week"])
        item["attempts"] += 1
        self._put(_OUTBOX, self._outbox)
        result = None
        try:
            async with asyncio.timeout(self.REQUEST_SECONDS):
                result = await self.ports.remember(candidate)
        except (TimeoutError, ConnectionError, OSError):
            pass
        accepted = getattr(result, "action", None) in {"inserted", "merged", "updated"} and getattr(result, "id", None) is not None
        if accepted or item["attempts"] >= 3:
            self._put(f"tg:benchmark:memory:{item['week']}:{item['owner']}",
                {"accepted": accepted, "attempts": item["attempts"], "action": getattr(result, "action", "unconfirmed")})
            self._outbox.pop(0)
        self._put(_OUTBOX, self._outbox)

    async def close(self):
        """Stop owned tasks; unresolved dispatched runs remain durable and reserved."""
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def disconnect(self):
        # Transport epoch is never native cleanup proof.
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._next_poll = 0
