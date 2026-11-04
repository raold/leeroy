"""The guild's calendar: what the guild posts for the week, who posts it, and who signs up.

What is posted (realm_calendar.kind), by whom (a title word), and when:

  raid         the standing raid lead          the period's raid nights, after the Tuesday 04:00
                                               reset: the weekdays he logged in on at least twice
                                               in the 28 days before it, at his median login hour,
                                               at most 4; Tuesday to Thursday at 20:00 without
                                               history. A night he then missed is updated after
                                               it: the day's offline raid, or no raid.
  offline      the raid-lead rotation, by load when an offline raid starts; its result once it
                                               has ended (calendar_update)
  birthday     events, social, historian, then  the week before a member's card birthday; a date
               the raid-lead rotation           with more than two shares one "Guild birthdays"
                                               event naming them all
  anniversary  the same                        the week before [realm] guild_anniversary, only
                                               when it is set (local data: never in a log)
  meeting      the standing raid lead          Monday (caught up until Wednesday), for Thursday
                                               at 21:00, inviting him

The raid-lead rotation is the standing raid lead (the one cast tank titled raid lead: the kick
guard's rule), then the others titled raid lead. A creator already holding 25 live events (its
realm_calendar rows within 30 days) passes the post on down its chain; native refuses at 30.

Every post, signup and update goes through the direct ledger (owner "calendar"), never the
body ledger, so his raid's hold never holds one back; native fences each. A post's ref is its
business identity (raid:<date>, meeting:<date>, birthday:<guid>:<year> or birthday:<date> for a shared date, anniversary:<year>,
offline:<command>): its realm_calendar row is written 'pending' in the same Store unit that
prepares it, and it is bound to native's event id from the post's job value ('posted'). A
refused or never-sent post is 'failed' and may be tried again after RETRY_S, by another holder
when there is one, MAX_TRIES times per ref. One that may have been done (unknown) is 'posted'
with no event id, like every row from before migration 0016: never posted again, never
updated, and nobody signs up for it.

Signups follow a schedule seeded by the event and the member, so it is the same after any
restart: 1-8 hours after the post, never after the event; a conscientiousness of 3 or less
signs up late or, half the time, tentative; a decline only for an event that does not need
him; at most 99 per event. Each is ref signup:<event>:<guid>; a realm_rsvp row (written once
native has done it, or may have) means it is never sent again. His own RSVPs come back as
calendar_rsvp events and are stored as realm_rsvp rows under his guid.

The desk works once a minute, synchronously, and spawns one owned child to send what it
prepared; anything prepared that never reached the barrier is abandoned when that child ends.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
import json
import logging
import random
import re
import statistics
import uuid

from leeroy_mind.guild.protocol import Job as GuildJob
from leeroy_mind.guild.raids import CATALOG

from .calendar_time import product_period
from .config import LIMITS
from .ledger import former_members_in_unit
from .protocol import CalendarPost, CalendarRsvp, CalendarSignup, CalendarUpdate, RealmJob

log = logging.getLogger(__name__)

OWNER = "calendar"
EVENTS = ("realm_job", "job", "calendar_rsvp")
TICK_S = 60
CREATOR_CAP = LIMITS.calendar_creator_live_cap  # 25; native refuses at 30.
LIVE_S = 30 * 86400
SIGNUP_CAP = 99  # Native's invites per event; his own invite takes one of them.
SIGNUP_MIN_S, SIGNUP_MAX_S = 3600, 8 * 3600
SIGNUP_LAST_S = 60  # Nobody signs up in the event's last minute.
SIGNUPS_PER_PASS = 12
POST_AHEAD_S = 900  # A night, meeting or birthday closer than this is not posted.
OFFLINE_AHEAD_S = 120  # An offline raid's entry starts just after its start: native refuses the past.
RETRY_S = 600
MAX_TRIES = 3
HISTORY_S = 28 * 86400
NIGHTS_MAX = 4
DEFAULT_NIGHTS = (1, 2, 3)  # Tuesday, Wednesday, Thursday (Monday is 0).
NIGHT_HOUR = 20
MEETING_DAY, MEETING_HOUR = 3, 21  # Thursday 21:00.
MEETING_LAST_POST_DAY = 2  # Posted on Monday, caught up until Wednesday; never on its own day.
WEEK_BEFORE_DAYS = 7
EVENT_HOUR = 20  # Birthdays and the anniversary.
BIRTHDAYS_SINGLE = 2  # More birthdays than this on one date share one combined event.
MISSED_AFTER_S = 4 * 3600  # A night he missed is updated this long after it began...
MISSED_UNTIL_S = 2 * 86400  # ...and not after this.
CAME_BEFORE_S, CAME_AFTER_S = 3600, 3 * 3600
DAY_S = 12 * 3600
DECLINE_ODDS = 0.15
SALT = "realm-calendar-v1"
LEAD_TITLE = "raid lead"
SOCIAL_TITLES = ("events", "social", "historian")
SIGNUP_KINDS = ("raid", "meeting", "birthday", "anniversary")
NEEDS_HIM = frozenset({"raid", "meeting"})
OFFLINE_KEY = "guild:offline:owned"
_LIVE = frozenset({"prepared", "dispatched", "acked", "unknown"})
_ID = re.compile(r"[0-9a-f]{32}")
_STATUS = re.compile(r"[a-z_]{1,32}")
_U32 = 0xFFFFFFFF
_I64 = (1 << 63) - 1
_PLANS_MAX = 8192


@dataclass(frozen=True, slots=True)
class Post:
    kind: str
    ref: str
    at: int
    title: str
    event_type: str
    chain: tuple[int, ...]
    by_load: bool = False
    invite_human: bool = False
    description: str = ""


def _wipes(n: int) -> str:
    return "no wipes" if n == 0 else "1 wipe" if n == 1 else f"{n} wipes"


def result_text(raid_key, kills, wipes, failed) -> str:
    """An offline raid's result as the calendar says it: "cleared, 2 wipes"."""
    raid = CATALOG.get(raid_key) if type(raid_key) is str else None
    if raid is None:
        text = f"{kills} kills, {_wipes(wipes)}"
    elif kills >= raid.bosses:
        text = f"cleared, {_wipes(wipes)}"
    else:
        text = f"{kills} of {raid.bosses} bosses, {_wipes(wipes)}"
    return text + (", called off" if failed else "")


