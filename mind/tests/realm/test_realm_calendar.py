"""The guild's calendar desk: its posts, their event ids, the members' signups and his RSVPs.

Over a real Store and the real direct ledger. A small realm answers like native would: each
command passes the ledger's barrier, is acked, and a post's job carries its event id.
"""
import asyncio
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import make_persona, run
from leeroy_mind.guild.protocol import Job
from leeroy_mind.human import HumanIdentity
from leeroy_mind.protocol import Ack, Hello, Member
from leeroy_mind.realm import calendar, protocol
from leeroy_mind.realm.actions import ActionContext
from leeroy_mind.realm.calendar import CalendarDesk, signup_plan
from leeroy_mind.realm.config import RealmConfig
from leeroy_mind.realm.direct import DirectLedger
from leeroy_mind.realm.guard import KickGuard
from leeroy_mind.realm.ports import RealmDisabled, _title_matches
from leeroy_mind.realm.service import attach_realm
from dbtemplate import open_migrated
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_clock import FakeClock
from realm.fakes import RealmApp, enabled

NY = ZoneInfo("America/New_York")
HUMAN, GUILD, PROCESS = 10, 21, "calendar-test"
LEAD, SECOND, THIRD = 102, 103, 104  # The standing raid lead (a tank) and two other raid leads.
TUESDAY = datetime(2026, 11, 3, 5, 0, tzinfo=NY)  # An hour after the Tuesday 04:00 reset.
RAID_ID = "c" * 32


