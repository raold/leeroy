"""The race for firsts: where each rival guild stands on every raid boss, always behind the guild.

A rival's realm_progress row (guild, map, boss_id, difficulty) moves by three sources only:

- **canon** (seed_canon): the guild's history (realm_canon_kill) is its first kill of each canon
  boss, on the canon day, never a live kill. Progression holds each some days after it; drama
  all but the newest; gankers only those more than four weeks older than the newest canon day.
- **the weekly rules** (weekly, once per product week), for bosses outside the canon only:
  progression kills a boss some days after the guild's own first live kill and otherwise edges
  closer (best_pct down to 2); drama stalls behind the guild's newest kill (best_pct 30 or
  more); gankers take at most one boss a week that the guild killed more than four weeks ago.
  Each rule kill is a public forum fact (race_rule_kill_v1).
- **real benchmarks** (on_benchmark): a training-ground benchmark run by a Realm guild (Training
  still picks the guild itself). A kill faster than the guild's best is a loss with a story:
  realm_match, a public fact (benchmark_loss_v1), a grudge on the raiders it beat and a boast. A
  pull on a boss the guild has never really killed is only logged (realm_benchmark_ahead).

The rules never set a kill before the guild's own live kill, and nothing ever kills an Ulduar
hard mode (owner ruling): the guild's own hard modes cannot be observed, so rivals only make
progress on them. Ulduar has no heroic difficulty, so its hard modes take the heroic slots:
(603, boss, 2) is a boss's 10-man hard mode and (603, boss, 3) its 25-man one.

benchmark_allowed is the training ground's question (contract note TG1): True for a real guild
kill at exactly that size and difficulty (realm_kill, public or private, or Training's own
measured pulls of his or an offline raid), otherwise None, so Training falls back to its own
history. Never False, never raised, and never from the canon: benchmarks stay real.
"""
from __future__ import annotations

from datetime import date, datetime, time
import hashlib
import json
import logging
import random
from zoneinfo import ZoneInfo

from leeroy_mind.guild.raids import CATALOG
from leeroy_mind.wow import BOSS_NAMES, is_raid_fight

from .calendar_time import TIMEZONE, product_period
from .ports import BenchmarkPull
from .records import FactRow, Rejected
from .rule_facts import put_rule_fact_in_unit
from .store import canon_kills

log = logging.getLogger(__name__)

DAY_S = 86400.0
TICK_S = 60
BENCHMARK_TICK_S = 600
BENCHMARK_WINDOW_S = 14 * DAY_S
WEEK_KEY = "realm:race:week"
ULDUAR = 603
# Ulduar's bosses with a hard mode the race can stake. Yogg-Saron's (12) no-Keepers mode is canon
# (Death's Demise), and the easier Keeper counts with it; Algalon (13) has none of his own.
HARD_MODE_BOSSES = frozenset({0, 3, 4, 7, 8, 9, 10, 11})
_UNOBSERVED = frozenset({624})  # Vault of Archavon reports no boss states: nobody's kill is seen.
LAGS = {"progression": (3, 10), "drama": (7, 21), "gankers": (14, 28)}  # days behind the guild
FLOORS = {"progression": 2, "drama": 30}  # the lowest best_pct each comes to by rule
STEP = (5, 15)  # best_pct points a week
CANON_DRAMA_PCT = (30, 60)
GANKER_AGE_S = 28 * DAY_S
_U32 = 0xFFFFFFFF
# The guild's own measured kills on the training ground's ledger, as Training reads them.
_TG_KILLS = ("kind IN ('his','offline') AND outcome='killed' AND complete=1 AND scoreable=1 "
             "AND assisted=0 AND end_ms>start_ms AND size=CASE WHEN difficulty IN (1,3) THEN 25 ELSE 10 END")
_REALM_KILLS = ("difficulty BETWEEN 0 AND 3 AND size=CASE WHEN difficulty IN (1,3) THEN 25 ELSE 10 END "
                "AND heroic=(difficulty>=2)")


def difficulty(size: int, heroic: bool) -> int:
    return int(size == 25) + 2 * int(heroic)


def is_hard_mode(map_id: int, difficulty: int) -> bool:
    return map_id == ULDUAR and difficulty in (2, 3)


