"""Run a reciprocal plan under one ledger owner. The durable action reducer proves each step."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import re
from uuid import uuid4

from leeroy_mind.bodies import BodyEvidenceUnavailable
from leeroy_mind.guild.protocol import BotDo
from leeroy_mind.nightly.attraction import reflection_row
from leeroy_mind.protocol import Hello
from leeroy_mind.realm.actions import ActionRejected
from leeroy_mind.realm.action_store import ActionCapacity, ActionUnavailable
from leeroy_mind.realm.protocol import DayBegin, DayRevoke, DayState, DayStatus, Duel, RealmMove, RpgStatus
from leeroy_mind.server.state import pulled_into_play, tie_kind
from .dayplan import holidays, slots_for

AWAY_MAX = 20
FEATURE = "day_plan_handoff_v1"
STATUS = {"city": "wander npc", "inn": "rest", "holiday": "wander npc",
          "camp": "idle", "questing": "do quest", "gathering": "do gather"}
ACTED = {"city": "Spent time in town", "inn": "Rested", "holiday": "Visited the festivities",
         "camp": "Camped", "questing": "Set out questing", "gathering": "Went gathering",
         "duel_practice": "Practiced dueling", "fishing": "Went fishing"}
REASONS = {"his_group": "you joined his group", "raid": "raid night took priority",
           "offline_raid": "you were called for a raid", "string": "he asked for you",
           "heroic": "you joined a dungeon run", "guild_add": "he called you into his group",
           "held": "you were needed elsewhere", "combat": "the fight took priority"}
LIVE = ("planned", "running")


def disposition(record):
    if record is None or record.state not in {"completed", "not_sent"}:
        return "pending"
    return "proved" if record.result_ok is True else "skipped"


def preempt_rows(store, rows, reason):
    with store.transaction():
        store.conn.executemany("UPDATE day_plan SET status='preempted',reason=? WHERE id=? "
                               "AND status IN ('planned','running')", [(reason,r["id"]) for r in rows])


def fact(row, names):
    partners = json.loads(row["partners"])
    company = " with " + ", ".join(names[g] for g in partners) if partners and all(
        g in names for g in partners) else ""
    return f"{ACTED[row['activity']]}{company} at {row['place'].replace('_', ' ')}."


def card_lines(conn, guid, now, names):
    row = conn.execute("SELECT d.*,p.evidence FROM day_plan d LEFT JOIN day_permit p ON "
        "d.action_ref='day:'||p.plan WHERE member=? AND starts<=? AND ends>? AND "
        "(d.status='preempted' OR (d.status IN ('running','done') AND p.evidence IS NOT NULL)) "
        "ORDER BY starts DESC LIMIT 1", (guid, now, now-86400)).fetchone()
    if not row:
        return []
    if row["status"] == "preempted":
        return ["Your plans changed because " + REASONS.get(row["reason"], "you were needed elsewhere") + "."]
    return [fact(row, names)]


def seeds(conn, now, names):
    from leeroy_mind.server.seeds import Seed
    rows = conn.execute("SELECT d.* FROM day_plan d JOIN day_permit p ON d.id=p.plan "
        "WHERE status IN ('running','done') AND p.evidence IS NOT NULL AND starts<=? AND ends>? "
        "ORDER BY starts DESC,id LIMIT 40", (now,now-86400))
    for row in rows:
        members = (row["member"], *json.loads(row["partners"]))
        if all(g in names for g in members):
            yield Seed(f"day:{row['id']}", "day", ("general",),
                       names[row["member"]] + " " + fact(row,names)[0].lower() + fact(row,names)[1:],
                       members, witnesses=members)


class DayRun:
    def __init__(self, realm):
        self.realm = realm
        self.app = realm.app
        self.store = self.app.store
        self.conn = self.store.conn
        self.boot = 0
        self.epoch = 0
        self.sending = set()
        self.due = {}
        self.waiters = {}
        self.owners = {}

    def rows(self, plan):
        return self.conn.execute("SELECT * FROM day_plan WHERE action_ref=? ORDER BY id",
                                 (f"day:{plan}",)).fetchall()

    def permit(self, plan):
        return self.conn.execute("SELECT * FROM day_permit WHERE plan=?", (plan,)).fetchone()

    def context_valid(self):
        c = self.realm.action_context()
        return c.enabled and c.connected and c.presence_known and c.connection_epoch == self.epoch and bool(self.boot)

    def reason(self, rows):
        now = self.app.clock.now()
        guids = {r["member"] for r in rows}
        if guids & pulled_into_play(self.app):
            return "his_group"
        if str(self.app.raid.phase(now)) == "combat":
            return "combat"
        row = rows[0]
        if (row["starts"],row["ends"]) not in slots_for(self.conn,row["night"]).values():
            return "raid"
        if row["activity"] == "holiday" and row["place"] not in holidays(self.store, now):
            return "holiday_ended"
        if len(guids) == 2:
            a,b = sorted(guids)
            if {tie_kind(self.conn,a,b),tie_kind(self.conn,b,a)} & {"feud","rival"}:
                return "changed_tie"
        return ""

    def close(self, plan, status, reason):
        owner = f"day:{plan}"
        with self.store.transaction():
            self.conn.execute("UPDATE day_plan SET status=?,reason=? WHERE action_ref=? AND "
                              "status IN ('planned','running')", (status,reason,owner))
            self.conn.execute("UPDATE day_permit SET state='revoking',reason=? WHERE plan=? "
                              "AND state NOT IN ('closed','revoking','unresolved')", (reason,plan))
        self.realm.queue.drop_owner(owner)
        self.realm.bodies.release(owner)

    def send_control(self, permit, command):
        plan = permit["plan"]
        if plan in self.sending:
            return
        self.sending.add(plan)
        async def send():
            try:
                ack = await self.app.bridge.send(command, preserve_id=True)
                if isinstance(command, DayBegin) and not ack.ok:
                    self.close(plan,"skipped","refused")
                    with self.store.transaction():
                        self.conn.execute("UPDATE day_permit SET state='closed' WHERE plan=? "
                                          "AND token IS NULL", (plan,))
            finally:
                self.sending.discard(plan)
        task = self.realm.spawn(send)
        if task is None:
            self.sending.discard(plan)
        else:
            task.add_done_callback(lambda _: self.sending.discard(plan))

    def observe(self, ev):
        if self.realm._delivery_epoch(ev) != self.realm.action_context().connection_epoch:
            return
        permit = self.conn.execute("SELECT * FROM day_permit WHERE begin_id=?", (ev.origin_id,)).fetchone()
        if not permit or not re.fullmatch("[a-f0-9]{32}", ev.permit) or ev.serial <= 0:
            return
        plan = permit["plan"]
        rows = self.rows(plan)
        c = self.realm.action_context()
        if (not c.presence_known or (ev.guild,ev.human) != (c.guild,c.human) or
                tuple(sorted(ev.members)) != tuple(sorted(r["member"] for r in rows))
                or permit["boot"] != self.boot or (permit["token"] is not None and
                (permit["token"],permit["serial"]) != (ev.permit,str(ev.serial)))):
            return
        if ev.state not in {"open","revoking","closed","unresolved"} or permit["state"] == "closed":
            return
        if ev.state == "open" and permit["state"] in {"revoking","unresolved"}:
            state = permit["state"]
        else:
            state = ev.state
        if ev.state != "open" and ev.reason in REASONS:
            self.close(plan,"preempted",ev.reason)
        with self.store.transaction():
            self.conn.execute("UPDATE day_permit SET token=?,serial=?,state=? WHERE plan=?",
                              (ev.permit,str(ev.serial),state,plan))
        if state == "closed":
            self.realm.action_store.day_released(plan, ev, self.app.clock.now())
            self.realm.bodies.release(f"day:{plan}")
            self.due.pop(plan,None)

    async def on_event(self, ev):
        if isinstance(ev, Hello):
            self.boot = ev.started_ms if FEATURE in ev.features else 0
            self.epoch = self.realm._delivery_epoch(ev) or 0
        elif isinstance(ev, DayState):
            self.observe(ev)
        else:
            for permit in self.conn.execute("SELECT plan FROM day_permit WHERE state IN ('opening','open')").fetchall():
                plan = permit["plan"]
                rows = self.rows(plan)
                if reason := self.reason(rows):
                    self.close(plan, "preempted" if reason in REASONS else "skipped", reason)

    def cohort(self, row):
        participants = sorted([row["member"], *json.loads(row["partners"])])
        rows = self.conn.execute("SELECT * FROM day_plan WHERE night=? AND starts=? AND member "
            "IN (SELECT value FROM json_each(?)) ORDER BY id", (row["night"],row["starts"],
                                                                json.dumps(participants))).fetchall()
        if len(rows) != len(participants) or any(r["status"] != "planned" or r["action_ref"] is not None
                or (r["ends"],r["activity"],r["place"]) != (row["ends"],row["activity"],row["place"])
                or sorted([r["member"], *json.loads(r["partners"])]) != participants for r in rows):
            return []
        return rows

    def start(self, row, now, occupied):
        rows = self.cohort(row)
        if not rows:
            return
        plan = rows[0]["id"]
        owner = f"day:{plan}"
        guids = [r["member"] for r in rows]
        why = self.reason(rows)
        if any(g in self.owners for g in guids):
            why = "held"
        if not why and row["activity"] != "offline":
            if not self.context_valid():
                why = "native_unavailable"
            elif occupied + len(guids) > AWAY_MAX:
                why = "away_cap"
            else:
                try:
                    claimed = self.realm.bodies.claim(owner, guids, rank="realm_day", until=row["ends"])
                except BodyEvidenceUnavailable:
                    claimed = set()
                if set(guids) != set(claimed):
                    self.realm.bodies.release(owner)
                    why = "held"
        with self.store.transaction():
            self.conn.executemany("UPDATE day_plan SET action_ref=? WHERE id=?", [(owner,r["id"]) for r in rows])
            if why:
                status = "preempted" if why in REASONS else "skipped"
                self.conn.execute("UPDATE day_plan SET status=?,reason=? WHERE action_ref=?", (status,why,owner))
            elif row["activity"] == "offline":
                self.conn.execute("UPDATE day_plan SET status='done',reason='no_game_action' WHERE action_ref=?", (owner,))
            else:
                self.conn.execute("INSERT INTO day_permit(plan,begin_id,boot,state) VALUES(?,?,?,'opening')",
                                  (plan,uuid4().hex,self.boot))
        if why or row["activity"] == "offline":
            return
        permit = self.permit(plan)
        self.send_control(permit, DayBegin(permit["begin_id"], tuple(sorted(guids)), row["activity"],
                                           row["place"], int(row["ends"]*1000)))

    def submit(self, permit, rows, kind, guid):
        plan, token = permit["plan"], permit["token"]
        row = rows[0]
        ident = uuid4().hex
        if kind == "move":
            cmd = RealmMove(ident,guid,row["place"],slot=next(i for i,r in enumerate(rows) if r["member"]==guid),
                            day_permit=token)
        elif kind == "fishing":
            cmd = BotDo(ident,(guid,),"do","go fishing",day_permit=token)
        elif kind == "duel":
            cmd = Duel(ident,guid,rows[1]["member"],day_permit=token)
        else:
            cmd = RpgStatus(ident,guid,STATUS[row["activity"]],day_permit=token)
        with self.store.transaction():
            self.conn.execute("INSERT INTO day_step(id,plan,kind,member) VALUES(?,?,?,?)",(ident,plan,kind,guid))
        def valid():
            return (self.context_valid() and self.app.clock.now() < row["ends"] and
                    self.permit(plan)["state"] == "open" and not self.reason(rows))
        try:
            self.realm.queue.submit(cmd, owner=f"day:{plan}", still_valid=valid)
        except (ActionRejected, ActionCapacity, ActionUnavailable, ValueError):
            self.close(plan, "skipped", "admission_refused")

    def record(self, plan, rows, ids):
        now = self.app.clock.now()
        members = [r["member"] for r in rows]
        names = {g:m.name for g,m in self.store.cast().items()}
        with self.store.transaction():
            p = self.permit(plan)
            if p["evidence"] is not None or p["state"] != "open":
                return
            payload = {"members": members, "plan": plan, "actions": ids, "activity": rows[0]["activity"],
                       "place": rows[0]["place"]}
            event = self.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES(?,'day_activity',?,?)",
                                      (now,members[0],json.dumps(payload))).lastrowid
            for r in rows:
                memory = reflection_row(r["member"],fact(r,names), now=now, actors=members,
                    evidence=(f"event:{event}",), key=f"day:{r['id']}", importance=3,valence=.2)
                self.app.writer.put(replace(memory,kind="event",source="game",place=r["place"]))
            self.conn.execute("UPDATE day_permit SET evidence=? WHERE plan=?",(event,plan))
            self.conn.execute("UPDATE day_plan SET status='running' WHERE action_ref=?",(f"day:{plan}",))

    def advance(self, permit, now):
        plan = permit["plan"]
        rows = self.rows(plan)
        row = rows[0]
        state = permit["state"]
        if state in ("opening","open"):
            if reason := self.reason(rows):
                self.close(plan, "preempted" if reason in REASONS else "skipped", reason)
                state = "revoking"
            elif now >= row["ends"]:
                self.close(plan, "done" if permit["evidence"] else "skipped", "ended")
                state = "revoking"
        if state != "open":
            if now >= self.due.get(plan,0) and plan not in self.sending and self.context_valid():
                self.due[plan] = now + 2
                cmd = (DayRevoke(uuid4().hex,permit["begin_id"],self.permit(plan)["reason"] or "ended")
                       if state == "revoking" else DayStatus(uuid4().hex,permit["begin_id"]))
                self.send_control(permit,cmd)
            return
        if permit["evidence"] or not self.context_valid():
            return
        guids = [r["member"] for r in rows]
        owner = f"day:{plan}"
        if not set(guids) <= self.realm.bodies.claims_for(owner):
            # A process restart loses leases. A permit on a former bridge serial is cleanup-only.
            self.close(plan, "preempted", "held")
            return
        steps = {(s["kind"],s["member"]):s["id"] for s in self.conn.execute(
            "SELECT * FROM day_step WHERE plan=?", (plan,))}
        sequence = [("move",g) for g in guids]
        if row["activity"] == "duel_practice":
            sequence.append(("duel", guids[0]))
        else:
            sequence.extend(("fishing" if row["activity"]=="fishing" else "status",g) for g in guids)
        for kind,guid in sequence:
            ident = steps.get((kind,guid))
            if ident is None:
                self.submit(permit,rows,kind,guid)
                return
            result = self.realm.action_store.get(ident)
            if result is None or disposition(result) == "skipped":
                self.close(plan, "skipped", "action_refused")
                return
            if disposition(result) == "pending":
                if result.state in ("unknown","conflict"):
                    self.close(plan,"skipped","action_unproved")
                return
        self.record(plan,rows,list(steps.values()))

    async def tick(self):
        now = self.app.clock.now()
        for p in self.conn.execute("SELECT * FROM day_permit WHERE state!='closed'").fetchall():
            self.advance(p,now)
        with self.store.transaction():
            self.conn.execute("UPDATE day_plan SET status='skipped',reason='expired' WHERE status='planned' "
                              "AND action_ref IS NULL AND ends<=?", (now,))
        for r in self.conn.execute("SELECT * FROM day_plan WHERE status='planned' AND action_ref IS NULL "
                                   "AND starts<=? AND ends>? ORDER BY starts,id LIMIT 40", (now,now)).fetchall():
            occupied = self.conn.execute("SELECT count(*) FROM day_plan d JOIN day_permit p ON "
                                         "d.action_ref='day:'||p.plan WHERE p.state!='closed'").fetchone()[0]
            self.start(r,now,occupied)

    async def handoff(self, members, reason, valid, send):
        """Submit the original owner action once, after overlapping native permits report closed."""
        targets = set(members)
        if not targets or any(g in self.owners for g in targets):
            raise ActionRejected("day_takeover_busy")
        key = uuid4().hex
        self.owners.update((g,key) for g in targets)
        try:
            blocks = self.conn.execute("SELECT DISTINCT p.plan FROM day_permit p JOIN day_plan d ON "
                "d.action_ref='day:'||p.plan WHERE p.state!='closed' AND d.member IN "
                "(SELECT value FROM json_each(?))",(json.dumps(sorted(targets)),)).fetchall()
            for (plan,) in blocks:
                self.close(plan,"preempted",reason)
            if blocks:
                deadline = asyncio.get_running_loop().time()+30
                while any(self.permit(p)["state"] != "closed" for (p,) in blocks):
                    if not valid() or asyncio.get_running_loop().time() >= deadline:
                        raise ActionRejected("day_cleanup_unresolved")
                    await asyncio.sleep(.05)
            if not valid():
                raise ActionRejected("day_takeover_expired")
            return await send()
        finally:
            for g in targets:
                self.owners.pop(g,None)