def _name(n):
    letters = "abcdefghijklmnopqrstuvwxyz"
    return "Mem" + letters[n // 26 % 26] + letters[n % 26]


class Ports:
    """The reads the desk takes from the Realm's ports, as LivePorts answers them."""

    def __init__(self, rig):
        self.rig = rig

    def now(self):
        return self.rig.clock.now()

    def human(self):
        return HumanIdentity(HUMAN, "Testmaster", "", "", True)

    def cast(self):
        return [Member(g, p.character_name, 2, 1, 0, 80, 3, False, 0, 0, -1)
                for g, p in sorted(self.rig.personas.items())]

    def persona(self, guid):
        return self.rig.personas.get(guid)

    def by_title(self, word):
        return [m.guid for m in self.cast() if _title_matches(self.persona(m.guid).play.officer_title, word)]

    def officers(self):
        return [m.guid for m in self.cast() if self.persona(m.guid).play.guild_role == "officer"]

    def away(self):
        return set(self.rig.away)

    def his_logins(self, since):
        return [row[0] for row in self.rig.store.conn.execute(
            "SELECT started FROM session WHERE started>=? ORDER BY started,id", (since,))]

    async def send(self, cmd):
        return await self.rig.native(cmd)


class FakeRealm:
    def __init__(self, rig):
        self.rig = rig
        self.app = SimpleNamespace(store=rig.store, clock=rig.clock, data_dir=rig.root)
        self.ports = Ports(rig)
        self.cfg = RealmConfig(enabled=True)
        self.enabled = True
        self.guard = KickGuard(rig.store, self.ports, process_id=PROCESS, clock=rig.clock)
        self.tasks = []

    def part(self, name):
        return self.rig.ledger if name == "direct" else None

    def action_context(self):
        return ActionContext(self.enabled, True, True, 1, PROCESS, 1, GUILD, HUMAN)

    def spawn(self, fn):
        if not self.enabled:
            return None
        task = asyncio.get_running_loop().create_task(fn())
        self.tasks.append(task)
        return task


class Rig:
    def __init__(self, root, *, start=TUESDAY, auto_job=True):
        self.root = root
        self.clock = FakeClock(start)
        self.store = Store(open_migrated(root / "mind.db"), clock=self.clock)
        self.ledger = DirectLedger(self.store, process_id=PROCESS, clock=self.clock)
        self.personas, self.away, self.sent = {}, set(), []
        self.auto_job, self.next_event, self.answer = auto_job, 4000, None
        self.realm = FakeRealm(self)
        self.desk = CalendarDesk(self.realm)

    def member(self, guid, name=None, **over):
        self.personas[guid] = make_persona(guid, name or _name(guid), **over)

    def lead(self, guid=LEAD, name="Belra", raid_role="tank", **over):
        self.member(guid, name, officer_title="Raid Lead", guild_role="officer", raid_role=raid_role, **over)

    def context(self):
        return self.realm.action_context()

    async def native(self, cmd):
        """The bridge and native together, with the service's routing to the direct ledger."""
        if not self.realm.enabled:
            raise RealmDisabled("realm disabled")
        self.ledger.before_send(cmd, context=self.context())
        self.sent.append(cmd)
        ok, error = self.answer(cmd) if self.answer is not None else (True, "")
        ack = Ack(0, cmd.id, ok, error)
        self.ledger.on_ack(cmd, ack)
        if ok and self.auto_job:
            ev = self.job(cmd)
            if ev is not None:  # After the ledger, the service hands a realm_job to its parts.
                await self.desk.on_event(ev)
        return ack

    def job(self, cmd, *, state="done", value=None):
        if type(cmd) is protocol.CalendarPost:
            if value is None:
                self.next_event += 1
                value = self.next_event
            ev = protocol.RealmJob(0, cmd.id, "calendar_post", state, "", cmd.creator_guid, value)
        elif type(cmd) is protocol.CalendarSignup:
            ev = protocol.RealmJob(0, cmd.id, "calendar_signup", state, "", cmd.guid, 0 if value is None else value)
        else:
            return None
        self.ledger.on_job(ev)
        return ev

    def tick(self, seconds=60):
        self.clock._t += seconds

        async def go():
            await self.desk.tick()
            await self.settle()
        run(go())

    def hours(self, hours):
        for _ in range(int(hours * 60)):
            self.tick()

    async def settle(self):
        while self.realm.tasks:
            tasks, self.realm.tasks = self.realm.tasks, []
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)

    def event(self, ev):
        async def go():
            await self.desk.on_event(ev)
            await self.settle()
        run(go())

    def posts(self, event_type=None):
        return [c for c in self.sent if type(c) is protocol.CalendarPost
                and (event_type is None or c.event_type == event_type)]

    def signups(self, event_id=None):
        return [c for c in self.sent if type(c) is protocol.CalendarSignup
                and (event_id is None or c.event_id == event_id)]

    def updates(self):
        return [c for c in self.sent if type(c) is protocol.CalendarUpdate]

    def rows(self, kind=None):
        return [dict(r) for r in self.store.conn.execute(
            "SELECT id,kind,creator,at,title,ref,event_id,command_id,state FROM realm_calendar ORDER BY id")
            if kind is None or r["kind"] == kind]

    def rsvps(self, event_id):
        return {r[0]: r[1] for r in self.store.conn.execute(
            "SELECT guid,status FROM realm_rsvp WHERE event=? ORDER BY guid", (event_id,))}

    def login(self, local, minutes=120):
        start = local.timestamp()
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO session(started,ended) VALUES (?,?)",
                                    (start, start + minutes * 60))

    def owned(self, **over):
        value = {**dict(id=RAID_ID, phase="accepted", terminal=None,
                        plan=dict(raid_key="icc", size=10, heroic=False)), **over}
        with self.store.transaction():
            self.store.conn.execute("INSERT INTO meta(key,value) VALUES ('guild:offline:owned',?) "
                                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(value),))

    def close(self):
        self.store.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _local(d, hour, minute=0):
    return datetime.combine(d, time(hour, minute), tzinfo=NY)


def _offline_job(state="progress", stage="running", kills=0, wipes=0):
    return Job(0, RAID_ID, "offline_raid", state, "", stage, (LEAD, SECOND), (), kills, wipes, 77, 0, 0, 0)


def _live_rows(rig, creator, n, *, kind="raid", start=0):
    """n live entries (posted, upcoming) already held by `creator`."""
    with rig.store.transaction():
        for i in range(n):
            rig.store.conn.execute(
                "INSERT INTO realm_calendar(kind,creator,at,title,ref,updated,event_id,command_id,state) "
                "VALUES (?,?,?,?,?,?,?,NULL,'posted')",
                (kind, creator, rig.clock.now() + 86400 + i, "Held", f"held:{creator}:{start + i}",
                 rig.clock.now(), 900000 + creator * 1000 + start + i))


def _live_counts(rig):
    cutoff = rig.clock.now() - calendar.LIVE_S
    return dict(rig.store.conn.execute(
        "SELECT creator,COUNT(*) FROM realm_calendar WHERE state IN ('pending','posted') AND at>=? "
        "GROUP BY creator", (cutoff,)).fetchall())


# -- the week's raid nights ------------------------------------------------------------------------

