"""Run a reciprocal plan under one ledger owner. The durable action reducer proves each step."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import re
from uuid import uuid4

from leeroy_mind.bridge import BridgeError, NotConnected
from leeroy_mind.bodies import BodyEvidenceUnavailable
from leeroy_mind.guild.protocol import BotDo, ClearLockout, DayOwnerCheck, StartMemberDungeon, StartOfflineRaid
from leeroy_mind.nightly.attraction import reflection_row
from leeroy_mind.protocol import Hello
from leeroy_mind.realm.actions import ActionRejected
from leeroy_mind.realm.action_store import ActionCapacity, ActionUnavailable
from leeroy_mind.realm.protocol import DayBegin, DayRevoke, DayState, DayStatus, Duel, RealmMove, RpgStatus
from leeroy_mind.server.state import pulled_into_play, tie_kind
from .dayplan import holidays, slots_for

HANDOFF_SECONDS = 30
AWAY_MAX = 20
FEATURE = "day_plan_handoff_v1"
STATUS = {"city": "wander npc", "inn": "rest", "holiday": "wander npc",
          "camp": "idle", "questing": "wander npc", "gathering": "do gather"}
ACTED = {"city": "Spent time in town", "inn": "Rested", "holiday": "Visited the festivities",
         "camp": "Camped", "questing": "Visited the quest camp", "gathering": "Went gathering",
         "duel_practice": "Practiced dueling", "fishing": "Went fishing"}
REASONS = {"his_group": "you joined his group", "raid": "raid night took priority",
           "offline_raid": "you were called for a raid", "string": "he asked for you",
           "heroic": "a dungeon run took priority", "guild_add": "he called you into his group",
           "held": "you were needed elsewhere", "combat": "the fight took priority"}
NATIVE_REASONS = set(REASONS) | {'ended', 'context_changed', 'action_unproved', 'action_refused',
                                  'admission_refused', 'holiday_ended', 'changed_tie'}


def check_refusal(command, error):
    """Reduce a native diagnostic to a code before it reaches any owner controller."""
    error = (error or "").strip()
    if error.startswith("unknown type"):
        return "unsupported"
    if error.startswith("bad field"):
        return "invalid"
    if isinstance(command, ClearLockout) and error.startswith("in_instance:"):
        # ClearLockout reports exactly those requested GUIDs still transferring to or inside its map.
        manifest = {str(guid) for guid in command.guids if type(guid) is int and 0 < guid <= 0xFFFFFFFF}
        suffix = error.removeprefix("in_instance:")
        if len(suffix) > 11 * len(manifest):
            return "refused"
        named = suffix.split(",")
        return error if len(named) == len(set(named)) and set(named) <= manifest else "refused"
    return error if re.fullmatch(r"[a-z_]{1,40}(?::(?:[a-z_]{1,40}|[0-9]{1,10})){0,2}", error) else "refused"


def disposition(record):
    if record is None or record.state not in {"completed", "not_sent"}:
        return "pending"
    return "proved" if record.result_ok is True else "skipped"


def fact(row, names, phase='activity'):
    partners = json.loads(row["partners"])
    companions = ", ".join(names[g] for g in partners) if partners and all(g in names for g in partners) else ""
    place = row['place'].replace('_', ' ')
    if phase == "arrival":
        return f"Arrived at {place}."
    activity = ACTED[row['activity']]
    if phase == "shared" and companions:
        if row['activity'] == 'duel_practice':
            return f"{activity} with {companions} at {place}."
        return f"{activity} at {place}. {companions} was there."
    return f"{activity} at {place}."


def card_lines(conn, guid, now, names):
    row = conn.execute("SELECT d.* FROM day_plan d WHERE member=? AND starts<=? AND ends>? AND "
        "(d.status='preempted' OR (d.status IN ('running','done','skipped') AND d.evidence IS NOT NULL)) "
        "ORDER BY starts DESC LIMIT 1", (guid, now, now-86400)).fetchone()
    if not row:
        return []
    if row["status"] == "preempted":
        return ["Your plans changed because " + REASONS.get(row["reason"], "you were needed elsewhere") + "."]
    return [fact(row, names, row['phase'])]


def seed_valid(conn, key, now):
    prefix, _, plan = key.partition(":")
    if prefix != "day" or not plan.isascii() or not plan.isdecimal() or len(plan) > 18:
        return False
    return conn.execute("SELECT 1 FROM day_plan d WHERE d.id=? AND status IN ('running','done') AND d.evidence IS NOT NULL "
                        "AND starts<=? AND ends>?", (int(plan), now, now - 86400)).fetchone() is not None


def seeds(conn, now, names):
    from leeroy_mind.server.seeds import Seed
    rows = conn.execute("SELECT d.* FROM day_plan d WHERE status IN ('running','done') AND d.evidence IS NOT NULL AND starts<=? AND ends>? "
        "ORDER BY starts DESC,id LIMIT 40", (now,now-86400))
    for row in rows:
        members = (row["member"], *json.loads(row["partners"])) if row["phase"] == "shared" else (row["member"],)
        if all(g in names for g in members):
            description = fact(row, names, row['phase'])
            yield Seed(f"day:{row['id']}", "day", ("general",),
                       names[row["member"]] + " " + description[0].lower() + description[1:],
                       members, witnesses=members)


class DayRun:
    def __init__(self, realm, *, admission=True):
        self.admission = admission
        self.realm = realm
        self.app = realm.app
        self.store = self.app.store
        self.conn = self.store.conn
        self.advertised = False
        self.boot = 0
        self.epoch = 0
        self.sending = set()
        self.revokes = set()
        self.wakeup = asyncio.Event()
        self.due = {}
        with self.store.transaction():
            self.conn.execute("UPDATE day_handoff SET state='expired' WHERE state='waiting'")
        self.owners = {}
        self.listeners = {}
        self.cleanup_tasks = set()

    def adopt_hello(self, ev, epoch):
        self.boot = ev.started_ms
        self.advertised = FEATURE in ev.features
        self.epoch = epoch

    def configure(self, enabled):
        if self.admission == enabled:
            return
        self.admission = enabled
        if not enabled:
            for plan, in self.conn.execute(
                    "SELECT plan FROM day_permit WHERE state IN ('opening','open','verifying')").fetchall():
                self.close(plan, "skipped", "context_changed", notify=False)
        self.changed()

    def changed(self):
        before = self.wakeup
        self.wakeup = asyncio.Event()
        before.set()

    def rows(self, plan):
        return self.conn.execute("SELECT * FROM day_plan WHERE action_ref=? ORDER BY id",
                                 (f"day:{plan}",)).fetchall()

    def permit(self, plan):
        return self.conn.execute("SELECT * FROM day_permit WHERE plan=?", (plan,)).fetchone()

    def context_valid(self, *, cleanup=False):
        c = self.realm._forget_context() if cleanup else self.realm.action_context()
        return ((cleanup or c.enabled and self.admission and self.advertised) and c.connected and c.presence_known
                and self.realm._transport_epoch == self.epoch and bool(self.boot))

    def reason(self, rows):
        now = self.app.clock.now()
        guids = {r["member"] for r in rows}
        if guids & pulled_into_play(self.app):
            guild = self.app.guild
            for part, code in (('offline', 'offline_raid'), ('raid_night', 'raid')):
                controller = getattr(guild, part, None)
                if controller is not None and guids & controller.members_in_play():
                    return code
            return 'his_group'
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

    def close(self, plan, status, reason, *, notify=True):
        owner = f"day:{plan}"
        with self.store.transaction():
            self.conn.execute("UPDATE day_plan SET status=CASE WHEN ?='done' AND evidence IS NULL "
                              "THEN 'skipped' ELSE ? END,reason=? WHERE action_ref=? AND "
                              "status IN ('planned','running')", (status,status,reason,owner))
            changed = self.conn.execute("UPDATE day_permit SET state='revoking',reason=? WHERE plan=? "
                              "AND state NOT IN ('closed','revoking','unresolved')", (reason,plan))
        unsubscribe = self.listeners.pop(plan, None)
        if unsubscribe is not None:
            unsubscribe()
        self.realm.queue.drop_owner(owner)
        self.realm.bodies.release(owner)
        permit = self.permit(plan)
        if (changed.rowcount and notify and permit["token"] and permit["boot"] == self.boot
                and self.context_valid(cleanup=True)):
            self.send_control(permit, self.revoke_command(permit))
            self.due[plan] = self.app.clock.now() + 2

    @staticmethod
    def revoke_command(permit):
        return DayRevoke(uuid4().hex, permit["begin_id"], permit["reason"] or "ended",
                         permit["token"], int(permit["serial"]), permit["boot"])

    def send_control(self, permit, command):
        plan = permit["plan"]
        sending = self.revokes if isinstance(command, DayRevoke) else self.sending
        if plan in sending:
            return
        sending.add(plan)
        writer_epoch = self.epoch
        async def send():
            if isinstance(command, DayBegin) and (not self.admission or self.permit(plan)["state"] != "opening"):
                # Only this spawned writer can send this begin ID. No permit exists on native.
                self.close(plan, "skipped", "context_changed", notify=False)
                with self.store.transaction():
                    self.conn.execute("UPDATE day_permit SET state='closed' WHERE plan=? AND token IS NULL", (plan,))
                self.changed()
                return
            ack = await self.app.bridge.send(command, preserve_id=True, expected_epoch=writer_epoch)
            if ack is None or ack.id != command.id:
                return  # Only this control's reply can establish a definite refusal.
            if isinstance(command, DayBegin) and not ack.ok:
                self.close(plan, "skipped", "admission_refused")
                definite = ack.error in {'day_off', 'day_scope', 'day_capacity'} or ack.error.startswith('busy:')
                with self.store.transaction():
                    self.conn.execute("UPDATE day_permit SET state=? WHERE plan=? AND token IS NULL",
                                      ('closed' if definite else 'unresolved', plan))
                self.changed()
            elif isinstance(command, DayStatus) and not ack.ok and ack.error == 'day_unknown':
                self.close(plan, "skipped", "action_unproved")
                with self.store.transaction():
                    state = ("closed" if self.realm.action_store.day_previous_boot_resolved(permit, never_dispatched=True)
                             else "unresolved")
                    self.conn.execute("UPDATE day_permit SET state=? WHERE plan=? AND state!='closed'", (state, plan))
                self.changed()
        if not self.realm.enabled and not isinstance(command, DayBegin) and self.context_valid(cleanup=True):
            if len(self.cleanup_tasks) >= 20:
                sending.discard(plan)
                return
            context = self.realm._forget_context()
            async def cleanup():
                if self.realm._forget_context() == context:
                    await send()
            task = asyncio.create_task(cleanup())
            self.cleanup_tasks.add(task)
            def finished(done):
                self.cleanup_tasks.discard(done)
                if not done.cancelled():
                    done.exception()  # An uncertain transport retains the permit for a later query.
            task.add_done_callback(finished)
        else:
            task = self.realm.spawn(send)
        if task is None:
            sending.discard(plan)
        else:
            task.add_done_callback(lambda _: sending.discard(plan))

    def observe(self, ev):
        if not self.context_valid(cleanup=True) or self.realm._delivery_epoch(ev) != self.epoch:
            return
        permit = self.conn.execute("SELECT * FROM day_permit WHERE begin_id=?", (ev.origin_id,)).fetchone()
        if not permit or not re.fullmatch("[a-f0-9]{32}", ev.permit) or ev.serial <= 0:
            return
        plan = permit["plan"]
        rows = self.rows(plan)
        c = self.realm.action_context()
        if (not c.presence_known or (ev.guild,ev.human) != (c.guild,c.human) or
                tuple(ev.members) != tuple(sorted(r["member"] for r in rows))
                or (permit["guild"],permit["human"]) != (c.guild,c.human)
                or ev.started_ms != self.boot or permit["boot"] != self.boot or (permit["token"] is not None and
                (permit["token"],permit["serial"]) != (ev.permit,str(ev.serial)))):
            return
        if ((ev.state == 'open' and ev.reason) or (ev.state != 'open' and ev.reason not in NATIVE_REASONS)
                or ev.state not in {'open', 'revoking', 'closed', 'unresolved'} or permit['state'] == 'closed'):
            return
        if ev.state == "open" and permit["state"] in {"revoking","unresolved"}:
            state = permit["state"]
        else:
            state = ev.state
        if (len(ev.released) != len(set(ev.released)) or len(ev.released) > 4
                or any(not re.fullmatch("[a-f0-9]{32}", ident) for ident in ev.released)
                or len(ev.succeeded) != len(set(ev.succeeded)) or not set(ev.succeeded) <= set(ev.released)):
            return
        known = {r[0] for r in self.conn.execute("SELECT id FROM day_step WHERE plan=?", (plan,))}
        previous_success = set(json.loads(permit["succeeded"]))
        if not set(ev.released) <= known or (ev.state == "closed" and not previous_success <= set(ev.succeeded)):
            return
        if ev.state != "open":
            if ev.reason in REASONS:
                self.close(plan, "preempted", ev.reason, notify=False)
            elif permit["state"] in {"opening", "open", "verifying"}:
                status = "done" if ev.reason == "ended" else "skipped"
                self.close(plan, status, ev.reason, notify=False)
        with self.store.transaction():
            succeeded = sorted(set(json.loads(permit["succeeded"])) | set(ev.succeeded))
            self.conn.execute("UPDATE day_permit SET token=?,serial=?,state=?,succeeded=? WHERE plan=?",
                              (ev.permit, str(ev.serial), state, json.dumps(succeeded), plan))
            if state == "closed":
                released = json.dumps(sorted(ev.released))
                if permit["release_ids"] is not None and released != permit["release_ids"]:
                    raise ValueError("contradictory day seal")
                self.conn.execute("UPDATE day_permit SET release_ids=? WHERE plan=?", (released, plan))
                if not self.realm.action_store.day_released(plan, self.app.clock.now()):
                    self.conn.execute("UPDATE day_permit SET state='unresolved' WHERE plan=?", (plan,))
                    self.changed()
                    return
        self.changed()
        if state == "closed":
            self.realm.bodies.release(f"day:{plan}")
            self.due.pop(plan,None)

    async def on_event(self, ev):
        if isinstance(ev, Hello):
            if self.realm._delivery_epoch(ev) != self.realm._transport_epoch:
                return
            self.adopt_hello(ev, self.realm._delivery_epoch(ev) or 0)
            c = self.realm.action_context()
            if c.presence_known:
                for p in self.conn.execute("SELECT * FROM day_permit WHERE state!='closed'").fetchall():
                    if (p["guild"], p["human"]) != (c.guild, c.human):
                        continue
                    if (p["process"], p["epoch"]) != (c.process_id, str(c.connection_epoch)):
                        if p["state"] in {"opening", "open", "verifying"}:
                            self.close(p["plan"], "skipped", "context_changed", notify=False)
                        if p["boot"] != self.boot:
                            resolved = self.boot and self.realm.action_store.day_previous_boot_resolved(p)
                            state = "closed" if resolved else "unresolved"
                            with self.store.transaction():
                                self.conn.execute("UPDATE day_permit SET state=? WHERE plan=?", (state, p["plan"]))
                        self.due.pop(p["plan"], None)
                self.changed()
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
                    holders = self.realm.bodies.holders_of(guids)
                    free = all(holders.get(g, owner) is None for g in guids)
                    claimed = self.realm.bodies.claim(owner, guids, rank="realm_day", until=row["ends"]) if free else set()
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
                context = self.realm.action_context()
                self.conn.execute("INSERT INTO day_permit(plan,begin_id,boot,guild,human,process,epoch,state) "
                                  "VALUES(?,?,?,?,?,?,?,'opening')",
                                  (plan, uuid4().hex, self.boot, context.guild, context.human,
                                   context.process_id, str(context.connection_epoch)))
        if why or row["activity"] == "offline":
            return
        def preempted(_members):
            reason = self.reason(rows)
            holders = self.realm.bodies.holders_of(_members) if not reason and _members else {}
            if not reason and any(holder in {'raid', 'offline_raid'} for holder in holders.values()):
                return  # A tentative roster hold fences dispatch. Its validated request owns revocation.
            reason = reason or 'held'
            if self.permit(plan)["state"] in {"opening", "open"}:
                self.close(plan, "preempted", reason)
        self.listeners[plan] = self.realm.bodies.on_preempt(owner, preempted)
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
        def ready():
            return not any(r["member"] in self.owners for r in rows) and self.permit(plan)["state"] != "verifying"

        try:
            self.realm.queue.submit(cmd, owner=f"day:{plan}", still_valid=valid, ready_to_send=ready)
        except (ActionRejected, ActionCapacity, ActionUnavailable, ValueError):
            self.close(plan, "skipped", "admission_refused")

    def record(self, plan, rows, ids, phase):
        now = self.app.clock.now()
        members = [r["member"] for r in rows]
        if phase == "shared":
            members = sorted({g for r in rows for g in (r["member"], *json.loads(r["partners"]))})
        names = {g:m.name for g,m in self.store.cast().items()}
        with self.store.transaction():
            p = self.permit(plan)
            if p["state"] != "open":
                return
            rows = [r for r in rows if self.conn.execute("SELECT phase FROM day_plan WHERE id=?",
                                                         (r["id"],)).fetchone()[0] not in (phase, "activity", "shared")]
            if not rows:
                return
            payload = {"members": members, "plan": plan, "actions": ids, "activity": rows[0]["activity"],
                       "place": rows[0]["place"]}
            payload["phase"] = phase
            event = self.conn.execute("INSERT INTO game_event(ts,type,guid,payload) VALUES(?,'day_activity',?,?)",
                                      (now, members[0], json.dumps(payload))).lastrowid
            for r in rows:
                memory = reflection_row(r["member"],fact(r,names,phase), now=now, actors=members,
                    evidence=(f"event:{event}",), key=f"day:{r['id']}", importance=3,valence=.2)
                self.app.writer.put(replace(memory,kind="event",source="game",place=r["place"]))
                self.conn.execute("UPDATE day_plan SET status='running',evidence=?,phase=? WHERE id=?",
                                  (event, phase, r["id"]))
            self.conn.execute("UPDATE day_permit SET evidence=? WHERE plan=?", (event, plan))

    def advance(self, permit, now):
        plan = permit["plan"]
        rows = self.rows(plan)
        row = rows[0]
        state = permit["state"]
        c = self.realm.action_context()
        if (self.boot and self.boot != permit["boot"] and self.context_valid(cleanup=True)
                and (c.guild, c.human) == (permit["guild"], permit["human"])
                and self.realm.action_store.day_previous_boot_resolved(permit)):
            self.close(plan, "skipped", "context_changed", notify=False)
            with self.store.transaction():
                self.conn.execute("UPDATE day_permit SET state='closed' WHERE plan=?", (plan,))
            self.changed()
            return
        if (not self.admission or not self.realm.enabled) and state in ("opening", "open"):
            self.close(plan, "skipped", "context_changed")
            state = "revoking"
        if permit["release_ids"] is not None:
            with self.store.transaction():
                if self.realm.action_store.day_released(plan, now):
                    self.conn.execute("UPDATE day_permit SET state='closed' WHERE plan=?", (plan,))
                    self.due.pop(plan, None)
                    self.changed()
            return
        if state in ("opening","open"):
            if reason := self.reason(rows):
                self.close(plan, "preempted" if reason in REASONS else "skipped", reason)
                state = "revoking"
            elif now >= row["ends"]:
                self.close(plan, "done", "ended")
                state = "revoking"
        if state != "open":
            if now >= self.due.get(plan,0) and plan not in self.sending and self.context_valid(cleanup=True) and permit['boot'] == self.boot:
                self.due[plan] = now + (60 if state == 'unresolved' else 2)
                cmd = (self.revoke_command(self.permit(plan))
                       if state == "revoking" and permit["token"] else DayStatus(uuid4().hex, permit["begin_id"]))
                self.send_control(permit,cmd)
            return
        if any(r['member'] in self.owners for r in rows):
            return
        if permit['phase'] == 'activity' or not self.context_valid():
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
                if not self.realm.bodies.may_move_all(owner, guids, rank="realm_day"):
                    return
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
            if ident not in json.loads(permit["succeeded"]):
                if now >= self.due.get(plan, 0) and plan not in self.sending:
                    self.due[plan] = now + 5
                    self.send_control(permit, DayStatus(uuid4().hex, permit["begin_id"]))
                return
            if kind == "duel":
                self.record(plan, rows, [ident], "shared")
            else:
                phase = "arrival" if kind == "move" else "shared" if kind == "status" and len(rows) == 2 else "activity"
                self.record(plan, [r for r in rows if r["member"] == guid], [ident], phase)
        with self.store.transaction():
            self.conn.execute("UPDATE day_permit SET phase='activity' WHERE plan=?", (plan,))

    async def tick(self):
        now = self.app.clock.now()
        for p in self.conn.execute("SELECT * FROM day_permit WHERE state!='closed'").fetchall():
            self.advance(p,now)
        if not self.admission or not self.realm.enabled:
            return
        with self.store.transaction():
            self.conn.execute("UPDATE day_plan SET status='skipped',reason='expired' WHERE status='planned' "
                              "AND action_ref IS NULL AND ends<=?", (now,))
        for r in self.conn.execute("SELECT * FROM day_plan WHERE status='planned' AND action_ref IS NULL "
                                   "AND starts<=? AND ends>? ORDER BY starts,id LIMIT 40", (now,now)).fetchall():
            occupied = self.conn.execute("SELECT count(*) FROM day_plan d JOIN day_permit p ON "
                                         "d.action_ref='day:'||p.plan WHERE p.state!='closed'").fetchone()[0]
            self.start(r,now,occupied)

    async def shutdown(self):
        for task in self.cleanup_tasks:
            task.cancel()
        if self.cleanup_tasks:
            await asyncio.gather(*self.cleanup_tasks, return_exceptions=True)

    def inspection_origins(self,members):
        rows = self.conn.execute("SELECT DISTINCT p.begin_id,p.token,p.boot FROM day_permit p "
            "JOIN day_plan d ON d.action_ref='day:'||p.plan WHERE p.state!='closed' AND d.member IN "
            "(SELECT value FROM json_each(?)) ORDER BY p.begin_id",(json.dumps(sorted(members)),)).fetchall()
        return tuple(r[0] for r in rows) if all(r[1] and r[2]==self.boot for r in rows) else None

    async def handoff(self, command, members, reason, valid, send):
        """Submit the original owner action once, after overlapping native permits report closed."""
        request_id = command.id
        targets = set(members)
        manifest = json.dumps(sorted(targets))
        if any(g in self.owners for g in targets):
            raise ActionRejected("day_takeover_busy")
        blocks = self.conn.execute("SELECT DISTINCT p.plan FROM day_permit p JOIN day_plan d ON "
            "d.action_ref='day:'||p.plan WHERE p.state!='closed' AND d.member IN "
            "(SELECT value FROM json_each(?))", (manifest,)).fetchall()
        if self.conn.execute("SELECT 1 FROM day_handoff WHERE request_id=?", (request_id,)).fetchone():
            raise ActionRejected("day_takeover_duplicate")
        if not valid():
            raise ActionRejected("day_takeover_expired")
        context = self.realm.action_context()
        heroics = getattr(getattr(self.app,"guild",None),"heroics",None)
        heroic = ""
        if heroics is not None and reason in {"raid","summon","string"}:
            from .heroics import overlapping
            heroic = overlapping(self.conn,targets,context.guild,context.human)
        starting = isinstance(command,StartMemberDungeon)
        if not blocks and not heroic and not starting:
            return await send(command)
        if not self.context_valid(cleanup=True):
            raise ActionRejected("day_takeover_expired")
        if any(self.permit(plan)["boot"] != self.boot for (plan,) in blocks):
            raise ActionRejected("day_cleanup_unresolved")
        self.owners.update((g, request_id) for g in targets)
        try:
            with self.store.transaction():
                self.conn.execute("INSERT INTO day_handoff VALUES(?,?,?,?,?,?,?,?,'waiting')",
                    (request_id, reason, manifest, context.process_id, str(context.connection_epoch),
                     context.guild, context.human, self.app.clock.now() + HANDOFF_SECONDS))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + HANDOFF_SECONDS
            expires = self.app.clock.now() + HANDOFF_SECONDS
            writer_epoch = self.epoch

            def current():
                return (loop.time() < deadline and self.app.clock.now() < expires and
                        self.realm.action_context() == context and self.epoch == writer_epoch and valid())

            async def wait_for_evidence():
                try:
                    await asyncio.wait_for(self.wakeup.wait(), min(1, deadline - loop.time()))
                except TimeoutError:
                    pass

            async def control(check):
                if not current():
                    raise ActionRejected("day_takeover_expired")
                def verify_permits():
                    if not getattr(check, "day_driver_ticket", ""):
                        with self.store.transaction():
                            self.conn.executemany("UPDATE day_permit SET state='verifying' WHERE plan=? AND state='open'",
                                                  blocks)

                try:
                    async with asyncio.timeout_at(deadline):
                        ack = await self.app.bridge.send(check, preserve_id=True, expected_epoch=writer_epoch)
                except NotConnected:
                    raise ActionRejected("no_link") from None
                except asyncio.CancelledError:
                    verify_permits()
                    raise
                except (BridgeError, TimeoutError, OSError):
                    ack = None
                if ack is None or ack.id != check.id:
                    # The admission check could have revoked its permits. No original write was entered.
                    verify_permits()
                    raise ActionRejected("day_check_unknown")
                return "" if ack.ok else check_refusal(check, ack.error)

            # The opening row is committed before its spawned DayBegin reaches the writer. Only
            # an authenticated DayState can introduce its origin to an owner check. A definite
            # refusal closes the row, so it needs no native handoff at all.
            while True:
                if not current():
                    raise ActionRejected("day_takeover_expired")
                active = [p for (plan,) in blocks if (p := self.permit(plan))["state"] != "closed"]
                missing = [p for p in active if not p["token"]]
                if not missing:
                    break
                if any(p["state"] == "unresolved" for p in missing):
                    raise ActionRejected("day_cleanup_unresolved")
                for p in missing:
                    self.advance(p, self.app.clock.now())
                await wait_for_evidence()

            if active or heroic or starting:
                origins = tuple(p["begin_id"] for p in active)
                binding = DayOwnerCheck(request_id, origins, int(expires * 1000), heroic_start=heroic,
                                        owner_guid=context.human if heroic else 0)
                check = replace(command, id=uuid4().hex, day_check=binding)
                refusal = await control(check)
                if refusal == "day_driver_required" and isinstance(command, StartOfflineRaid):
                    preparation = replace(check, id=uuid4().hex, day_driver_ticket=check.id)
                    refusal = await control(preparation)
                    if not refusal:
                        check = replace(check, id=uuid4().hex)
                        refusal = await control(check)
                if refusal:
                    raise ActionRejected(refusal)
                for p in active:
                    self.close(p["plan"], "preempted", reason, notify=False)
                command = replace(command, day_prepared=check.id)
            while True:
                active = [p for (plan,) in blocks if (p := self.permit(plan))["state"] != "closed"]
                retained = heroic and overlapping(self.conn,targets,context.guild,context.human)==heroic
                if not active and not retained:
                    break
                if (not current() or any(p["state"] == "unresolved" and p["release_ids"] is None for p in active)):
                    raise ActionRejected("day_cleanup_unresolved")
                if retained:
                    try:
                        async with asyncio.timeout_at(deadline):
                            await heroics.status(heroic)
                    except (BridgeError, OSError, TimeoutError):
                        raise ActionRejected("dungeon_cleanup_unresolved") from None
                    if not active and not overlapping(self.conn,targets,context.guild,context.human):
                        break
                await wait_for_evidence()
            if not current():
                raise ActionRejected("day_takeover_expired")
            with self.store.transaction():
                self.conn.execute("UPDATE day_handoff SET state='sent' WHERE request_id=?", (request_id,))
            try:
                return await send(command, expected_epoch=writer_epoch)
            except NotConnected:
                # BridgeClient checks the pinned writer immediately before write().
                with self.store.transaction():
                    self.conn.execute("UPDATE day_handoff SET state='expired' WHERE request_id=?", (request_id,))
                raise ActionRejected("no_link") from None
        finally:
            with self.store.transaction():
                self.conn.execute("UPDATE day_handoff SET state='expired' WHERE request_id=? AND state='waiting'", (request_id,))
            for g in targets:
                self.owners.pop(g,None)
            self.realm.queue.wake()
