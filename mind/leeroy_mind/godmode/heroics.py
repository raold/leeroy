"""Member-proposed Halls of Lightning heroics. A Discord post and a native body receipt
are separate evidence: a proposal alone never reserves a member."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
from uuid import uuid4

from leeroy_mind.bodies import BodyEvidenceUnavailable, BodyHold
from leeroy_mind.bridge import BridgeError, NotConnected
from leeroy_mind.guild.protocol import DayOwnerCheck, MemberDungeonStatus, StartMemberDungeon
from leeroy_mind.guild.roles import role_of
from leeroy_mind.llm.client import GpuPaused
from leeroy_mind.realm.actions import ActionRejected
from leeroy_mind.server.state import roster_ties

FEATURE = "member_dungeon_v1"
OWNER = "member_dungeon"
CHANNEL = "lfg-and-alts"
MAP, NAME = 602, "Halls of Lightning"
BOSSES = {0: "General Bjarngrim", 3: "Volkhan", 1: "Ionar", 2: "Loken"}
# Running heartbeats arrive every 60 seconds. Keep a 15-second delivery margin.
WINDOW, DRIVER_GAP, MAX_DRIVER, HEARTBEAT_FRESH = 3600, 60, 8, 75
HELD = "sent IS NOT NULL AND returned=0 AND phase IN ('sent','running','terminal')"


@dataclass(frozen=True)
class Candidate:
    free: bool
    raid_role: str | None = None


def _friendly(ties, a, b):
    strengths = {"crush": 3, "friend": 2, "fond": 1}
    return max(strengths.get(ties.get((a, b)), 0), strengths.get(ties.get((b, a)), 0))


def _compatible(ties, a, rest):
    return all(
        ties.get((a, b)) not in ("rival", "feud") and ties.get((b, a)) not in ("rival", "feud") for b in rest
    )


def propose_group(conn, cast, states, *, now):
    """Select a tank-led legal party, preferring the starter's friends within each role."""
    roles = {
        g: role_of(member.class_, member.spec_tab, states[g].raid_role)
        for g, member in cast.items()
        if g in states and states[g].free and member.level == 80
    }
    ties = roster_ties(conn, set(roles))
    order = ("tank", "healer", "damage", "damage", "damage")

    def fill(party):
        if len(party) == len(order):
            return party
        candidates = [
            g for g in roles if g not in party and roles[g] == order[len(party)] and _compatible(ties, g, party)
        ]
        candidates.sort(key=lambda g: (-_friendly(ties, party[0], g) if party else 0, g))
        for g in candidates:
            if result := fill([*party, g]):
                return result
        return None

    if any(list(roles.values()).count(role) < order.count(role) for role in set(order)):
        return None
    members = fill([])
    if members:
        return dict(
            map=MAP,
            members=members,
            roles={str(g): role for g, role in roles.items()},
            bench=sorted(set(roles) - set(members)),
        )
    return None


def offer(store, group, *, day, now, guild, human):
    ident = uuid4().hex
    with store.transaction():
        inserted = store.conn.execute(
            "INSERT INTO member_heroic(id,day,guild,human,created,expires,map,"
            "roster,roles,bench,phase) VALUES(?,?,?,?,?,?,?,?,?,?,'offered') "
            "ON CONFLICT(guild,human,day) DO NOTHING",
            (
                ident,
                day,
                guild,
                human,
                now,
                now + WINDOW,
                group["map"],
                json.dumps(group["members"]),
                json.dumps(group["roles"]),
                json.dumps(group["bench"]),
            ),
        )
    return ident if inserted.rowcount else None


def open_chat(conn, start, conversation, now):
    return bool(
        conn.execute(
            "UPDATE member_heroic SET conversation=?,phase='chat' WHERE id=? AND phase='offered' AND expires>?",
            (conversation, start, now),
        ).rowcount
    )