def test_raid_nights_from_his_logins(rig):
    rig.lead()
    rig.member(SECOND, "Coros", officer_title="Raid Lead", raid_role="healer")
    # The last 28 days before the reset: Tuesdays, Wednesdays and Saturdays twice each at
    # 20-21h, one Friday (once is not enough), and one login after the reset (not history).
    for day, hour in ((date(2026, 10, 27), 20), (date(2026, 10, 20), 21), (date(2026, 10, 28), 20),
                      (date(2026, 10, 21), 21), (date(2026, 10, 31), 20), (date(2026, 10, 24), 21),
                      (date(2026, 10, 30), 19)):
        rig.login(_local(day, hour, 10))
    rig.login(_local(date(2026, 10, 27), 22, 40))  # The same Tuesday again: one evening, one day.
    rig.login(TUESDAY - timedelta(minutes=30) + timedelta(hours=1))
    rig.tick()
    raids = rig.posts("raid")
    assert [datetime.fromtimestamp(p.at, NY) for p in raids] == [
        _local(date(2026, 11, 3), 20), _local(date(2026, 11, 4), 20), _local(date(2026, 11, 7), 20)]
    assert {p.creator_guid for p in raids} == {LEAD}  # The standing raid lead.
    assert all(p.title == "Raid night" and p.guild_event and not p.invite_human for p in raids)
    assert [r["state"] for r in rig.rows("raid")] == ["posted"] * 3
    rig.tick()
    rig.tick(3 * 3600)
    assert len(rig.posts("raid")) == 3  # Each night once.


def test_raid_nights_default_without_history(rig):
    rig.lead()
    rig.tick()
    assert [datetime.fromtimestamp(p.at, NY) for p in rig.posts("raid")] == [
        _local(date(2026, 11, 3), 20), _local(date(2026, 11, 4), 20), _local(date(2026, 11, 5), 20)]


def test_raid_nights_after_the_week_has_begun(tmp_path):
    rig = Rig(tmp_path, start=datetime(2026, 11, 4, 21, 0, tzinfo=NY))  # Wednesday night.
    try:
        rig.lead()
        rig.tick()
        assert [datetime.fromtimestamp(p.at, NY) for p in rig.posts("raid")] == [_local(date(2026, 11, 5), 20)]
    finally:
        rig.close()


def test_raid_night_never_declined(rig):
    for event_id in range(1, 400):
        for c in range(1, 11):
            due, status = signup_plan("raid", event_id, 200 + c, c, 1000.0, 1000.0 + 16 * 3600)
            assert status in {"signed_up", "tentative"}
            assert 1000.0 + 3600 <= due <= 1000.0 + 8 * 3600
    low = Counter(signup_plan("raid", e, 201, 2, 1000.0, 60000.0)[1] for e in range(1, 400))
    assert low["tentative"] and low["signed_up"] and not low["declined"]
    assert not {signup_plan("raid", e, 201, 8, 1000.0, 60000.0)[1] for e in range(1, 400)} - {"signed_up"}
    # A low conscientiousness that is not tentative signs up late: in the last quarter.
    late = [signup_plan("raid", e, 201, 2, 0.0, 60000.0) for e in range(1, 400)]
    assert all(due >= 3600 + 0.75 * 7 * 3600 for due, status in late if status == "signed_up")
    # A decline exists, but only where he is not needed.
    social = Counter(signup_plan("birthday", e, 201, 8, 1000.0, 60000.0)[1] for e in range(1, 400))
    assert social["declined"] and social["signed_up"]
    # Through the desk: a sloppy member signs up for the raid night, never declining.
    rig.lead()
    for guid in range(110, 130):
        rig.member(guid, conscientiousness={"score": 2, "behaviour": "forgets things"})
    rig.tick()
    rig.hours(9)
    tuesday = rig.rows()[0]["event_id"]
    statuses = set(rig.rsvps(tuesday).values())
    assert statuses <= {"signed_up", "tentative"} and len(rig.rsvps(tuesday)) == 21


# -- offline raids -------------------------------------------------------------------------------

def test_offline_raid_posted_and_updated(rig):
    rig.lead()
    rig.member(SECOND, "Coros", officer_title="raid lead", raid_role="healer")
    rig.owned()
    rig.event(_offline_job("progress", "grouping"))  # Not started yet.
    assert rig.posts() == []
    rig.event(_offline_job())
    (post,) = rig.posts("raid")
    assert post.title == "Icecrown Citadel 10, offline" and post.at == int(rig.clock.now()) + calendar.OFFLINE_AHEAD_S
    assert post.creator_guid in {LEAD, SECOND}
    rig.event(_offline_job())  # Running again: still the one entry.
    assert len(rig.posts()) == 1
    event_id = rig.rows()[0]["event_id"]
    rig.tick()
    assert rig.updates() == []  # Nothing to say while it runs.
    rig.owned(terminal=dict(state="done", kills=12, wipes=2, reason=""))
    rig.event(_offline_job("done", "tearing_down", kills=12, wipes=2))
    rig.tick()
    (update,) = rig.updates()
    assert (update.event_id, update.description, update.title, update.at) == (event_id, "cleared, 2 wipes", "", 0)
    rig.tick(3600)
    assert len(rig.updates()) == 1  # Said once.