def mode_label(map_id: int, difficulty: int) -> str:
    size = "25-man" if difficulty & 1 else "10-man"
    if is_hard_mode(map_id, difficulty):
        return f"{size} hard mode"
    return f"{size} heroic" if difficulty & 2 else size


def _raid_key(map_id, boss, difficulty) -> bool:
    return (type(map_id) is int and type(boss) is int and type(difficulty) is int
            and (map_id, boss) in BOSS_NAMES and is_raid_fight(map_id, boss) and 0 <= difficulty <= 3)


def race_keys() -> frozenset[tuple[int, int, int]]:
    """Every (map, boss, difficulty) the rivals can race on: each raid fight at each mode its
    raid has, and the Ulduar hard modes."""
    keys = set()
    for raid in CATALOG.values():
        if raid.map_id in _UNOBSERVED:
            continue
        modes = [difficulty(size, heroic) for size in raid.sizes
                 for heroic in ((False, True) if raid.heroic else (False,))]
        for map_id, boss in BOSS_NAMES:
            if map_id == raid.map_id and is_raid_fight(map_id, boss):
                keys.update((map_id, boss, mode) for mode in modes)
    keys.update((ULDUAR, boss, mode) for boss in HARD_MODE_BOSSES for mode in (2, 3))
    return frozenset(keys)


RACE_KEYS = race_keys()


def _canon_day(day) -> float | None:
    """The canon day's start, local to the product calendar."""
    try:
        return datetime.combine(date.fromisoformat(day), time(0), tzinfo=ZoneInfo(TIMEZONE)).timestamp()
    except (TypeError, ValueError, OverflowError, KeyError):  # ZoneInfoNotFoundError is a KeyError.
        return None


def _killed(row) -> bool:
    return row is not None and row["state"] == "killed"