def record_post(conn, start, conversation, author, message, now):
    conn.execute(
        "INSERT OR IGNORE INTO member_heroic_post SELECT h.id,h.generation,m.author,m.id "
        "FROM member_heroic h JOIN discord_message m ON m.id=? AND m.origin='mind' AND m.deleted IS NULL "
        "AND m.channel=? AND m.author=? AND m.ts>=h.created AND m.ts<=? "
        "WHERE h.id=? AND h.conversation=? AND h.phase='chat' AND h.expires>? AND m.author IN "
        "(SELECT value FROM json_each(h.roster))",
        (message, CHANNEL, author, now, start, conversation, now),
    )


def speakers(conn, start):
    return {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT p.member FROM member_heroic_post p "
            "JOIN member_heroic h ON h.id=p.start AND h.generation=p.generation "
            "JOIN discord_message m ON m.id=p.message AND m.deleted IS NULL AND m.channel=? AND m.origin='mind' "
            "AND m.author=p.member WHERE p.start=? AND p.member IN (SELECT value FROM json_each(h.roster))",
            (CHANNEL, start),
        )
    }


def consent(conn, start):
    row = conn.execute("SELECT replaced FROM member_heroic WHERE id=?", (start,)).fetchone()
    posted = speakers(conn, start)
    return bool(row and len(posted) >= 4 and set(json.loads(row[0])) <= posted)


def end_offers(store, reason):
    with store.transaction():
        store.conn.execute(
            "UPDATE member_heroic SET phase='talk_only',reason=? WHERE phase IN ('offered','chat','agreed')",
            (reason,),
        )


def holds(conn):
    return tuple(
        BodyHold(i, OWNER, 3, frozenset(json.loads(roster)), True)
        for i, roster in conn.execute("SELECT id,roster FROM member_heroic WHERE " + HELD)
    )


def overlapping(conn, members, guild, human):
    return next(
        (
            i
            for i, roster in conn.execute(
                "SELECT id,roster FROM member_heroic WHERE " + HELD + " AND guild=? AND human=?", (guild, human)
            )
            if set(members).intersection(json.loads(roster))
        ),
        "",
    )


def _encounters(row, conn):
    terminal_ms = round(row["result_at"] * 1000)
    return [(boss, state, frozenset(json.loads(witnesses))) for boss, state, witnesses in conn.execute(
        "SELECT boss,state,witnesses FROM member_heroic_encounter WHERE start=? AND ts<=?", (row["id"], terminal_ms)
    ) if boss in BOSSES and state in ("killed", "wiped")]


def _result(row, encounters):
    # The two sources can describe the same encounter. The journal also survives a native
    # restart between heartbeats. Keep the native terminal fields immutable for later status.
    kills = max(row["kills"], len({boss for boss, state, _ in encounters if state == "killed"}))
    wiped = [boss for boss, state, _ in encounters if state == "wiped"]
    wipes = max(row["wipes"], len(wiped))
    outcome = "Cleared" if row["terminal"] == "done" and kills == 4 else "Ran"
    summary = f"{outcome} {NAME} heroic with {kills} boss kill{'s' if kills != 1 else ''} and {wipes} wipe{'s' if wipes != 1 else ''}"
    names = [BOSSES[b] for b in sorted(set(wiped))]
    return kills, wipes, summary + "." + (f" We wiped on {', '.join(names)}." if names else "")


def _results(row, conn):
    encounters = _encounters(row, conn)
    personal = {}
    for guid in json.loads(row["roster"]):
        result = _result(row, [entry for entry in encounters if guid in entry[2]])
        if result[0] or result[1]:
            personal[guid] = result
    return _result(row, encounters), personal