def test_offline_raid_result_text():
    assert calendar.result_text("icc", 12, 2, False) == "cleared, 2 wipes"
    assert calendar.result_text("icc", 9, 1, False) == "9 of 12 bosses, 1 wipe"
    assert calendar.result_text("icc", 4, 0, True) == "4 of 12 bosses, no wipes, called off"
    assert calendar.result_text("nope", 3, 2, False) == "3 kills, 2 wipes"


def test_an_offline_job_that_is_not_the_guilds_own_posts_nothing(rig):
    rig.lead()
    rig.event(_offline_job())  # No owned offline raid at all.
    rig.owned(id="d" * 32)
    rig.event(_offline_job())  # Another raid's.
    assert rig.posts() == [] and rig.rows() == []


# -- who posts -----------------------------------------------------------------------------------

def test_creators_rotate_before_the_cap(rig):
    rig.lead()
    rig.member(SECOND, "Coros", officer_title="Raid Lead", raid_role="healer")
    rig.member(THIRD, "Dunna", officer_title="raid lead and dkp", raid_role="ranged")
    _live_rows(rig, LEAD, 25)
    _live_rows(rig, SECOND, 3)
    _live_rows(rig, THIRD, 1)
    rig.owned()
    rig.event(_offline_job())
    assert [p.creator_guid for p in rig.posts()] == [THIRD]  # The rotation by load.
    # The week's raid nights pass from the standing lead (full) to the next raid lead.
    rig.tick()
    assert [p.creator_guid for p in rig.posts("raid")] == [THIRD, SECOND, SECOND, SECOND]
    assert max(_live_counts(rig).values()) <= 25
    # Everyone full: next week's nights are not posted at all, and nobody passes 25.
    _live_rows(rig, SECOND, 25 - _live_counts(rig)[SECOND], start=100)
    _live_rows(rig, THIRD, 25 - _live_counts(rig)[THIRD], start=100)
    rig.tick(7 * 86400)
    assert len(rig.posts("raid")) == 4 and max(_live_counts(rig).values()) == 25


def test_refused_post_retried_by_another_creator_then_given_up(rig):
    rig.lead()
    rig.member(SECOND, "Coros", officer_title="Raid Lead", raid_role="healer")
    rig.answer = lambda cmd: (False, "busy:training_owner")
    rig.tick()
    tuesday = [p for p in rig.posts("raid") if datetime.fromtimestamp(p.at, NY).day == 3]
    assert [p.creator_guid for p in tuesday] == [LEAD]
    assert {r["state"] for r in rig.rows()} == {"failed"}
    rig.tick()  # Within the retry pause: nothing.
    assert len(rig.posts("raid")) == 3
    rig.tick(calendar.RETRY_S)
    tuesday = [p for p in rig.posts("raid") if datetime.fromtimestamp(p.at, NY).day == 3]
    assert [p.creator_guid for p in tuesday] == [LEAD, SECOND]  # Another holder next.
    for _ in range(5):
        rig.tick(calendar.RETRY_S)
    tuesday = [p for p in rig.posts("raid") if datetime.fromtimestamp(p.at, NY).day == 3]
    assert len(tuesday) == calendar.MAX_TRIES  # Then given up.
    rig.answer = None
    rig.tick(calendar.RETRY_S)
    assert len(rig.posts("raid")) == 3 * calendar.MAX_TRIES


# -- the officer meeting, birthdays and the anniversary ----------------------------------------------

def test_meeting_invites_him(tmp_path):
    rig = Rig(tmp_path, start=datetime(2026, 11, 9, 10, 0, tzinfo=NY))  # A Monday.
    try:
        rig.lead()
        rig.member(SECOND, "Coros", guild_role="officer", raid_role="healer")
        rig.member(105, "Ebbo")  # Not an officer.
        rig.tick()
        (meeting,) = rig.posts("meeting")
        assert (meeting.creator_guid, meeting.invite_human, meeting.title) == (LEAD, True, "Officer meeting")
        assert datetime.fromtimestamp(meeting.at, NY) == _local(date(2026, 11, 12), 21)
        rig.hours(9)
        assert set(rig.rsvps(rig.rows()[0]["event_id"])) == {LEAD, SECOND}  # Officers only.
        assert len(rig.posts("meeting")) == 1
    finally:
        rig.close()