class Race:
    """The race part: canon seeding, the weekly rules, benchmarks and Training's question."""

    name = "race"

    def __init__(self, realm, *, salt: str = "race_v1"):
        self.realm = realm
        self._salt = salt
        self._next = 0.0
        self._next_benchmarks = 0.0

    # -- the realm it runs in ---------------------------------------------------------------

    @property
    def _store(self):
        return self.realm.app.store

    def _now(self) -> float:
        return self.realm.app.clock.now()

    def _context(self):
        """The live context (enabled, a valid hello naming his guild and him), or None."""
        if self.realm.enabled is not True:
            return None
        ctx = self.realm.action_context()
        if (ctx.enabled is not True or ctx.connected is not True or ctx.presence_known is not True
                or type(ctx.guild) is not int or not 0 < ctx.guild <= _U32
                or type(ctx.human) is not int or ctx.human <= 0):
            return None
        return ctx

    def _rng(self, *parts) -> random.Random:
        """Seeded per boss (and per week where it moves weekly), so a rerun says the same."""
        seed = hashlib.sha256(":".join(str(p) for p in (self._salt, *parts)).encode("utf-8")).digest()
        return random.Random(int.from_bytes(seed[:8], "big"))

    def _lag(self, guild, key, role) -> float:
        low, high = LAGS[role]
        return self._rng(guild, *key, role, "lag").uniform(low, high) * DAY_S

    # -- reads ------------------------------------------------------------------------------

    @staticmethod
    def _really_killed(conn, key, until_ms) -> bool:
        """A real guild kill of exactly this mode by until_ms: native's or a measured pull."""
        map_id, boss, mode = key
        if conn.execute(f"SELECT 1 FROM realm_kill WHERE {_REALM_KILLS} AND map=? AND boss_id=? "
                        "AND difficulty=? AND killed_ms<=? LIMIT 1", (map_id, boss, mode, until_ms)).fetchone():
            return True
        return conn.execute(f"SELECT 1 FROM tg_pull WHERE {_TG_KILLS} AND map=? AND boss_id=? AND difficulty=? "
                            "AND end_ms<=? AND received<=? LIMIT 1",
                            (map_id, boss, mode, until_ms, until_ms / 1000)).fetchone() is not None

    @staticmethod
    def _live_kills(conn, until_ms) -> dict:
        """The guild's first real kill of each mode, in seconds."""
        kills = {}
        rows = [*conn.execute(f"SELECT map,boss_id,difficulty,MIN(killed_ms) FROM realm_kill WHERE {_REALM_KILLS} "
                              "AND killed_ms<=? GROUP BY map,boss_id,difficulty", (until_ms,)),
                *conn.execute(f"SELECT map,boss_id,difficulty,MIN(end_ms) FROM tg_pull WHERE {_TG_KILLS} "
                              "AND end_ms<=? AND received<=? GROUP BY map,boss_id,difficulty",
                              (until_ms, until_ms / 1000))]
        for map_id, boss, mode, ms in rows:
            key = (map_id, boss, mode)
            if _raid_key(*key) and not is_hard_mode(map_id, mode) and type(ms) is int:
                kills[key] = min(kills.get(key, ms / 1000), ms / 1000)
        return kills

    @staticmethod
    def _canon(conn, kills=None) -> dict:
        """Each canon key's canon day (its earliest), in seconds."""
        canon = {}
        for kill in canon_kills(conn) if kills is None else kills:
            key = tuple(getattr(kill, name, None) for name in ("map", "boss_id", "difficulty"))
            day = _canon_day(getattr(kill, "day", None))
            if not _raid_key(*key) or day is None:
                log.warning("realm_race_canon_invalid")
                continue
            canon[key] = min(canon.get(key, day), day)
        return canon

    @staticmethod
    def _rivals(conn) -> list[tuple[int, str, str]]:
        return [(row[0], row[1], row[2]) for row in conn.execute(
            "SELECT id,role,name FROM realm_guild WHERE status='active' ORDER BY id")
            if type(row[0]) is int and 0 < row[0] <= _U32 and row[1] in LAGS and type(row[2]) is str]

    @staticmethod
    def _rows(conn, guild) -> dict:
        return {(row["map"], row["boss_id"], row["difficulty"]): row for row in conn.execute(
            "SELECT * FROM realm_progress WHERE guild=?", (guild,))}

    @staticmethod
    def _put(conn, guild, key, *, state, best_pct, killed_at, kill_s, source, now):
        conn.execute(
            "INSERT INTO realm_progress(guild,map,boss_id,difficulty,state,best_pct,killed_at,kill_s,source,updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(guild,map,boss_id,difficulty) DO UPDATE SET "
            "state=excluded.state,best_pct=excluded.best_pct,killed_at=excluded.killed_at,"
            "kill_s=excluded.kill_s,source=excluded.source,updated=excluded.updated",
            (guild, *key, state, best_pct, killed_at, kill_s, source, now))

    # -- the training ground's question -----------------------------------------------------

    def benchmark_allowed(self, map_id, boss_id, size, heroic) -> bool | None:
        """True for a real guild kill at this size and difficulty, otherwise None. Never False
        and never raised: Training stops its boss list at the first answer that is not True,
        and falls back to its own history on None. The canon never counts."""
        try:
            if (self.realm.enabled is not True or type(map_id) is not int or type(boss_id) is not int
                    or type(size) is not int or size not in (10, 25) or type(heroic) is not bool
                    or not _raid_key(map_id, boss_id, 0)):
                return None
            until_ms = int(self._now() * 1000)
            key = (map_id, boss_id, difficulty(size, heroic))
            return True if self._really_killed(self._store.conn, key, until_ms) else None
        except Exception:
            return None

    # -- canon ------------------------------------------------------------------------------

    def seed_canon(self, kills=None) -> int:
        """Seed every active rival's canon rows from realm_canon_kill (or the given kills);
        returns how many rows changed. Idempotent: run again it changes nothing. Called from the
        canon step's apply and again by every weekly run, so a rival added later is seeded too.
        Nothing is written while the Realm is disabled."""
        if self.realm.enabled is not True:
            return 0
        store = self._store
        with store.transaction():
            return self._seed_canon(store.conn, kills, self._now())

    def _seed_canon(self, conn, kills, now) -> int:
        canon = {key: day for key, day in self._canon(conn, kills).items()
                 if not is_hard_mode(key[0], key[2]) and day <= now}
        if not canon:
            return 0
        ordered = sorted(canon, key=lambda key: (canon[key], key))
        newest = ordered[-1]
        changed = 0
        for guild, role, _ in self._rivals(conn):
            rows = self._rows(conn, guild)
            for key in ordered:
                day, row = canon[key], rows.get(key)
                if role == "drama" and key == newest:
                    pct = self._rng(guild, *key, "canon_pct").randint(*CANON_DRAMA_PCT)
                    changed += self._canon_progress(conn, guild, key, row, pct, now)
                elif role != "gankers" or canon[newest] - day > GANKER_AGE_S:
                    killed_at = day + self._lag(guild, key, role)
                    if killed_at <= now:  # Otherwise not yet: a later run seeds it.
                        changed += self._canon_kill(conn, guild, key, row, killed_at, now)
        return changed

    def _canon_kill(self, conn, guild, key, row, killed_at, now) -> int:
        if (_killed(row) and row["source"] == "canon" and row["killed_at"] == killed_at
                and row["best_pct"] == 0):
            return 0
        self._put(conn, guild, key, state="killed", best_pct=0, killed_at=killed_at,
                  kill_s=row["kill_s"] if row is not None else None, source="canon", now=now)
        return 1

    def _canon_progress(self, conn, guild, key, row, pct, now) -> int:
        if _killed(row):
            return 0  # A kill is never taken back.
        if row is not None and type(row["best_pct"]) is int:
            pct = max(FLOORS["drama"], min(row["best_pct"], pct))
            if row["source"] == "canon" and row["state"] == "progressing" and row["best_pct"] == pct:
                return 0
        self._put(conn, guild, key, state="progressing", best_pct=pct, killed_at=None,
                  kill_s=row["kill_s"] if row is not None else None, source="canon", now=now)
        return 1

    # -- the weekly rules -------------------------------------------------------------------

    def weekly(self, now) -> bool:
        """This product week's rules, once (meta realm:race:week), with the canon seeded first.
        False when it has already run this week, there are no rivals, or the Realm is not live."""
        ctx = self._context()
        if ctx is None:
            return False
        period = product_period(now)
        store = self._store
        if store.get_meta(WEEK_KEY) == period.key:
            return False
        human = self.realm.ports.human()
        with store.transaction():
            conn = store.conn
            rivals = self._rivals(conn)
            if not rivals or store.get_meta(WEEK_KEY) == period.key:
                return False
            self._seed_canon(conn, None, now)
            canon = self._canon(conn)
            live = self._live_kills(conn, int(now * 1000))
            kills = {**live, **canon}  # The canon day is the guild's first kill of a canon boss.
            ordered = sorted((key for key in kills if not is_hard_mode(key[0], key[2])),
                             key=lambda key: (kills[key], key))
            behind = set(ordered[:-1])  # Drama never passes the guild's newest kill.
            keys = sorted((RACE_KEYS | set(live)) - set(canon))
            for guild, role, name in rivals:
                rows = self._rows(conn, guild)
                open_keys = [key for key in keys if not _killed(rows.get(key))
                             and (rows.get(key) is None or rows[key]["source"] != "canon")]
                if role == "gankers":
                    old = [key for key in open_keys if key in live and now - live[key] > GANKER_AGE_S]
                    if old:
                        key = min(old, key=lambda key: (live[key], key))
                        self._rule_kill(conn, ctx, human, rivals, guild, role, name, key, rows.get(key),
                                        live[key], now, now)
                    continue
                for key in open_keys:
                    row = rows.get(key)
                    if key in live and (role == "progression" or key in behind):
                        killed_at = live[key] + self._lag(guild, key, role)
                        if killed_at <= now:
                            self._rule_kill(conn, ctx, human, rivals, guild, role, name, key, row,
                                            live[key], killed_at, now)
                            continue
                    self._step(conn, guild, role, key, row, period.key, now)
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                         "value=excluded.value", (WEEK_KEY, period.key))
        return True

    def _step(self, conn, guild, role, key, row, period, now):
        current = row["best_pct"] if row is not None and type(row["best_pct"]) is int else 100
        pct = max(FLOORS[role], min(100, current) - self._rng(guild, *key, period, "step").randint(*STEP))
        self._put(conn, guild, key, state="progressing", best_pct=pct, killed_at=None,
                  kill_s=row["kill_s"] if row is not None else None, source="rule", now=now)

    def _rule_kill(self, conn, ctx, human, rivals, guild, role, name, key, row, guild_at, killed_at, now):
        """A rival's kill by rule, never before the guild's own, and its public forum fact."""
        if killed_at < guild_at or is_hard_mode(key[0], key[2]):
            raise AssertionError("the race never kills first, nor a hard mode")
        self._put(conn, guild, key, state="killed", best_pct=0, killed_at=killed_at,
                  kill_s=row["kill_s"] if row is not None else None, source="rule", now=now)
        map_id, boss, mode = key
        known = {rival: "forum" for rival, _, _ in rivals}
        known[ctx.guild] = "forum"
        known[guild] = "own"
        fact = put_rule_fact_in_unit(
            self._store, policy="race_rule_kill_v1", key=f"{guild}:{map_id}:{boss}:{mode}", kind="rival_kill",
            text=f"{name} killed {BOSS_NAMES[(map_id, boss)]} on {mode_label(map_id, mode)}.", public=True,
            known_by=sorted(known.items()),
            inputs={"guild": guild, "role": role, "map": map_id, "boss": boss, "difficulty": mode,
                    "guild_kill_ms": int(round(guild_at * 1000)), "killed_ms": int(round(killed_at * 1000))},
            ts=killed_at, human=human)
        if type(fact) is Rejected:
            log.warning("realm_race_fact_refused policy=race_rule_kill_v1 reason=%s", fact.reason)

    # -- benchmarks -------------------------------------------------------------------------

    def benchmarks(self, now) -> int:
        """Count every sealed benchmark of the last two weeks not yet seen; returns how many."""
        if self._context() is None:
            return 0
        counted = 0
        for pull in self.realm.ports.benchmark_results(max(0.0, now - BENCHMARK_WINDOW_S)):
            try:
                counted += self.on_benchmark(pull) == "counted"
            except Exception as error:  # One bad benchmark never holds up the rest.
                log.warning("realm_race_benchmark_failed: %s", type(error).__name__)
        return counted

    def on_benchmark(self, pull) -> str:
        """One real benchmark: 'counted', 'seen' (counted before), 'ignored' (not a Realm
        guild's) or 'deferred' (the Realm is not live)."""
        ctx = self._context()
        if ctx is None:
            return "deferred"
        if type(pull) is not BenchmarkPull or type(pull.id) is not int or pull.id <= 0:
            return "ignored"
        store, now = self._store, self._now()
        human = self.realm.ports.human()
        losses = []
        with store.transaction():
            conn = store.conn
            if conn.execute("SELECT 1 FROM realm_benchmark_seen WHERE benchmark_id=?", (pull.id,)).fetchone():
                return "seen"
            guild = conn.execute("SELECT id,name FROM realm_guild WHERE id=? AND status='active'",
                                 (pull.guild_id,)).fetchone()
            if guild is None:
                return "ignored"
            gid, name = guild["id"], guild["name"]
            starts = [p.started_s for p in pull.pulls]
            before_ms = int((min(starts) if starts else pull.completed_s) * 1000)
            best = {}
            for p in pull.pulls:
                key, seconds = (p.map, p.boss_id, p.difficulty), p.ended_s - p.started_s
                if (p.kind == "benchmark" and p.outcome == "killed" and _raid_key(*key)
                        and not is_hard_mode(p.map, p.difficulty) and seconds > 0
                        and (key not in best or seconds < best[key][0])):
                    best[key] = (seconds, p.ended_s)
            rows = self._rows(conn, gid)
            for key in sorted(best):
                seconds, ended = best[key]
                if not self._really_killed(conn, key, before_ms):
                    log.info("realm_benchmark_ahead benchmark=%d guild=%d map=%d boss=%d difficulty=%d",
                             pull.id, gid, *key)
                    continue
                kill_s = max(1, int(round(seconds)))
                self._benchmark_kill(conn, gid, key, rows.get(key), ended, kill_s, now)
                mine = self._guild_best(conn, key, before_ms)
                if mine is not None and kill_s < mine[0]:
                    fact = self._loss(conn, ctx, human, pull, gid, name, key, kill_s, ended, mine, now)
                    if type(fact) is FactRow:
                        losses.append(fact)
            conn.execute("INSERT INTO realm_benchmark_seen(benchmark_id,guild,ts) VALUES (?,?,?)",
                         (pull.id, gid, now))
        self._boast(gid, losses)
        return "counted"

    def _benchmark_kill(self, conn, guild, key, row, ended, kill_s, now):
        if _killed(row):  # Killed before: keep the first kill, and the fastest time.
            best = row["kill_s"] if type(row["kill_s"]) is int and row["kill_s"] > 0 else kill_s
            first = row["killed_at"] if row["killed_at"] is not None and row["killed_at"] <= ended else ended
            self._put(conn, guild, key, state="killed", best_pct=0, killed_at=first,
                      kill_s=min(best, kill_s), source=row["source"], now=now)
        else:
            self._put(conn, guild, key, state="killed", best_pct=0, killed_at=ended, kill_s=kill_s,
                      source="benchmark", now=now)

    @staticmethod
    def _guild_best(conn, key, before_ms):
        """The guild's fastest measured kill of this mode before the benchmark: (seconds, pull).

        Only from pulls recorded since the overhead gate passed: before then the stored kills
        are its A/B sample, and a faster kill may have gone unrecorded, so no best is claimed."""
        from leeroy_mind.training.store import full_since_ms
        since = full_since_ms(conn)
        if since is None:
            return None
        row = conn.execute(f"SELECT id,end_ms-start_ms AS ms FROM tg_pull WHERE {_TG_KILLS} AND map=? "
                           "AND boss_id=? AND difficulty=? AND end_ms<=? AND start_ms>=? "
                           "ORDER BY ms,end_ms,id LIMIT 1", (*key, before_ms, since)).fetchone()
        return None if row is None else (max(1, int(round(row["ms"] / 1000))), row["id"])

    def _loss(self, conn, ctx, human, pull, gid, name, key, kill_s, ended, mine, now):
        """A rival beat the guild's best for real: the match, its public fact and the grudges."""
        map_id, boss, mode = key
        guild_s, guild_pull = mine
        detail = {"benchmark": pull.id, "guild": gid, "map": map_id, "boss": boss, "difficulty": mode,
                  "rival_s": kill_s, "guild_s": guild_s, "guild_pull": guild_pull}
        conn.execute("INSERT INTO realm_match(ts,kind,winner,detail) VALUES (?,?,?,?)",
                     (now, "benchmark", str(gid), json.dumps(detail, sort_keys=True, separators=(",", ":"))))
        minutes, seconds = divmod(kill_s, 60)
        fact = put_rule_fact_in_unit(
            self._store, policy="benchmark_loss_v1", key=f"{pull.id}:{map_id}:{boss}:{mode}",
            kind="benchmark_loss",
            text=f"{name} killed {BOSS_NAMES[(map_id, boss)]} in {minutes}:{seconds:02d}, faster than us",
            public=True, known_by=sorted({ctx.guild: "server", gid: "own"}.items()), inputs=detail,
            ts=ended, human=human)
        if type(fact) is Rejected:
            log.warning("realm_race_fact_refused policy=benchmark_loss_v1 reason=%s", fact.reason)
        cast = {member.guid for member in self.realm.ports.cast()} - {human.guid}
        for (guid,) in conn.execute("SELECT guid FROM tg_member WHERE pull=? ORDER BY guid", (guild_pull,)):
            if guid in cast:
                conn.execute("INSERT INTO realm_grudge(guild,guid,points,last,reason) VALUES (?,?,1,?,'benchmark') "
                             "ON CONFLICT(guild,guid) DO UPDATE SET points=points+1,last=excluded.last,"
                             "reason=excluded.reason", (gid, guid, now))
        return fact

    def _boast(self, guild, facts):
        """The rival boasts in Trade: the speech part's hook, when it has one."""
        if not facts:
            return
        hook = getattr(self.realm.part("speech"), "boast", None)
        if not callable(hook):
            return
        for fact in facts:
            try:
                hook(guild, fact)
            except Exception as error:
                log.warning("realm_race_boast_failed: %s", type(error).__name__)

    # -- the Realm's loop -------------------------------------------------------------------

    async def tick(self) -> None:
        """Weekly once a product week (checked every 60 s) and benchmarks every 600 s, while the
        Realm is live. Never the Realm loop's failure."""
        try:
            if self._context() is None:
                return
            now = self._now()
            if now >= self._next:
                self._next = now + TICK_S
                self.weekly(now)
            if now >= self._next_benchmarks:
                self._next_benchmarks = now + BENCHMARK_TICK_S
                self.benchmarks(now)
        except Exception as error:
            log.warning("realm_race_failed: %s", type(error).__name__)


def attach(realm):
    """The race part, with no I/O until the Realm's own tick."""
    return Race(realm), ()