def seeds(conn, now, guild, human):
    from leeroy_mind.server.seeds import Seed

    for r in conn.execute(
        "SELECT * FROM member_heroic WHERE guild=? AND human=? AND "
        "((phase='offered' AND created<=? AND expires>?) OR result_at BETWEEN ? AND ?)",
        (guild, human, now, now, now - 86400, now),
    ):
        members = tuple(json.loads(r["roster"]))
        if r["phase"] == "offered":
            yield Seed(
                "lfg:" + r["id"],
                "lfg",
                (CHANNEL,),
                f"Invite the party to run {NAME} heroic together.",
                members,
                weight=8.0,
                witnesses=members,
            )
        else:
            result, personal = _results(r, conn)
            witnesses = tuple(guid for guid, known in personal.items() if known == result)
            if not witnesses:
                # Individual encounter seeds below still have their own witnesses.
                if not (r["kills"] or r["wipes"]):
                    continue
                result, witnesses = _result(r, ()), members
            yield Seed(
                "heroic:" + r["id"],
                "heroic_result",
                (CHANNEL, "general"),
                result[2],
                members,
                witnesses=witnesses,
            )
    for ident, boss, state, ts, witnesses in conn.execute(
        "SELECT e.start,e.boss,e.state,e.ts,e.witnesses "
        "FROM member_heroic_encounter e JOIN member_heroic h ON h.id=e.start WHERE h.guild=? AND h.human=? "
        "AND e.ts BETWEEN ? AND ? AND (h.result_at IS NULL OR e.ts<=ROUND(h.result_at*1000)) "
        "ORDER BY e.ts DESC LIMIT 64",
        (guild, human, (now - 86400) * 1000, now * 1000),
    ):
        party = tuple(json.loads(witnesses))
        verb = "Defeated" if state == "killed" else "Wiped on"
        yield Seed(
            f"heroic:{ident}:{boss}:{state}:{ts}",
            "heroic_result",
            (CHANNEL, "general"),
            f"{verb} {BOSSES[boss]} in {NAME} heroic.",
            party,
            witnesses=party,
        )