@pytest.mark.parametrize("start, posted", [
    (datetime(2026, 11, 11, 10, 0, tzinfo=NY), True),   # Wednesday: a missed Monday caught up.
    (datetime(2026, 11, 12, 10, 0, tzinfo=NY), False),  # Thursday: never on its own day.
    (datetime(2026, 11, 13, 10, 0, tzinfo=NY), False),  # Friday.
])
def test_meeting_posted_from_monday_until_wednesday(tmp_path, start, posted):
    rig = Rig(tmp_path, start=start)
    try:
        rig.lead()
        rig.tick()
        assert [datetime.fromtimestamp(p.at, NY) for p in rig.posts("meeting")] == (
            [_local(date(2026, 11, 12), 21)] if posted else [])
    finally:
        rig.close()


def test_anniversary_from_local_config(tmp_path):
    rig = Rig(tmp_path, start=datetime(2027, 3, 15, 10, 0, tzinfo=NY))
    try:
        rig.lead()
        rig.member(SECOND, "Coros", officer_title="Historian")
        rig.member(THIRD, "Dunna", officer_title="Social")
        rig.tick()
        assert rig.posts("other") == []  # No anniversary while the setting is empty.
        rig.realm.cfg = RealmConfig(enabled=True, guild_anniversary="03-19")
        rig.tick(3600)
        (post,) = rig.posts("other")
        assert (post.title, post.creator_guid) == ("Guild anniversary", THIRD)  # social before historian
        assert datetime.fromtimestamp(post.at, NY) == _local(date(2027, 3, 19), 20)
        rig.tick(86400)
        assert len(rig.posts("other")) == 1
    finally:
        rig.close()


def test_anniversary_only_the_week_before(tmp_path):
    rig = Rig(tmp_path, start=datetime(2027, 3, 10, 10, 0, tzinfo=NY))
    try:
        rig.lead(birthday="09-01")
        rig.realm.cfg = RealmConfig(enabled=True, guild_anniversary="03-19")
        rig.tick()
        assert rig.posts("other") == []
        rig.tick(2 * 86400)
        (post,) = rig.posts("other")
        assert post.creator_guid == LEAD  # No events, social or historian: the standing lead.
    finally:
        rig.close()


def test_birthday_posted_the_week_before(rig):
    rig.lead(birthday="11-20")
    rig.member(SECOND, "Coros", officer_title="Events", birthday="11-06")
    rig.member(105, "Ebbo", birthday="11-06")
    rig.member(106, "Fenna", birthday="11-05")  # He kicked her: a former member, no post.
    with rig.store.transaction():
        rig.store.conn.execute("INSERT INTO realm_kick(guid,ts,rank) VALUES (106,1000.0,4)")
    rig.tick()
    birthdays = sorted((p.title, p.creator_guid, datetime.fromtimestamp(p.at, NY)) for p in rig.posts("other"))
    assert birthdays == [("Coros's birthday", SECOND, _local(date(2026, 11, 6), 20)),
                         ("Ebbo's birthday", SECOND, _local(date(2026, 11, 6), 20))]
    rig.tick(86400)
    assert len(rig.posts("other")) == 2
    rig.tick(10 * 86400)  # The lead's own, the week before it.
    assert [p.title for p in rig.posts("other")][-1] == "Belra's birthday"


def test_a_crowded_birthday_is_one_combined_event(rig):
    rig.lead(birthday="09-01")
    names = ["Ebbo", "Fenna", "Garr", "Hilde", "Ivo"]
    for i, name in enumerate(names):
        rig.member(105 + i, name, birthday="11-06")
    rig.tick()
    events = rig.posts("other")
    assert 1 <= len(events) <= 3
    text = " | ".join(f"{e.title} {e.description}" for e in events)
    for name in names:
        assert text.count(name) == 1
    rig.tick(86400)
    assert len(rig.posts("other")) == len(events)  # Posted once.
    rig.hours(9)
    event = rig.rows("birthday")[0]["event_id"]
    assert 0 < len(rig.signups(event)) <= calendar.SIGNUP_CAP


