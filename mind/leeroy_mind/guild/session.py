"""Raid attendance, scoped event handling and a public, sent-line MVP vote."""

import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import random
import re

from leeroy_mind.clock import night_of
from leeroy_mind.memory.writer import MemoryCandidate
from leeroy_mind.personas.screens import has_word
from leeroy_mind.storage.store import Store
from leeroy_mind.talk.turns import TurnKind
from leeroy_mind.text import strip_wow_markup

from .dkp import apply_due_decay, attendance_entries, kill_entries
from .lines import instruction, member_name, outcome_words, public_text, template
from .loot import LootMaster, _digest, _get, _once, _put
from .raids import CATALOG, validate
from .store import _insert_ledger, balances, bench_streaks


NOT_RAIDED = "not-raided"  # meta guild:raid:<id>:not-raided: the run never reached a fight


def _record_attendance(ports, cfg, plan, raid_id, now):
    """Attendance rows and their DKP, inside the caller's transaction."""
    raided = list(plan.members) + ([ports.human_guid] if plan.human_in_raid else [])
    bench = list(plan.bench)
    ports.conn.executemany("INSERT INTO guild_attendance(raid_id,guid,status) VALUES (?,?,?)",
                           [(raid_id, g, "raided") for g in raided] + [(raid_id, g, "benched") for g in bench])
    _insert_ledger(ports.conn, attendance_entries(raided, bench, raid_id, now, cfg.dkp))


@dataclass(frozen=True, slots=True)
class RaidPlan:
    raid_key: str
    size: int
    heroic: bool
    offline: bool
    lead_guid: int
    tank_guid: int
    loot_guid: int
    members: list[int]
    bench: list[int]
    human_in_raid: bool


@dataclass(slots=True)
class RaidContextInfo:
    raid_id: int
    plan: RaidPlan
    map_id: int
    # Loot packets lack membership; only validated summon/boss evidence binds this.
    instance_id: int | None = None