class Heroics:
    def __init__(self, guild, realm):
        self.guild, self.realm, self.app = guild, realm, guild.app
        self.ports, self.store, self.conn = guild.ports, self.app.store, self.app.store.conn
        self.enabled = False
        self.advertised = False
        self.poll_at = self.offer_at = 0
        self.playing = self.lease = None
        self.stopping = set()
        self.tick_lock = asyncio.Lock()
        self.attached = None
        self.write("UPDATE member_heroic SET phase='talk_only',reason='chat_ended' WHERE phase='chat'")
        realm.bodies.add_holder_source(OWNER, lambda: holds(self.conn))
        self.set_playing(None)

    def write(self, sql, parameters=()):
        """Commit a journal transition before the next await or bridge write."""
        with self.store.transaction():
            self.conn.execute(sql, parameters)

    def configure(self, on):
        self.enabled = on
        if not on:
            self.end_offers("disabled")

    def end_offers(self, reason):
        end_offers(self.store, reason)

    def row(self, ident):
        return self.conn.execute("SELECT * FROM member_heroic WHERE id=?", (ident,)).fetchone()

    def context(self):
        c = self.realm._forget_context()
        return (c.guild, c.human) if c.connected and c.presence_known and self.guild._ready() else (0, 0)

    def states(self, members=None):
        cast = self.ports.cast()
        busy = (
            self.ports.away()
            | self.ports.in_dungeon()
            | self.guild.raid_night.members_in_play()
            | self.guild.offline.members_in_play()
            | {g for hold in holds(self.conn) for g in hold.guids}
        )
        guild, human = self.context()
        days = {
            g: owner
            for g, owner in self.conn.execute(
                "SELECT d.member,d.action_ref FROM day_plan d JOIN day_permit p ON d.action_ref='day:'||p.plan "
                "WHERE p.guild=? AND p.human=? AND p.state NOT IN ('closed','unresolved')",
                (guild, human),
            )
        }
        states = {}
        for g in cast if members is None else members:
            if g not in cast:
                continue
            p = self.ports.persona(g)
            if g == self.ports.human_guid or p is None or g in busy or g not in self.app.world.online:
                continue
            try:
                free = not self.realm.bodies.others(g, besides={OWNER, days[g]} if g in days else {OWNER})
            except BodyEvidenceUnavailable:
                free = False
            states[g] = Candidate(free, p.play.raid_role)
        return cast, states

    def current(self, r):
        if not (
            r
            and self.enabled
            and self.advertised
            and not self.ports.human_online()
            and (r["guild"], r["human"]) == self.context()
            and self.app.clock.now() < r["expires"]
            and r["phase"] in ("offered", "chat", "agreed")
            and not self.ports.gpu_paused()
        ):
            return False
        roster, roles = json.loads(r["roster"]), json.loads(r["roles"])
        cast, states = self.states(roster)
        ties = roster_ties(self.conn, set(roster))
        return all(
            g in states
            and states[g].free
            and cast[g].level == 80
            and role_of(cast[g].class_, cast[g].spec_tab, states[g].raid_role) == roles[str(g)]
            and _compatible(ties, g, roster[:i])
            for i, g in enumerate(roster)
        )

    def chatting(self, c, g):
        r = self.row(c.seed.key.removeprefix("lfg:"))
        return (
            self.current(r)
            and r["phase"] == "chat"
            and r["conversation"] == c.id
            and g in json.loads(r["roster"])
        )

    def dropped(self, c, g):
        ident = c.seed.key.removeprefix("lfg:")
        r = self.row(ident)
        if not self.current(r) or r["conversation"] != c.id or r["phase"] != "chat":
            return False
        cast, states = self.states()
        roster, bench, roles = json.loads(r["roster"]), json.loads(r["bench"]), json.loads(r["roles"])
        rest = [m for m in roster if m != g]
        ties = roster_ties(self.conn, set(rest + bench))
        sub = next(
            (
                m
                for m in bench
                if m in states
                and states[m].free
                and cast[m].level == 80
                and role_of(cast[m].class_, cast[m].spec_tab, states[m].raid_role) == roles[str(g)]
                and _compatible(ties, m, rest)
            ),
            None,
        )
        if sub is None:
            return False
        roster[roster.index(g)] = sub
        bench.remove(sub)
        required = (set(json.loads(r["replaced"])) - {g}) | {sub}
        self.write(
            "UPDATE member_heroic SET generation=generation+1,roster=?,bench=?,replaced=? WHERE id=?",
            (json.dumps(roster), json.dumps(bench), json.dumps(sorted(required)), ident),
        )
        c.cast[c.cast.index(g)] = sub
        c.seed = replace(c.seed, about=tuple(roster), witnesses=tuple(roster))
        return True

    def finish_chat(self, c):
        ident = c.seed.key.removeprefix("lfg:")
        r = self.row(ident)
        if r and r["phase"] == "chat" and r["conversation"] == c.id:
            agreed = c.reason == "agreed" and self.current(r) and consent(self.conn, ident)
            self.write(
                "UPDATE member_heroic SET phase=?,reason=? WHERE id=?",
                ("agreed" if agreed else "talk_only", "" if agreed else "chat_ended", ident),
            )

    def set_playing(self, value):
        self.playing = value
        if value:
            self.store.set_meta("guild:heroic-presence", json.dumps([value[0], sorted(value[1])]))
        else:
            self.write("DELETE FROM meta WHERE key='guild:heroic-presence'")

    def members_in_play(self):
        if self.playing and 0 <= self.app.clock.now() - self.playing[0] <= HEARTBEAT_FRESH:
            return self.playing[1]
        return frozenset()

    async def release(self):
        if self.lease:
            holder = self.lease
            await self.ports.release_gpu(holder)
            if self.lease == holder:
                self.lease = None

    def fail(self, ident, reason):
        self.write(
            "UPDATE member_heroic SET phase='talk_only',reason=? WHERE id=? "
            "AND phase IN ('offered','chat','agreed')",
            (reason, ident),
        )

    async def prepare(self, r):
        from leeroy_mind.godmode.dayrun import HANDOFF_SECONDS, check_refusal

        ident = r["id"]
        members = tuple(json.loads(r["roster"]))
        cmd = StartMemberDungeon(ident, members[0], members[1:], MAP)
        day = self.realm.parts["day"]
        epoch = day.epoch

        def valid():
            return (
                self.current(self.row(ident))
                and consent(self.conn, ident)
                and day.context_valid(cleanup=True)
                and epoch == day.epoch
            )

        if not valid():
            self.fail(ident, "consent_expired")
            return
        origins = day.inspection_origins(members)
        if origins is None:
            return  # an opening day_begin has no authenticated origin yet
        until = min(r["expires"], self.app.clock.now() + HANDOFF_SECONDS)
        binding = DayOwnerCheck(ident, origins, int(until * 1000))
        check = replace(cmd, id=uuid4().hex, day_check=binding, dungeon_inspect=True)

        async def probe(control):
            if not valid() or self.app.clock.now() >= until:
                raise ActionRejected("consent_expired")
            ack = await self.app.bridge.send(control, preserve_id=True, expected_epoch=epoch)
            if ack is None or ack.id != control.id:
                raise ActionRejected("check_unknown")
            return "" if ack.ok else check_refusal(control, ack.error)

        try:
            refusal = await probe(check)
            if refusal == "day_driver_required":
                if r["tries"] >= MAX_DRIVER:
                    self.fail(ident, "driver_pending")
                    return
                self.write(
                    "UPDATE member_heroic SET tries=tries+1,next_try=? WHERE id=?",
                    (self.app.clock.now() + DRIVER_GAP, ident),
                )
                refusal = await probe(replace(check, id=uuid4().hex, dungeon_driver_ticket=check.id))
                if refusal == "driver_pending" and r["tries"] + 1 < MAX_DRIVER:
                    return
            if refusal:
                self.fail(ident, refusal)
                return
            self.lease = "member_dungeon:" + ident
            if await self.ports.acquire_gpu(self.lease) is False or not valid():
                self.fail(ident, "admission_changed")
                return

            async def original(prepared, *, expected_epoch):
                if not valid():
                    raise ActionRejected("consent_expired")
                grant = self.realm.bodies.claim(OWNER, members, rank=3, until=self.app.clock.now() + 30)
                try:
                    if (
                        grant != set(members)
                        or not self.realm.bodies.may_move_all(OWNER, members, rank=3)
                        or not valid()
                    ):
                        raise ActionRejected("held")
                    self.write(
                        "UPDATE member_heroic SET phase='sent',sent=? WHERE id=? AND phase='agreed'",
                        (self.app.clock.now(), ident),
                    )
                    try:
                        return await self.ports.send(prepared, expected_epoch=expected_epoch)
                    except NotConnected:
                        self.write(
                            "UPDATE member_heroic SET phase='talk_only',returned=1,reason='no_link' "
                            "WHERE id=? AND phase='sent'",
                            (ident,),
                        )
                        raise
                finally:
                    self.realm.bodies.release(OWNER, grant)

            ack = await day.handoff(cmd, members, "heroic", valid, original)
            if ack and ack.id == ident:
                if ack.ok:
                    self.write("UPDATE member_heroic SET phase='running' WHERE id=? AND phase='sent'", (ident,))
                else:
                    self.write(
                        "UPDATE member_heroic SET phase='talk_only',returned=1,reason=? WHERE id=? "
                        "AND phase='sent'",
                        (check_refusal(cmd, ack.error), ident),
                    )
        except GpuPaused:
            self.fail(ident, "gpu_paused")
        except ActionRejected as exc:
            self.fail(ident, check_refusal(cmd, str(exc)))
        except (BridgeError, OSError, TimeoutError):
            self.fail(ident, "link_changed")  # entered originals stay sent until reconciled
        finally:
            if self.row(ident)["phase"] != "running":
                await self.release()

    async def status(self, ident):
        day = self.realm.parts["day"]
        c = self.context()
        r = self.row(ident)
        if not r or c != (r["guild"], r["human"]) or not day.context_valid(cleanup=True):
            return
        epoch = day.epoch
        cmd = MemberDungeonStatus(uuid4().hex, ident)
        ack = await self.app.bridge.send(cmd, preserve_id=True, expected_epoch=epoch)
        if (
            ack
            and ack.id == cmd.id
            and not ack.ok
            and ack.error == "unknown_dungeon"
            and self.context() == c
            and epoch == day.epoch
        ):
            self.write(
                "UPDATE member_heroic SET phase='talk_only',returned=1,reason='not_accepted' "
                "WHERE id=? AND phase='sent'",
                (ident,),
            )

    async def tick(self):
        async with self.tick_lock:
            await self._tick()

    async def _tick(self):
        from leeroy_mind.guild.protocol import StopMemberDungeon

        now = self.app.clock.now()
        guild, human = self.context()
        if not guild:
            return
        self.write(
            "UPDATE member_heroic SET phase='talk_only',reason='expired' WHERE expires<=? "
            "AND phase IN ('offered','chat','agreed')",
            (now,),
        )
        owned = self.conn.execute(
            "SELECT * FROM member_heroic WHERE guild=? AND human=? AND " + HELD + " ORDER BY created",
            (guild, human),
        ).fetchall()
        if owned:
            active = next((r for r in owned if r["phase"] == "running" and r["id"] == self.attached), None)
            if self.lease and (
                not self.enabled or self.ports.human_online() or all(r["terminal"] for r in owned)
            ):
                await self.release()
            elif (
                active
                and self.enabled
                and not self.ports.human_online()
                and not self.ports.gpu_paused()
                and not self.lease
            ):
                self.lease = "member_dungeon:" + active["id"]
                try:
                    if await self.ports.acquire_gpu(self.lease) is False:
                        await self.release()
                except GpuPaused:
                    await self.release()
            if now >= self.poll_at:
                self.poll_at = now + 5
                for r in owned:
                    if not self.enabled and r["phase"] != "terminal" and r["id"] not in self.stopping:
                        cmd = StopMemberDungeon(uuid4().hex, r["id"])
                        ack = await self.app.bridge.send(
                            cmd, preserve_id=True, expected_epoch=self.realm._transport_epoch
                        )
                        if ack and ack.ok and ack.id == cmd.id:
                            self.stopping.add(r["id"])
                    await self.status(r["id"])
            if any(r["slot_held"] for r in owned):
                return
        if self.lease:
            await self.release()
        if not self.enabled or not self.advertised or self.ports.human_online() or self.ports.gpu_paused():
            return
        day = self.app.clock.local().date().isoformat()
        r = self.conn.execute(
            "SELECT * FROM member_heroic WHERE guild=? AND human=? AND "
            "(day=? OR phase IN ('offered','chat','agreed')) ORDER BY created DESC LIMIT 1",
            (guild, human, day),
        ).fetchone()
        if r:
            if r["phase"] == "agreed" and now >= r["next_try"]:
                await self.prepare(r)
        elif now >= self.offer_at:
            self.offer_at = now + 60
            cast, states = self.states()
            group = propose_group(self.conn, cast, states, now=now)
            if group:
                offer(self.store, group, day=day, now=now, guild=guild, human=human)

    def boss(self, ev):
        if (
            self.realm._delivery_epoch(ev) != self.realm._transport_epoch
            or ev.map != MAP
            or ev.boss_id not in BOSSES
            or ev.state not in ("killed", "wiped")
            or not ev.instance_id
        ):
            return
        guild, human = self.context()
        matches = []
        for r in self.conn.execute(
            "SELECT * FROM member_heroic WHERE phase='running' AND sent IS NOT NULL AND guild=? AND human=? AND instance=?",
            (guild, human, ev.instance_id),
        ):
            members = set(json.loads(r["roster"]))
            if (
                not ev.members
                or not set(ev.members) <= members
                or not r["sent"] * 1000 <= ev.ts <= self.app.clock.now() * 1000 + 1000
            ):
                continue
            matches.append(r["id"])
        if len(matches) != 1:
            return
        start = matches[0]
        self.write(
            "INSERT OR IGNORE INTO member_heroic_encounter "
            "SELECT ?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM member_heroic_encounter "
            "WHERE start=? AND boss=? AND state='killed')",
            (start, ev.boss_id, ev.state, ev.ts, json.dumps(sorted(set(ev.members))), start, ev.boss_id),
        )

    def job(self, ev):
        if ev.kind != OWNER or self.realm._delivery_epoch(ev) != self.realm._transport_epoch:
            return
        r = self.row(ev.id)
        now = self.app.clock.now()
        if not r or r["sent"] is None or r["returned"] or self.context() != (r["guild"], r["human"]):
            return
        members = frozenset(json.loads(r["roster"]))
        if (
            len(ev.guids) != 5
            or set(ev.guids) != members
            or any(type(g) is not int for g in ev.guids)
            or ev.map_id not in (None, MAP)
            or not r["sent"] * 1000 - 1000 <= ev.ts <= now * 1000 + 1000
            or type(ev.instance_id) is not int
            or not 0 <= ev.instance_id <= 0xFFFFFFFF
            or r["instance"]
            and r["instance"] != ev.instance_id
            or not ev.instance_id
            and (ev.kills or ev.wipes)
            or type(ev.kills) is not int
            or not 0 <= ev.kills <= 4
            or type(ev.wipes) is not int
            or not 0 <= ev.wipes <= 0xFFFFFFFF
            or ev.revision is not None
            and (type(ev.revision) is not int or ev.revision < max(1, r["revision"]))
        ):
            return
        if ev.state == "progress":
            if r["terminal"] or ev.stage not in (
                "accepted",
                "grouping",
                "teleporting",
                "starting",
                "running",
                "restarting",
                "tearing_down",
            ):
                return
            self.write(
                "UPDATE member_heroic SET phase='running',instance=?,revision=MAX(revision,?) WHERE id=?",
                (ev.instance_id, ev.revision or 0, ev.id),
            )
            self.attached = ev.id
            if ev.stage == "running" and ev.members_in_instance is not None:
                if self.playing and self.playing[0] > ev.ts / 1000:
                    return
                live = (
                    ev.members_in_instance == 5
                    and ev.instance_id
                    and type(ev.boss_engaged) is bool
                    and type(ev.last_progress_ts) is int
                    and r["sent"] * 1000 - 1000 <= ev.last_progress_ts <= ev.ts
                    and 0 <= now - ev.ts / 1000 <= HEARTBEAT_FRESH
                )
                self.set_playing((ev.ts / 1000, members) if live else None)
            elif ev.stage != "running":
                self.set_playing(None)
            return
        if (
            ev.state not in ("done", "failed")
            or ev.revision is None
            or type(ev.returned) is not bool
            or ev.slot_held is not None and type(ev.slot_held) is not bool
            or ev.map_id != MAP
            or r["terminal"]
            and (ev.state, ev.reason, ev.kills, ev.wipes)
            != (r["terminal"], r["reason"], r["kills"], r["wipes"])
        ):
            return
        if self.attached in (None, ev.id):
            self.attached = None
            self.set_playing(None)
        with self.store.transaction():
            self.write(
                "UPDATE member_heroic SET phase='terminal',terminal=?,reason=?,returned=?,revision=?,"
                "slot_held=MIN(slot_held,?),"
                "instance=?,kills=?,wipes=?,result_at=COALESCE(result_at,?) WHERE id=?",
                (
                    ev.state,
                    ev.reason[:80],
                    ev.returned,
                    ev.revision,
                    not ev.returned and ev.slot_held is not False,
                    ev.instance_id,
                    ev.kills,
                    ev.wipes,
                    ev.ts / 1000,
                    ev.id,
                ),
            )
            if not r["terminal"]:
                self.remember_result(ev, members, now)
        self.realm.parts["day"].changed()

    def remember_result(self, ev, members, now):
        from leeroy_mind.nightly.attraction import reflection_row

        result, personal = _results(self.row(ev.id), self.conn)
        if not personal:
            return
        kills, wipes, _ = result
        witnesses = sorted(guid for guid, known in personal.items() if known == result)
        event = self.conn.execute(
            "INSERT INTO game_event(ts,type,guid,payload) VALUES(?,'member_dungeon',?,?)",
            (
                ev.ts / 1000,
                min(witnesses or personal),
                json.dumps(
                    dict(
                        start=ev.id,
                        map=MAP,
                        instance=ev.instance_id,
                        kills=kills,
                        wipes=wipes,
                        members=sorted(members),
                        witnesses=witnesses,
                    )
                ),
            ),
        ).lastrowid
        for guid, (personal_kills, _, fact) in personal.items():
            memory = reflection_row(
                guid,
                fact,
                now=now,
                event=ev.ts / 1000,
                actors=members,
                evidence=(f"event:{event}",),
                key=f"heroic:{ev.id}",
                importance=4,
                valence=0.4 if personal_kills else -0.2,
            )
            self.app.writer.put(replace(memory, kind="event", source="game", place=NAME))