def test_two_birthdays_on_a_date_stay_single_events(rig):
    rig.lead(birthday="09-01")
    rig.member(105, "Ebbo", birthday="11-06")
    rig.member(106, "Fenna", birthday="11-06")
    rig.tick()
    assert sorted((p.title, p.description) for p in rig.posts("other")) == [
        ("Ebbo's birthday", ""), ("Fenna's birthday", "")]


# -- signups -------------------------------------------------------------------------------------

def test_signups_capped(rig):
    rig.lead()
    for guid in range(200, 320):  # 120 more members.
        rig.member(guid)
    rig.tick()
    tuesday = rig.rows()[0]["event_id"]
    rig.hours(9)
    signed = [c.guid for c in rig.signups(tuesday)]
    assert len(signed) == len(set(signed)) == calendar.SIGNUP_CAP == 99
    assert len(rig.rsvps(tuesday)) == 99 and HUMAN not in rig.rsvps(tuesday)


def test_signup_schedule_deterministic_no_duplicates(rig):
    assert signup_plan("raid", 77, 201, 5, 100.0, 90000.0) == signup_plan("raid", 77, 201, 5, 100.0, 90000.0)
    assert signup_plan("raid", 77, 201, 5, 100.0, 90000.0) != signup_plan("raid", 78, 201, 5, 100.0, 90000.0)
    assert signup_plan("raid", 77, 201, 5, 100.0, 120.0) is None  # Too late to sign up at all.
    near = signup_plan("raid", 77, 201, 5, 100.0, 1900.0)  # An event soon after its post.
    assert 100.0 <= near[0] <= 1900.0 - 60
    rig.lead()
    for guid in range(200, 230):
        rig.member(guid)
    rig.tick()
    tuesday = rig.rows()[0]["event_id"]
    rig.hours(3)
    rig.auto_job = False  # Native took these, and its results are still to come.
    rig.hours(1)
    waiting = [c for c in rig.signups(tuesday)
               if rig.ledger.state("calendar", f"signup:{tuesday}:{c.guid}") == ("acked", None, None)]
    assert waiting

    lost = []

    async def cut(cmd):  # Past the barrier, then the mind stopped before any answer.
        rig.ledger.before_send(cmd, context=rig.context())
        rig.sent.append(cmd)
        lost.append(cmd)
    rig.native = cut
    before = len(rig.signups(tuesday))
    for _ in range(240):
        rig.tick()
        if len(rig.signups(tuesday)) > before:
            break
    cut_off = rig.signups(tuesday)[before:]
    assert cut_off
    del rig.native
    # A restart: the next process recovers the ledger and runs a fresh desk on the same store.
    rig.ledger = DirectLedger(rig.store, process_id="next", clock=rig.clock)
    assert rig.ledger.recover(process_id="next") == (0, len(lost))
    rig.realm.action_context = lambda: ActionContext(True, True, True, 1, "next", 1, GUILD, HUMAN)
    rig.desk = CalendarDesk(rig.realm)
    rig.auto_job = True
    rig.tick()
    for cmd in waiting:  # Their results arrive late: still the very same intents.
        rig.job(cmd)
    rig.hours(6)
    signed = [c.guid for c in rig.signups(tuesday)]
    assert len(signed) == len(set(signed)) == 31  # Each member once, ever.
    assert set(rig.rsvps(tuesday)) == set(signed)  # Cut off counts as maybe done: never again.
    assert all(rig.rsvps(tuesday)[c.guid] == c.status for c in rig.signups(tuesday))


# -- the post and its event id ---------------------------------------------------------------------

def test_post_binds_event_id(rig):
    rig.lead()
    rig.auto_job = False
    rig.tick()
    posts = rig.posts("raid")
    assert {r["state"] for r in rig.rows()} == {"pending"}
    assert {r["command_id"] for r in rig.rows("raid")} == {p.id for p in posts}
    assert all(rig.ledger.state("calendar", r["ref"]) == ("acked", None, None) for r in rig.rows())
    rig.tick(3600)
    assert rig.signups() == [] and len(rig.posts()) == 4  # Waiting for the event ids: nothing more.
    ev = rig.job(posts[0], value=4242)
    rig.event(ev)  # Bound at once, from the job the ledger took.
    assert [(r["state"], r["event_id"]) for r in rig.rows()][0] == ("posted", 4242)
    failed = rig.job(posts[1], state="failed", value=0)
    rig.event(failed)
    assert rig.rows()[1]["state"] == "failed" and rig.rows()[1]["event_id"] is None
    rig.auto_job = True
    rig.hours(9)
    assert {c.event_id for c in rig.signups()} >= {4242}
    assert all(c.event_id in {r["event_id"] for r in rig.rows()} for c in rig.signups())