class RaidSession:
    def __init__(self, ports, cfg, armory, plan, raid_id):
        self.ports, self.cfg, self.armory, self.plan, self.raid_id = ports, cfg, armory, plan, raid_id
        self._prefix = f"guild:raid:{raid_id}:"
        row = ports.conn.execute("SELECT started,kills,wipes,ended FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone()
        self.started, self.kills, self.wipes = row[0], row[1], row[2]
        self._closing = row[3] is not None
        self._close_done = False
        self._vote = None
        self._lock = asyncio.Lock()
        self._members = frozenset(plan.members)
        self._participants = self._members | ({ports.human_guid} if plan.human_in_raid else set())
        self.ctx = RaidContextInfo(raid_id, plan, CATALOG[plan.raid_key].map_id,
                                   _get(ports.conn, self._prefix + "instance"))
        self.loot = LootMaster(ports, cfg, armory, self.ctx)

    @classmethod
    async def open(cls, ports, cfg, armory, plan, *, attend=True):
        """Open the raid's record. Without `attend`, attendance and its DKP wait for `attend()`,
        once the run is proved to have started (an offline raid that reached running)."""
        raid = CATALOG.get(plan.raid_key)
        if raid is None or validate(raid, plan.size, plan.heroic, raid.min_tier) is not None:
            raise ValueError("invalid raid plan mode")
        members = list(dict.fromkeys(plan.members))
        bench = list(dict.fromkeys(plan.bench))
        cast = ports.cast()
        if (type(plan.offline) is not bool or type(plan.human_in_raid) is not bool
                or any(type(g) is not int or g <= 0 for g in (*members, *bench, plan.lead_guid, plan.tank_guid, plan.loot_guid))
                or any(g not in cast or ports.persona(g) is None for g in (*members, *bench))
                or ports.human_guid in members or ports.human_guid in bench
                or set(members) & set(bench) or len(members) + int(plan.human_in_raid) > plan.size
                or plan.tank_guid not in members or plan.loot_guid not in members
                or plan.lead_guid not in set(members) | ({ports.human_guid} if plan.human_in_raid else set())
                or plan.offline and plan.human_in_raid):
            raise ValueError("invalid raid plan membership")
        # Freeze caller-owned lists before any await or database effect.
        plan = RaidPlan(plan.raid_key, plan.size, plan.heroic, plan.offline, plan.lead_guid,
                        plan.tank_guid, plan.loot_guid, members, bench, plan.human_in_raid)
        now = ports.clock.now()
        decay = apply_due_decay(ports.conn, ports.clock.local().replace(tzinfo=None), cfg.dkp) if attend else 0
        saved_plan = asdict(plan)
        with ports.conn:
            active = ports.conn.execute("SELECT raid_id FROM guild_raid WHERE ended IS NULL ORDER BY raid_id").fetchall()
            if active:
                raid_id = active[0][0]
                if len(active) != 1 or _get(ports.conn, f"guild:raid:{raid_id}:plan") != saved_plan:
                    raise ValueError("another raid session is active")
            else:
                row = ports.conn.execute(
                    "INSERT INTO guild_raid(started,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid) VALUES (?,?,?,?,?,?,?,?)",
                    (now, plan.raid_key, plan.size, int(plan.heroic), int(plan.offline),
                     plan.lead_guid, plan.tank_guid, plan.loot_guid))
                raid_id = row.lastrowid
                if attend:
                    _record_attendance(ports, cfg, plan, raid_id, now)
                _put(ports.conn, f"guild:raid:{raid_id}:plan", saved_plan)
                fix = getattr(ports, "fix_loot_rule", None)
                if fix is not None:
                    fix(raid_id)  # the guild's loot rule for this raid (decisions), in this unit
        session = cls(ports, cfg, armory, plan, raid_id)
        if decay:
            await session._say_decay()
        if attend:
            await session._bench_memories(now)
        ports.set_lead(plan.lead_guid)
        return session

    def _attended(self):
        return self.ports.conn.execute("SELECT 1 FROM guild_attendance WHERE raid_id=? LIMIT 1",
                                       (self.raid_id,)).fetchone() is not None

    async def _say_decay(self):
        await self.ports.say(self.plan.loot_guid, "guild", template("dkp_decay", pct=f"{self.cfg.dkp.decay * 100:g}"))

    async def attend(self):
        """Record attendance, its DKP and the bench's memories once, after an opening without them.
        The week's due DKP decay comes first, as at an opening with attendance."""
        now = self.ports.clock.now()
        decay = 0
        if not self._attended():
            decay = apply_due_decay(self.ports.conn, self.ports.clock.local().replace(tzinfo=None), self.cfg.dkp)
            with self.ports.conn:
                if not self._attended():
                    _record_attendance(self.ports, self.cfg, self.plan, self.raid_id, now)
        if decay:
            await self._say_decay()
        await self._bench_memories(now)

    def retract_attendance(self):
        """Take back attend(): this raid's attendance rows and their DKP, for a run voided before
        any boss was fought. Memories already written stay."""
        with self.ports.conn:
            self.ports.conn.execute("DELETE FROM guild_attendance WHERE raid_id=?", (self.raid_id,))
            self.ports.conn.execute("DELETE FROM dkp_ledger WHERE raid_id=? AND reason IN ('attendance','standby')",
                                    (self.raid_id,))

    async def _bench_memories(self, now):
        ports, plan, raid_id, bench = self.ports, self.plan, self.raid_id, self.plan.bench
        streaks = bench_streaks(ports.conn, bench)
        repeated = []
        for guid in bench:
            persona = ports.persona(guid)
            if persona is None or persona.play.guild_role not in {"raider", "officer"}:
                continue
            streak = streaks[guid]
            label = f"{plan.raid_key.upper()} {plan.size}"
            text = (f"I got benched for {label}." if streak == 1 else
                    f"I got benched for {label} again, {streak} raids in a row.")
            key = self._prefix + f"bench-memory:{guid}"
            if _get(ports.conn, key) is None:
                await ports.remember(MemoryCandidate(guid, "event", text, int(now), (guid,),
                                                     min(8, 1 + 2 * streak), -min(0.8, 0.2 * streak),
                                                     "game", (), raid_id=raid_id, key=f"bench:{raid_id}"))
                with ports.conn:
                    _put(ports.conn, key, True)
            if streak >= 2:
                repeated.append(member_name(ports, guid))
        if repeated and _once(ports.conn, self._prefix + "bench-note"):
            ports.propose(self._speaker, "officer", instruction("bench_note", names=", ".join(repeated)), kind=TurnKind.ASIDE)

    @property
    def _speaker(self):
        return self.plan.tank_guid if self.plan.lead_guid == self.ports.human_guid else self.plan.lead_guid

    def bind_instance(self, instance_id: int) -> bool:
        if type(instance_id) is not int or not 0 < instance_id <= 0xFFFFFFFF:
            raise ValueError("instance must be a nonzero unsigned integer")
        if self._closing:
            return False
        with self.ports.conn:
            row = self.ports.conn.execute("SELECT ended FROM guild_raid WHERE raid_id=?", (self.raid_id,)).fetchone()
            if row is None or row[0] is not None:
                return False
            bound = _get(self.ports.conn, self._prefix + "instance")
            if bound is not None and bound != instance_id:
                return False
            _put(self.ports.conn, self._prefix + "instance", instance_id)
        self.ctx.instance_id = instance_id
        return True

    def _fresh(self, ev):
        return ev.ts == 0 or ev.ts / 1000 >= self.started - 5

    async def on_boss(self, ev, *, can_announce=None):
        async with self._lock:
            actual = sorted(self._participants.intersection(ev.members))
            if (self._closing or ev.map != self.ctx.map_id or not actual or not self._fresh(ev)
                    or ev.state not in {"engaged", "killed", "wiped"}
                    or type(ev.instance_id) is not int or not 0 < ev.instance_id <= 0xFFFFFFFF
                    or not self.bind_instance(ev.instance_id)):
                return
            if ev.state == "engaged":
                return
            kill_key = self._prefix + f"kill:{ev.instance_id}:{ev.boss_id}"
            key = kill_key if ev.state == "killed" else self._prefix + f"wipe:{ev.instance_id}:{ev.boss_id}:{ev.ts}"
            with self.ports.conn:
                row = self.ports.conn.execute("SELECT ended,kills,wipes FROM guild_raid WHERE raid_id=?", (self.raid_id,)).fetchone()
                if row[0] is not None or _get(self.ports.conn, key) is not None or _get(self.ports.conn, kill_key) is not None:
                    return
                kills, wipes = row[1] + int(ev.state == "killed"), row[2] + int(ev.state == "wiped")
                if ev.state == "killed":
                    _insert_ledger(self.ports.conn, kill_entries(actual, self.raid_id, self.ports.clock.now(), self.cfg.dkp))
                self.ports.conn.execute("UPDATE guild_raid SET kills=?,wipes=? WHERE raid_id=?", (kills, wipes, self.raid_id))
                _put(self.ports.conn, key, True)
            self.kills, self.wipes = kills, wipes
            if ev.state == "killed" and (can_announce is None or can_announce()):
                boss = public_text(self.ports, ev.boss_name or f"boss {ev.boss_id + 1}")
                await self.ports.say(self.plan.loot_guid, "raid", template("dkp_kill", amount=self.cfg.dkp.kill, count=len(actual), boss=boss))

    async def on_loot_drop(self, ev):
        if not self._closing and self._fresh(ev):
            await self.loot.on_loot_drop(ev)

    async def on_chat(self, ev):
        if (self._closing or ev.from_guid != self.ports.human_guid or ev.via_bridge
                or ev.map != self.ctx.map_id or not self.plan.human_in_raid or not self._fresh(ev)):
            return
        if (ev.channel in {"raid", "party", "guild"} and ev.to_guid == 0
                or ev.channel == "whisper" and ev.to_guid == self.plan.loot_guid):
            self.loot.on_human_chat(ev.text)

    async def tick(self):
        if not self._closing:
            await self.loot.tick()

    async def close(self, outcome, *, raided=True):
        """End the record. A run that was never raided keeps its row, marked not raided,
        without the DKP standings, the wrap-up line or an MVP vote."""
        async with self._lock:
            if self._close_done:
                return self._vote
            self._closing = True
            try:
                await self.loot.close()
                with self.ports.conn:
                    self.ports.conn.execute("UPDATE guild_raid SET ended=?,outcome=? WHERE raid_id=? AND ended IS NULL",
                                            (self.ports.clock.now(), str(outcome), self.raid_id))
                    if not raided:
                        _put(self.ports.conn, self._prefix + NOT_RAIDED, True)
                    row = self.ports.conn.execute("SELECT kills,wipes,outcome,ended FROM guild_raid WHERE raid_id=?", (self.raid_id,)).fetchone()
                self.kills, self.wipes, outcome, ended = row
                if not raided:
                    self._close_done = True
                    return None
                if _once(self.ports.conn, self._prefix + "standings"):
                    current = balances(self.ports.conn)
                    ranked = sorted(self.ports.cast(), key=lambda g: (-current.get(g, 0), g))[:5]
                    top = ", ".join(f"{member_name(self.ports, g)} {current.get(g, 0):.1f}" for g in ranked)
                    await self.ports.say(self.plan.loot_guid, "guild", template("dkp_standings", top=top))
                if _once(self.ports.conn, self._prefix + "wrapup"):
                    self.ports.propose(self._speaker, "guild", instruction("raid_wrapup", raid=CATALOG[self.plan.raid_key].name,
                                       kills=self.kills, wipes=self.wipes, outcome=outcome_words(outcome)), kind=TurnKind.ASIDE)
                if self.kills:
                    self._vote = MvpVote(self.ports, random.Random(self.raid_id), lead=self._speaker,
                                         members=self.plan.members,
                                         night=night_of(datetime.fromtimestamp(ended, self.ports.clock.local().tzinfo)),
                                         receipt_key=self._prefix + "mvp")
                    self._vote.start(self.ports.clock.now())
                self._close_done = True
                return self._vote
            finally:
                active = self.ports.conn.execute("SELECT raid_id FROM guild_raid WHERE ended IS NULL").fetchall()
                if not any(row[0] != self.raid_id for row in active):
                    self.ports.set_lead(None)


class MvpVote:
    def __init__(self, ports, rng, *, lead, members, night, receipt_key=None):
        self.ports, self.rng, self.lead, self.night = ports, rng, lead, night
        self.members = tuple(sorted(set(members) - {ports.human_guid}))
        self._key = receipt_key
        self._lock = asyncio.Lock()
        self._state = None

    @property
    def voters(self):
        return tuple(self._state["voters"]) if self._state else ()

    def start(self, now):
        watermark = self.ports.conn.execute("SELECT COALESCE(MAX(id),0) FROM utterance").fetchone()[0]
        if self._key is None:
            self._key = f"guild:mvp:{self.night}:" + _digest((self.lead, self.members, now, watermark))[:24]
        saved = _get(self.ports.conn, self._key)
        if saved is None:
            cast = self.ports.cast()
            members = [g for g in self.members if g in cast and self.ports.persona(g) is not None]
            eligible = [g for g in members if g != self.lead and cast[g].online]
            voters = self.rng.sample(eligible, min(3, len(eligible)))
            saved = dict(started=now, due=now + 90, lead=self.lead, members=members, voters=voters,
                         watermark=watermark,
                         names={str(g): member_name(self.ports, g) for g in members},
                         done=self.lead == self.ports.human_guid or self.lead not in members)
            with self.ports.conn:
                _put(self.ports.conn, self._key, saved)
        self._state = saved
        if saved["done"]:
            return
        if saved["lead"] == self.ports.human_guid:
            return
        if _once(self.ports.conn, self._key + ":call"):
            self.ports.propose(saved["lead"], "guild", instruction("mvp_call"), kind=TurnKind.ASIDE)
        for guid in saved["voters"]:
            if _once(self.ports.conn, self._key + f":vote:{guid}"):
                names = ", ".join(name for g, name in saved["names"].items() if int(g) != guid)
                self.ports.propose(guid, "guild", instruction("mvp_vote", names=names), kind=TurnKind.ASIDE)

    def _public_refs(self, row):
        try:
            refs = json.loads(row["memory_ids"])
            if not isinstance(refs, list):
                return None
            refs = [f"memory:{ref}" if type(ref) is int else ref for ref in refs]
            if not refs:
                return ()
            # Reuse the complete P1c traversal, including paired public chat recall.
            from leeroy_mind.nightly.lexicon import public_sources
            return public_sources(Store(self.ports.conn, clock=self.ports.clock), refs)
        except (TypeError, ValueError):
            return None

    def _tally(self):
        state = self._state
        counts, first, used, provenance, fingerprints = Counter(), {}, set(), [], {}
        rows = self.ports.conn.execute("SELECT * FROM utterance WHERE id>? AND ts>=? AND ts<=? "
                                       "AND channel='guild' AND to_guid=0 AND dropped IS NULL ORDER BY ts,id",
                                       (state["watermark"], int(state["started"]), state["due"])).fetchall()
        names = state["names"]
        name_counts = Counter(name.casefold() for name in names.values())
        for row in rows:
            voter = row["guid"]
            if voter not in state["voters"] or voter in used or voter == self.ports.human_guid:
                continue
            refs = self._public_refs(row)
            if refs is None:
                continue
            text = strip_wow_markup(row["text"])
            hits = []
            for guid, name in names.items():
                target = int(guid)
                if target == voter or name_counts[name.casefold()] != 1 or not has_word(text, name):
                    continue
                match = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE)
                hits.append((match.start(), -len(name), target))
            if not hits:
                continue
            winner = min(hits)[2]
            used.add(voter)
            counts[winner] += 1
            first.setdefault(winner, (row["ts"], row["id"]))
            provenance.extend(refs)
            fingerprints[str(row["id"])] = _digest(dict(row))
        if not counts:
            return None
        winner = min(counts, key=lambda g: (-counts[g], first[g], g))
        return dict(winner=winner, votes=counts[winner], voters=sorted(used),
                    refs=list(dict.fromkeys(provenance)), fingerprints=fingerprints)

    def _still_public(self, result):
        for line_id, fingerprint in result["fingerprints"].items():
            row = self.ports.conn.execute("SELECT * FROM utterance WHERE id=?", (int(line_id),)).fetchone()
            if row is None or _digest(dict(row)) != fingerprint or self._public_refs(row) is None:
                return False
        return True

    async def tick(self, now) -> bool:
        async with self._lock:
            if self._state is None:
                return False
            self._state = _get(self.ports.conn, self._key)
            if self._state["done"]:
                return True
            if now < self._state["due"]:
                return False
            result = self._state.get("result")
            if "result" not in self._state:
                result = self._tally()
                with self.ports.conn:
                    if result is not None:
                        payload = {"winner": result["winner"], "votes": result["votes"],
                                   "members": result["voters"], "utterances": list(result["fingerprints"]), "night": self.night}
                        row = self.ports.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES (?,?,?,?)",
                                                      (now, "guild_mvp", result["winner"], json.dumps(payload)))
                        result["event"] = row.lastrowid
                    self._state["result"] = result
                    _put(self.ports.conn, self._key, self._state)
            if result is not None and self._still_public(result):
                name = self._state["names"][str(result["winner"])]
                if _once(self.ports.conn, self._key + ":announce"):
                    await self.ports.say(self._state["lead"], "guild", template("mvp_result", name=name, votes=result["votes"]))
                for owner, text, importance, valence in (
                    (result["winner"], f"Voted MVP of the night ({result['votes']} votes).", 6, 0.6),
                    (0, f"MVP of the night: {name} ({result['votes']} votes).", 5, 0.0),
                ):
                    receipt = self._key + f":memory:{owner}"
                    if _get(self.ports.conn, receipt) is not None or not self._still_public(result):
                        continue
                    await self.ports.remember(MemoryCandidate(owner, "event", text, int(now),
                                          (result["winner"],), importance, valence, "game",
                                          (f"event:{result['event']}", *result["refs"]), key=f"mvp:{self.night}"))
                    with self.ports.conn:
                        _put(self.ports.conn, receipt, True)
            self._state["done"] = True
            with self.ports.conn:
                _put(self.ports.conn, self._key, self._state)
            return True