def signup_plan(kind, event_id, guid, conscientiousness, posted_at, at) -> tuple[float, str] | None:
    """When (Unix seconds) and how one member signs up for one posted event: the same every
    time, seeded by the event and the member. None when it is too late to sign up at all."""
    end = min(posted_at + SIGNUP_MAX_S, at - SIGNUP_LAST_S)
    if end <= posted_at:
        return None
    start = min(posted_at + SIGNUP_MIN_S, posted_at + (end - posted_at) / 2)
    rng = random.Random(f"{SALT}:{event_id}:{guid}")
    coin, when, decline = rng.random(), rng.random(), rng.random()
    status = "signed_up"
    if conscientiousness <= 3:
        if coin < 0.5:
            status = "tentative"
        else:
            start += (end - start) * 0.75  # Late: in the last quarter.
    if kind not in NEEDS_HIM and decline < DECLINE_ODDS:
        status = "declined"
    return start + (end - start) * when, status


def _rank(event_id, guid) -> str:
    """Who gets one of an event's 99 places first: stable as the audience changes."""
    return hashlib.sha256(f"{SALT}:who:{event_id}:{guid}".encode("ascii")).hexdigest()


def _on_or_after(mmdd, today, days):
    """The next date of a valid "MM-DD" within `days` of today, or None. A leap day only
    happens in leap years."""
    if type(mmdd) is not str or re.fullmatch(r"[0-9]{2}-[0-9]{2}", mmdd) is None:
        return None
    for year in (today.year, today.year + 1):
        try:
            day = date(year, int(mmdd[:2]), int(mmdd[3:]))
        except ValueError:
            continue
        if 0 <= (day - today).days <= days:
            return day
    return None


def _clip(text, limit):
    data = text.encode("utf-8")[:limit]
    return data.decode("utf-8", "ignore")