def test_an_event_id_native_reuses_moves_to_the_new_post(rig):
    rig.lead()
    with rig.store.transaction():
        rig.store.conn.execute(
            "INSERT INTO realm_calendar(kind,creator,at,title,ref,updated,event_id,command_id,state) "
            "VALUES ('raid',?,?,?,?,?,4001,NULL,'posted')", (LEAD, 1000.0, "Old", "raid:2026-09-01", 1000.0))
        rig.store.conn.execute("INSERT INTO realm_rsvp(event,guid,status,ts) VALUES (4001,105,'signed_up',1000.0)")
    rig.tick()  # The first new post gets 4001 again (native freed it with the old event).
    rows = rig.rows()
    assert (rows[0]["ref"], rows[0]["event_id"], rows[0]["state"]) == ("raid:2026-09-01", None, "posted")
    assert rows[1]["event_id"] == 4001 and rig.rsvps(4001) == {}


def test_unknown_post_is_never_posted_again(rig):
    rig.lead()
    # A row from before 0016: posted, no event id. Its night is never posted again.
    with rig.store.transaction():
        rig.store.conn.execute(
            "INSERT INTO realm_calendar(kind,creator,at,title,ref,updated,event_id,command_id,state) "
            "VALUES ('raid',?,?,?,'raid:2026-11-04',?,NULL,NULL,'posted')",
            (LEAD, _local(date(2026, 11, 4), 20).timestamp(), "Raid night", 1000.0))

    async def lost(cmd):  # It passed the barrier and the ack never came back.
        rig.ledger.before_send(cmd, context=rig.context())
        rig.sent.append(cmd)
        rig.ledger.on_send_error(cmd, "AckTimeout")
        raise TimeoutError("ack timeout")
    rig.native = lost
    rig.tick()
    assert [datetime.fromtimestamp(p.at, NY).day for p in rig.posts("raid")] == [3, 5]
    states = {r["ref"]: (r["state"], r["event_id"]) for r in rig.rows("raid")}
    assert states == {"raid:2026-11-04": ("posted", None), "raid:2026-11-03": ("posted", None),
                      "raid:2026-11-05": ("posted", None)}
    del rig.native
    for _ in range(5):
        rig.tick(calendar.RETRY_S)
    assert len(rig.posts("raid")) == 2 and rig.signups() == []  # No event id: no signups either.


def test_a_post_that_never_left_is_closed_and_retried(rig):
    rig.lead()
    rig.realm.spawn = lambda fn: None  # The Realm would not start the send.
    rig.tick()
    assert rig.sent == [] and {r["state"] for r in rig.rows()} == {"failed"}
    assert {rig.ledger.state("calendar", r["ref"])[0] for r in rig.rows()} == {"abandoned"}
    del rig.realm.spawn
    rig.tick(calendar.RETRY_S)
    assert len(rig.posts("raid")) == 3


def test_posts_not_held_by_raid(tmp_path, monkeypatch):
    clock = FakeClock(datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc))  # Tuesday, after the reset.
    store = Store(open_migrated(tmp_path / "mind.db"), clock=clock)
    app = RealmApp(tmp_path, store, clock)
    store.upsert_cast([Member(LEAD, "Belra", 4, 11, 0, 80, 1, True, 0, 0, -1)], 1000.0)
    app.personas = {LEAD: make_persona(LEAD, "Belra", officer_title="Raid Lead", guild_role="officer")}
    # His raid night is waiting for the call: it holds the whole roster, the lead included.
    app.world.group = None
    app.guild = SimpleNamespace(raid_night=SimpleNamespace(state=SimpleNamespace(name="WAITING_CALL"),
                                                           session=None, _closing=()))
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    desk = realm.parts["calendar"]
    assert type(desk) is CalendarDesk

    async def go():
        await app.connection(True)
        await app.event(Hello(0, "test", "1", GUILD, app.human_guid))
        assert realm.ports.raid_holds_all() and realm.bodies.holder(LEAD) == "raid"
        await desk.tick()
        for _ in range(20):
            if not realm._children:
                break
            await asyncio.gather(*tuple(realm._children), return_exceptions=True)
        posts = [c for c in app.sent if type(c) is protocol.CalendarPost and c.event_type == "raid"]
        assert len(posts) == 3 and {p.creator_guid for p in posts} == {LEAD}
        direct = realm.parts["direct"]
        assert direct.state("calendar", "raid:2026-11-03") == ("acked", None, None)
        await app.event(protocol.RealmJob(1000, posts[0].id, "calendar_post", "done", "", LEAD, 4242))
        assert direct.state("calendar", "raid:2026-11-03") == ("acked", None, 4242)
        row = store.conn.execute("SELECT state,event_id FROM realm_calendar WHERE ref='raid:2026-11-03'").fetchone()
        assert tuple(row) == ("posted", 4242)
        assert realm.bodies.holder(LEAD) == "raid"  # Still held, and the post went out anyway.
        await realm.close()
    try:
        run(go())
    finally:
        store.close()


# -- his RSVPs and the nights he missed --------------------------------------------------------------

def test_his_rsvps_stored(rig):
    rig.event(protocol.CalendarRsvp(1_700_000_000_000, 4242, "accepted"))
    assert rig.rsvps(4242) == {HUMAN: "accepted"} and rig.desk.his_rsvp(4242) == "accepted"
    rig.event(protocol.CalendarRsvp(1_700_000_060_000, 4242, "declined"))
    assert rig.desk.his_rsvp(4242) == "declined" and rig.desk.his_rsvp(4243) is None
    for bad in (protocol.CalendarRsvp(0, 0, "accepted"), protocol.CalendarRsvp(0, 1 << 63, "accepted"),
                protocol.CalendarRsvp(0, 4243, "Accepted!"), protocol.CalendarRsvp(0, 4243, "")):
        rig.event(bad)
    assert rig.store.conn.execute("SELECT COUNT(*) FROM realm_rsvp").fetchone()[0] == 1


def test_raid_night_he_missed_is_updated(rig):
    rig.lead()
    rig.tick()
    tue, wed, thu = rig.rows("raid")
    # Tuesday he came; on Wednesday the guild ran ICC offline; on Thursday nothing happened.
    rig.login(_local(date(2026, 11, 3), 19, 50), minutes=180)
    wed_at = _local(date(2026, 11, 4), 20).timestamp()
    with rig.store.transaction():
        rig.store.conn.execute(
            "INSERT INTO guild_raid(started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid,"
            "kills,wipes) VALUES (?,?,'icc',10,0,1,?,?,?,5,3)", (wed_at + 3600, wed_at + 4 * 3600, LEAD, LEAD, LEAD))
    rig.clock._t = _local(date(2026, 11, 3), 23, 59).timestamp()
    rig.tick()
    assert rig.updates() == []  # Not yet 4 hours after a night.
    rig.clock._t = _local(date(2026, 11, 6), 3).timestamp()
    rig.tick()
    by_event = {u.event_id: u.description for u in rig.updates()}
    assert by_event == {wed["event_id"]: "night off: Icecrown Citadel offline, 5 of 12 bosses, 3 wipes",
                        thu["event_id"]: "no raid"}
    rig.tick(3600)
    assert len(rig.updates()) == 2


# -- the switch and the part --------------------------------------------------------------------------

def test_nothing_while_disabled(rig):
    rig.lead()
    rig.realm.enabled = False
    rig.tick()
    rig.event(protocol.CalendarRsvp(1_700_000_000_000, 4242, "accepted"))
    rig.owned()
    rig.event(_offline_job())
    assert rig.sent == [] and rig.rows() == [] and rig.rsvps(4242) == {}
    rig.realm.enabled = True
    rig.tick()
    assert len(rig.posts("raid")) == 3


def test_a_failing_step_never_fails_the_realm_or_the_other_steps(rig, caplog):
    rig.lead()
    for guid in range(200, 210):
        rig.member(guid)
    rig.tick()
    rig.realm.ports.his_logins = lambda since: 1 / 0  # The week's nights cannot be read now...
    rig.hours(9)
    assert len(rig.signups(rig.rows()[0]["event_id"])) == 11  # ...and the posted ones still get signups.
    failures = [r.getMessage() for r in caplog.records if r.getMessage().startswith("realm_calendar_failed")]
    assert failures == ["realm_calendar_failed step=posts error=ZeroDivisionError"]  # Said once.


def test_attach_is_a_part_without_io(rig):
    changes = rig.store.conn.total_changes
    part, events = calendar.attach(rig.realm)
    assert type(part) is CalendarDesk and set(events) == {"realm_job", "job", "calendar_rsvp"}
    assert rig.store.conn.total_changes == changes and rig.realm.tasks == []