class CalendarDesk:
    """The Realm part that keeps the guild's calendar. Synchronous apart from its sends."""

    name = "calendar"

    def __init__(self, realm):
        self.realm = realm
        self._next = None
        self._sending = None
        self._tries = {}  # ref -> (tries, not before): a refused signup's or update's pacing.
        self._plans = {}
        self._noted = set()
        self._failing = {}  # step -> the error it last failed with.

    # -- what it reads ----------------------------------------------------------------------

    @property
    def _store(self):
        return self.realm.app.store

    @property
    def _clock(self):
        return self.realm.app.clock

    @property
    def _ports(self):
        return self.realm.ports

    def _ledger(self):
        return self.realm.part("direct")

    @staticmethod
    def _ready(context) -> bool:
        return context.enabled is True and context.connected is True and context.presence_known is True

    def _human(self) -> int:
        guid = self._ports.human().guid
        return guid if type(guid) is int and 0 <= guid <= _U32 else 0

    def _note(self, event, key):
        if (event, key) not in self._noted:
            self._noted.add((event, key))
            log.info("%s kind=%s", event, key)

    def his_rsvp(self, event_id) -> str | None:
        """His latest answer to an event's invite, as native named it; None without one."""
        human = self._human()
        row = self._store.conn.execute("SELECT status FROM realm_rsvp WHERE event=? AND guid=?",
                                       (event_id, human)).fetchone() if human else None
        return None if row is None else row[0]

    def event_of(self, ref) -> int | None:
        """The native event id the post of `ref` was bound to, when it was."""
        row = self._store.conn.execute(
            "SELECT event_id FROM realm_calendar WHERE ref=? AND state='posted' AND event_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1", (ref,)).fetchone()
        return None if row is None else row[0]

    # -- the Realm part ---------------------------------------------------------------------

    async def tick(self):
        now = self._clock.now()
        if self._next is not None and now < self._next:
            return
        context = self.realm.action_context()
        if not self._ready(context) or self._ledger() is None:
            return
        self._next = now + TICK_S
        prepared = []
        try:
            self._reconcile(now)
        except Exception as error:  # Never the Realm loop's failure.
            log.warning("realm_calendar_failed step=reconcile error=%s", type(error).__name__)
        if self._sending is None or self._sending.done():
            self._pass(now, context, prepared)
        self._send(prepared)

    async def on_event(self, ev):
        if not self._ready(self.realm.action_context()) or self._ledger() is None:
            return
        try:
            if type(ev) is CalendarRsvp:
                self._his_rsvp(ev)
            elif type(ev) is RealmJob and ev.kind == "calendar_post":
                self._reconcile(self._clock.now())  # The ledger took its value: bind it now.
            elif type(ev) is GuildJob and ev.kind == "offline_raid":
                self._offline_job(ev)
        except Exception as error:
            log.warning("realm_calendar_event_failed: %s", type(error).__name__)

    # -- the pass ---------------------------------------------------------------------------

    def _pass(self, now, context, out):
        """Posts, then updates, then signups: one failing never stops the others."""
        for name, step in (("posts", self._posts), ("updates", self._updates), ("signups", self._signups)):
            try:
                step(now, context, out)
            except Exception as error:  # Never the Realm loop's failure; said once until it mends.
                if self._failing.get(name) != type(error).__name__:
                    self._failing[name] = type(error).__name__
                    log.warning("realm_calendar_failed step=%s error=%s", name, type(error).__name__)
            else:
                self._failing.pop(name, None)

    def _former(self, human):
        with self._store.transaction():
            return former_members_in_unit(self._store, human_guid=human)

    def _posts(self, now, context, out):
        former = self._former(self._human())
        for post in self._due_posts(now, self._chains(former), former):
            cmd = self._prepare_post(post, now, context)
            if cmd is not None:
                out.append(cmd)

    def _send(self, prepared):
        """One owned child sends what this pass prepared, in order. Whatever never reached the
        ledger's barrier (cancelled, disabled, not started) is abandoned when it ends."""
        if not prepared:
            return
        ledger = self._ledger()

        def close(_task=None):
            for cmd in prepared:
                try:
                    ledger.abandon_unsent(cmd)
                except Exception as error:  # Startup recovery abandons a prepared row anyway.
                    log.warning("realm_calendar_close_failed: %s", type(error).__name__)
            try:  # Acks came in during the sends: a refused post's row is free for its retry.
                self._reconcile(self._clock.now())
            except Exception as error:
                log.warning("realm_calendar_failed step=reconcile error=%s", type(error).__name__)

        async def send():
            for cmd in prepared:
                try:
                    await self._ports.send(cmd)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # The ledger row records the outcome; never resent.
                    log.debug("realm_calendar_unsent: %s", type(error).__name__)

        task = self.realm.spawn(send)
        if task is None:
            close()
            return
        task.add_done_callback(close)
        self._sending = task

    # -- who posts --------------------------------------------------------------------------

    def _chains(self, former):
        """(the raid-lead rotation, the social chain), never him or a former member."""
        human = self._human()
        lead = self.realm.guard.standing_lead(human)
        rotation = ([lead] if lead else []) + sorted(self._ports.by_title(LEAD_TITLE))
        social = [g for word in SOCIAL_TITLES for g in sorted(self._ports.by_title(word))] + rotation

        def clean(chain):
            seen = []
            for guid in chain:
                if type(guid) is int and 0 < guid <= _U32 and guid != human and guid not in former \
                        and guid not in seen:
                    seen.append(guid)
            return tuple(seen)
        return clean(rotation), clean(social)

    def _load(self, guid, now) -> int:
        return self._store.conn.execute(
            "SELECT COUNT(*) FROM realm_calendar WHERE creator=? AND state IN ('pending','posted') "
            "AND at>=?", (guid, now - LIVE_S)).fetchone()[0]

    def _pick(self, post, now, failed):
        """The post's creator: the first in its chain under the cap (the least loaded with
        by_load), skipping those it already failed with while anyone else is open."""
        loads = {guid: self._load(guid, now) for guid in post.chain}
        open_ = [guid for guid in post.chain if loads[guid] < CREATOR_CAP]
        fresh = [guid for guid in open_ if guid not in failed] or open_
        if not fresh:
            return None
        if post.by_load:
            return min(fresh, key=lambda guid: (loads[guid], fresh.index(guid)))
        return fresh[0]

    # -- posts ------------------------------------------------------------------------------

    def _local(self, day, hour) -> int:
        tz = self._clock.local().tzinfo
        return int(datetime.combine(day, time(hour), tzinfo=tz).timestamp())

    def raid_nights(self, now) -> list[datetime]:
        """The raid nights of the product period holding `now`, from his logins before it."""
        tz = self._clock.local().tzinfo
        period = product_period(now)
        start_day = date.fromisoformat(period.key)  # The reset's Tuesday.
        days = {}
        for ts in self._ports.his_logins(period.starts_at - HISTORY_S):
            if ts < period.starts_at:
                local = datetime.fromtimestamp(ts, tz)
                days.setdefault(local.weekday(), {}).setdefault(local.date(), local.hour)
        chosen = [day for day, dates in days.items() if len(dates) >= 2]
        if chosen:
            chosen = sorted(chosen, key=lambda day: (-len(days[day]), (day - 1) % 7))[:NIGHTS_MAX]
            hour = statistics.median_low([h for day in chosen for h in days[day].values()])
        else:
            chosen, hour = DEFAULT_NIGHTS, NIGHT_HOUR
        return sorted(datetime.combine(start_day + timedelta(days=(day - 1) % 7), time(hour), tzinfo=tz)
                      for day in chosen)

    def _due_posts(self, now, chains, former):
        rotation, social = chains
        posts = []
        for night in self.raid_nights(now):
            posts.append(Post("raid", f"raid:{night.date().isoformat()}", int(night.timestamp()),
                              "Raid night", "raid", rotation))
        local = self._clock.local()
        today = local.date()
        if local.weekday() <= MEETING_LAST_POST_DAY:
            thursday = today + timedelta(days=MEETING_DAY - local.weekday())
            posts.append(Post("meeting", f"meeting:{thursday.isoformat()}", self._local(thursday, MEETING_HOUR),
                              "Officer meeting", "meeting", rotation, invite_human=True))
        birthdays = {}  # day -> [(guid, character name)]
        for member in self._ports.cast():
            persona = self._ports.persona(member.guid) if member.guid not in former else None
            day = _on_or_after(persona.real_life.birthday, today, WEEK_BEFORE_DAYS) if persona else None
            if day is not None:
                birthdays.setdefault(day, []).append((member.guid, persona.character_name))
        for day, people in birthdays.items():
            at = self._local(day, EVENT_HOUR)
            if len(people) <= BIRTHDAYS_SINGLE:
                for guid, name in people:
                    posts.append(Post("birthday", f"birthday:{guid}:{day.year}", at,
                                      _clip(f"{name}'s birthday", 64), "other", social))
            else:  # A crowded day is one event naming everyone, not one per member.
                names = ", ".join(name for _, name in sorted(people))
                posts.append(Post("birthday", f"birthday:{day.isoformat()}", at, "Guild birthdays",
                                  "other", social, description=_clip(names, 255)))
        day = _on_or_after(getattr(self.realm.cfg, "guild_anniversary", ""), today, WEEK_BEFORE_DAYS)
        if day is not None:
            posts.append(Post("anniversary", f"anniversary:{day.year}", self._local(day, EVENT_HOUR),
                              "Guild anniversary", "other", social))
        return [post for post in posts if post.at > now + POST_AHEAD_S]

    def _prepare_post(self, post, now, context):
        """Prepare the post of a ref that has none live, and its 'pending' row, in one unit."""
        store, ledger = self._store, self._ledger()
        with store.transaction():
            rows = store.conn.execute("SELECT state,creator,updated FROM realm_calendar WHERE ref=?",
                                      (post.ref,)).fetchall()
            if any(row["state"] in ("pending", "posted") for row in rows):
                return None
            failed = [row for row in rows if row["state"] == "failed"]
            if len(failed) >= MAX_TRIES or (failed and now < max(row["updated"] for row in failed) + RETRY_S):
                return None
            creator = self._pick(post, now, {row["creator"] for row in failed})
            if creator is None:
                self._note("realm_calendar_no_creator", post.kind)
                return None
            cmd = CalendarPost(uuid.uuid4().hex, creator, post.title, description=post.description, event_type=post.event_type,
                               at=post.at, invite_human=post.invite_human)
            if ledger.prepare(cmd, owner=OWNER, ref=post.ref, context=context, expects_job=True) is None:
                return None
            store.conn.execute(
                "INSERT INTO realm_calendar(kind,creator,at,title,ref,updated,command_id,state) "
                "VALUES (?,?,?,?,?,?,?,'pending')",
                (post.kind, creator, post.at, post.title, post.ref, now, cmd.id))
        log.info("realm_calendar_post kind=%s", post.kind)
        return cmd

    def _reconcile(self, now):
        """Settle each pending row from its post's ledger state."""
        store, ledger = self._store, self._ledger()
        rows = store.conn.execute("SELECT id,ref FROM realm_calendar WHERE state='pending' "
                                  "AND command_id IS NOT NULL ORDER BY id").fetchall()
        for row in rows:
            settled = ledger.state(OWNER, row["ref"])
            state, value = (settled[0], settled[2]) if settled is not None else ("missing", None)
            if state in ("prepared", "dispatched") or (state == "acked" and value is None):
                continue
            with store.transaction():
                if state == "acked" and type(value) is int and 0 < value <= _I64:
                    old = store.conn.execute("SELECT id FROM realm_calendar WHERE event_id=? AND id<>?",
                                             (value, row["id"])).fetchone()
                    if old is not None:  # Native gave a freed id again: the old event is gone.
                        store.conn.execute("UPDATE realm_calendar SET event_id=NULL WHERE id=?", (old["id"],))
                        store.conn.execute("DELETE FROM realm_rsvp WHERE event=?", (value,))
                    store.conn.execute("UPDATE realm_calendar SET state='posted',event_id=?,updated=? "
                                       "WHERE id=? AND state='pending'", (value, now, row["id"]))
                elif state in ("acked", "unknown"):
                    # Done or maybe done, with no usable id: never posted again, never followed.
                    store.conn.execute("UPDATE realm_calendar SET state='posted',updated=? "
                                       "WHERE id=? AND state='pending'", (now, row["id"]))
                else:
                    store.conn.execute("UPDATE realm_calendar SET state='failed',updated=? "
                                       "WHERE id=? AND state='pending'", (now, row["id"]))

    # -- offline raids ----------------------------------------------------------------------

    def _owned(self):
        row = self._store.conn.execute("SELECT value FROM meta WHERE key=?", (OFFLINE_KEY,)).fetchone()
        try:
            value = json.loads(row[0]) if row is not None else None
        except ValueError:
            return None
        return value if type(value) is dict else None

    def _offline_job(self, ev):
        """The guild's own offline raid: its entry when it is running; its result is read
        from the guild's records at the next pass once it has ended."""
        owned = self._owned()
        if type(ev.id) is not str or not _ID.fullmatch(ev.id) or owned is None or owned.get("id") != ev.id:
            return
        if ev.state in ("done", "failed"):
            self._next = None
            return
        if ev.state != "progress" or ev.stage != "running":
            return
        plan = owned.get("plan") if type(owned.get("plan")) is dict else {}
        raid = CATALOG.get(plan.get("raid_key")) if type(plan.get("raid_key")) is str else None
        size = plan.get("size") if type(plan.get("size")) is int else 0
        title = (f"{raid.name} {size}{' heroic' if plan.get('heroic') is True else ''}, offline"
                 if raid is not None and size else "Offline raid")
        now = self._clock.now()
        rotation, _ = self._chains(self._former(self._human()))
        post = Post("offline", f"offline:{ev.id}", int(now) + OFFLINE_AHEAD_S, _clip(title, 64), "raid",
                    rotation, by_load=True)
        cmd = self._prepare_post(post, now, self.realm.action_context())
        if cmd is not None:
            self._send([cmd])

    def _offline_result(self, raid):
        """(raid key, kills, wipes, failed) of an ended offline raid, from the guild's records."""
        owned = self._owned()
        if owned is not None and owned.get("id") == raid and type(owned.get("terminal")) is dict:
            terminal, plan = owned["terminal"], owned.get("plan") or {}
            kills, wipes = terminal.get("kills"), terminal.get("wipes")
            if type(kills) is int and type(wipes) is int:
                return plan.get("raid_key"), kills, wipes, terminal.get("state") == "failed"
        for (payload,) in self._store.conn.execute(
                "SELECT payload FROM game_event WHERE type='guild_offline_raid' ORDER BY id DESC LIMIT 64"):
            try:
                result = json.loads(payload)
            except ValueError:
                continue
            if type(result) is dict and result.get("command") == raid:
                key = self._store.conn.execute("SELECT raid_key FROM guild_raid WHERE raid_id=?",
                                               (result.get("raid_id"),)).fetchone()
                kills, wipes = result.get("kills"), result.get("wipes")
                if type(kills) is int and type(wipes) is int:
                    return (key[0] if key else None), kills, wipes, False
        return None

    # -- updates ----------------------------------------------------------------------------

    def _may_try(self, ref, now, settled) -> bool:
        if settled is not None and settled[0] in _LIVE:
            return False
        tries, not_before = self._tries.get(ref, (0, 0.0))
        return tries < MAX_TRIES and now >= not_before

    def _tried(self, ref, now):
        if len(self._tries) > 4096:
            self._tries.clear()
        tries, _ = self._tries.get(ref, (0, 0.0))
        self._tries[ref] = (tries + 1, now + RETRY_S)

    def _night_text(self, at):
        """What a raid night he did not come to turned into, or None (he came, or the day's
        offline raid is still running)."""
        conn = self._store.conn
        if conn.execute("SELECT 1 FROM session WHERE started<=? AND (ended IS NULL OR ended>=?) LIMIT 1",
                        (at + CAME_AFTER_S, at - CAME_BEFORE_S)).fetchone() is not None:
            return None
        raid = conn.execute("SELECT raid_key,kills,wipes,ended FROM guild_raid WHERE offline=1 AND started>=? "
                            "AND started<? ORDER BY started DESC LIMIT 1", (at - DAY_S, at + DAY_S)).fetchone()
        if raid is None:
            return "no raid"
        if raid["ended"] is None:
            return None
        name = CATALOG[raid["raid_key"]].name if raid["raid_key"] in CATALOG else "a raid"
        return f"night off: {name} offline, {result_text(raid['raid_key'], raid['kills'], raid['wipes'], False)}"

    def _updates(self, now, context, out):
        ledger = self._ledger()
        rows = self._store.conn.execute(
            "SELECT id,kind,ref,at,event_id FROM realm_calendar WHERE state='posted' AND event_id IS NOT NULL "
            "AND kind IN ('offline','raid') AND at>=? ORDER BY id", (now - MISSED_UNTIL_S,)).fetchall()
        for row in rows:
            ref = f"update:{row['id']}"
            settled = ledger.state(OWNER, ref)
            if not self._may_try(ref, now, settled):
                continue
            if row["kind"] == "offline":
                result = self._offline_result(row["ref"][len("offline:"):])
                text = result_text(*result) if result is not None else None
            else:
                text = self._night_text(row["at"]) if now >= row["at"] + MISSED_AFTER_S else None
            if not text:
                continue
            cmd = CalendarUpdate(uuid.uuid4().hex, row["event_id"], description=_clip(text, 255))
            if ledger.prepare(cmd, owner=OWNER, ref=ref, context=context, expects_job=False) is not None:
                self._tried(ref, now)
                out.append(cmd)
                log.info("realm_calendar_update kind=%s", row["kind"])

    # -- signups ----------------------------------------------------------------------------

    def _plan(self, row, guid, persona):
        key = (row["id"], row["event_id"], guid)  # An id native gives again is another event.
        plan = self._plans.get(key)
        if plan is None and key not in self._plans:
            if len(self._plans) >= _PLANS_MAX:
                self._plans.clear()
            plan = self._plans[key] = signup_plan(row["kind"], row["event_id"], guid,
                                                  persona.big_five.conscientiousness.score,
                                                  row["updated"], row["at"])
        return plan

    def _signups(self, now, context, out):
        rows = self._store.conn.execute(
            f"SELECT id,kind,at,event_id,updated FROM realm_calendar WHERE state='posted' "
            f"AND event_id IS NOT NULL AND kind IN ({','.join('?' for _ in SIGNUP_KINDS)}) AND at>? "
            f"ORDER BY at,id", (*SIGNUP_KINDS, now)).fetchall()
        if not rows:
            return
        ledger, human, conn = self._ledger(), self._human(), self._store.conn
        former = self._former(human)
        away = self._ports.away()
        cast = {}
        for member in self._ports.cast():
            persona = self._ports.persona(member.guid)
            if persona is not None and member.guid not in former and member.guid not in away \
                    and member.guid != human:
                cast[member.guid] = persona
        officers = set(self._ports.officers())
        budget = SIGNUPS_PER_PASS
        for row in rows:
            event = row["event_id"]
            audience = sorted((g for g in cast if row["kind"] != "meeting" or g in officers),
                              key=lambda g: _rank(event, g))
            cap = SIGNUP_CAP - (1 if row["kind"] == "meeting" else 0)
            signed = {r[0] for r in conn.execute("SELECT guid FROM realm_rsvp WHERE event=? AND guid<>?",
                                                 (event, human))}
            taken, candidates = set(signed), []
            for guid in audience:
                if guid in signed:
                    continue
                plan = self._plan(row, guid, cast[guid])
                if plan is None or plan[0] > now:
                    continue
                ref = f"signup:{event}:{guid}"
                settled = ledger.state(OWNER, ref)
                if settled is not None and (settled[0] == "unknown" or (settled[0] == "acked" and settled[2] is not None)):
                    with self._store.transaction():  # Done, or maybe done: never sent again.
                        conn.execute("INSERT INTO realm_rsvp(event,guid,status,ts) VALUES (?,?,?,?) "
                                     "ON CONFLICT(event,guid) DO NOTHING", (event, guid, plan[1], now))
                    taken.add(guid)
                elif settled is not None and settled[0] in _LIVE:
                    taken.add(guid)  # On its way.
                elif self._may_try(ref, now, settled):
                    candidates.append((guid, ref, plan[1]))
            for guid, ref, status in candidates:
                if len(taken) >= cap or budget <= 0:
                    break
                cmd = CalendarSignup(uuid.uuid4().hex, event, guid, status)
                if ledger.prepare(cmd, owner=OWNER, ref=ref, context=context, expects_job=True) is None:
                    continue
                self._tried(ref, now)
                taken.add(guid)
                budget -= 1
                out.append(cmd)

    # -- his RSVPs --------------------------------------------------------------------------

    def _his_rsvp(self, ev):
        human = self._human()
        if (not human or type(ev.event_id) is not int or not 0 < ev.event_id <= _I64
                or type(ev.status) is not str or not _STATUS.fullmatch(ev.status) or type(ev.ts) is not int):
            return
        ts = ev.ts / 1000 if ev.ts > 0 else self._clock.now()
        with self._store.transaction():
            self._store.conn.execute(
                "INSERT INTO realm_rsvp(event,guid,status,ts) VALUES (?,?,?,?) "
                "ON CONFLICT(event,guid) DO UPDATE SET status=excluded.status,ts=excluded.ts",
                (ev.event_id, human, ev.status, ts))


def attach(realm):
    """The calendar desk as a Realm part, with the events it reads. No I/O."""
    return CalendarDesk(realm), EVENTS
