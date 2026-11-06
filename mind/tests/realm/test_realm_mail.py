"""The post office: realm_mail rows, each sent at most once through the direct ledger.

Most tests drive mail.PostOffice over a real Store and DirectLedger with a small realm surface;
test_letter_not_held_by_raid runs it inside the real service.
"""
import asyncio
import json
import random
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from conftest import make_persona, run, seed_test_cast
from leeroy_mind.human import HumanIdentity
from leeroy_mind.llm.scheduler import LLMScheduler
from leeroy_mind.memory.writer import Embedder, MemoryWriter, WriteResult
from leeroy_mind.protocol import Ack
from leeroy_mind.realm import mail, protocol
from leeroy_mind.realm.actions import ActionContext
from leeroy_mind.realm.config import RealmConfig
from leeroy_mind.realm.direct import DirectLedger
from leeroy_mind.realm.ports import RealmDisabled
from dbtemplate import open_migrated
from leeroy_mind.storage.rows import Relationship
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_llm import FakeLLM

PROCESS = "mail-test-process"
DAY = 86400.0
HOUR = 3600.0
# A Tuesday after the 04:00 New York reset: the product week runs to the next Tuesday.
T0 = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc).timestamp()
HUMAN = HumanIdentity(1, "Hiscar", "Boss", "", True, "he/him")
HOLDS = {"WAITING_CALL", "CALLED", "FORMING", "ASSEMBLING", "RUNNING"}


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def now(self):
        return self.t

    def local(self):
        return datetime.fromtimestamp(self.t, timezone.utc)


class Always(random.Random):
    """Every chance taken; choices stay seeded."""

    def random(self):
        return 0.0


class Never(random.Random):
    def random(self):
        return 0.99


class Ports:
    def __init__(self, realm, store, clock):
        self.realm, self.store, self.clock, self.conn = realm, store, clock, store.conn
        self.online, self.raid, self.personas, self.away_set = True, "", {}, set()
        self.offline, self.training = set(), set()
        self.sent, self.remembered, self.ack = [], [], (True, "")
        self.before = self.wire = self.writer = None

    def now(self):
        return self.clock.now()

    def human(self):
        return HUMAN

    def human_online(self):
        return self.online

    def cast(self):
        return [SimpleNamespace(guid=g) for g in sorted(self.personas)]

    def persona(self, guid):
        return self.personas.get(guid)

    def relationship(self, a, b):
        return self.store.get_relationship(a, b)

    def away(self):
        return set(self.away_set)

    def raid_state(self):
        return self.raid

    def raid_holds_all(self):
        return self.raid in HOLDS

    def offline_raid_members(self):
        return set(self.offline)

    def training_members(self):
        return set(self.training)

    async def send(self, cmd):
        """The service's path: the direct part's barrier, the wire, then the ack."""
        if not self.realm.enabled:
            raise RealmDisabled("realm disabled")
        if self.before is not None:
            await self.before(cmd)
        direct = self.realm.part("direct")
        direct.before_send(cmd, context=self.realm.action_context())
        self.sent.append(cmd)
        if self.wire is not None:
            try:
                await self.wire(cmd)
            except Exception as error:  # A cancelled caller leaves the shielded transport be.
                direct.on_send_error(cmd, type(error).__name__)
                raise
        ack = Ack(0, cmd.id, *self.ack)
        direct.on_ack(cmd, ack)
        return ack

    async def remember(self, candidate):
        self.remembered.append(candidate)
        if self.writer is not None:
            return await self.writer.write(candidate, enforce_limits=False)
        return WriteResult(len(self.remembered), "inserted")


class Bank:
    """The bank part's gift interface: a BoE in stock, and its withdrawal by ref."""

    def __init__(self, answers=("done",), offer=(47586, 3)):
        self.offer, self.answers, self.calls = offer, list(answers), []

    def gift_boe(self):
        return self.offer

    def withdraw_gift(self, ref, guid, item_id, count, tab):
        self.calls.append((ref, guid, item_id, count, tab))
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Realm:
    def __init__(self, store, clock):
        self.app = SimpleNamespace(store=store, clock=clock,
                                   world=SimpleNamespace(online=set(), afk=set(), group=set(),
                                                         in_group=lambda guid: guid in self.app.world.group))
        self.enabled, self.cfg = True, RealmConfig(enabled=True)
        self.ports = Ports(self, store, clock)
        self.direct = DirectLedger(store, process_id=PROCESS, clock=clock)
        self.bank = None
        self.queue = SimpleNamespace(submit=self._no_queue)
        self.tasks = []

    @staticmethod
    def _no_queue(*args, **kwargs):
        raise AssertionError("mail never queues a body command")

    def part(self, name):
        return {"direct": self.direct, "bank": self.bank}.get(name)

    def action_context(self):
        return ActionContext(self.enabled, True, True, 1, PROCESS, 1, 21, 1)

    def spawn(self, fn):
        if not self.enabled:
            return None
        task = asyncio.get_running_loop().create_task(fn())
        self.tasks.append(task)
        return task

    async def settle(self):
        while pending := [t for t in self.tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path, clock):
    store = Store(open_migrated(tmp_path / "mind.db"), clock=clock)
    yield store
    store.close()


@pytest.fixture
def realm(db, clock):
    return Realm(db, clock)


NAMES = {101: "Arkon", 102: "Belra", 103: "Coros", 104: "Dunna", 105: "Ebbo", 106: "Fenna",
         107: "Gwyl", 108: "Hesta", 109: "Ilsa", 110: "Jorna"}


def member(realm, guid, *, woman=False, stance="none", close=0.0):
    persona = make_persona(guid, NAMES[guid], **({"player_gender": "female"} if woman else {}),
                           stance=stance)
    realm.ports.personas[guid] = persona
    realm.app.world.online.add(guid)
    if close:
        realm.app.store.put_relationship(Relationship(a=guid, b=1, affinity=close, trust=close,
                                                      familiarity=close))
    return persona


def away_since(store, ended, started=None):
    with store.transaction():
        store.conn.execute("INSERT INTO session(started,ended) VALUES (?,?)",
                           (ended - HOUR if started is None else started, ended))


def mails(store, cols="sender,kind,state"):
    return [tuple(r) for r in store.conn.execute(f"SELECT {cols} FROM realm_mail ORDER BY id")]


def ready(store, sender=102, kind="letter", subject="Missed you", body="Come back soon.", ts=T0,
          items=()):
    with store.transaction():
        return store.conn.execute(
            "INSERT INTO realm_mail(ts,sender,kind,subject,body,items,state) VALUES (?,?,?,?,?,?,'ready')",
            (ts, sender, kind, subject, body, json.dumps(list(items)))).lastrowid


def direct_rows(store):
    return [tuple(r) for r in store.conn.execute(
        "SELECT owner,ref,kind,state,reason,value FROM realm_direct ORDER BY rowid")]


def job(realm, cmd, state="done", value=0, reason=""):
    return realm.direct.on_job(protocol.RealmJob(1, cmd.id, "mail_send", state, reason, cmd.from_guid, value))


async def tick(post, realm, clock, seconds=60):
    clock.t += seconds
    await post.tick()
    await realm.settle()


# -- attach and the switch ----------------------------------------------------------------------

def test_attach_is_inert_and_disabled_does_nothing(realm, db, clock):
    before = db.conn.total_changes
    post, events = mail.attach(realm)
    assert type(post) is mail.PostOffice and set(events) == {"mail_taken", "game_event"}
    assert db.conn.total_changes == before and realm.tasks == []  # No I/O at attach.
    member(realm, 102, woman=True, stance="fond", close=60)
    away_since(db, clock.now() - 8 * DAY)
    sent_id = ready(db)
    with db.transaction():
        db.conn.execute("UPDATE realm_mail SET state='sent',item_guid=9001 WHERE id=?", (sent_id,))
    realm.enabled = False
    changes = db.conn.total_changes

    async def go():
        await tick(post, realm, clock)
        await post.on_event(protocol.MailTaken(1, 9001, 46377, 3))
        await post.on_event(protocol.GameEvent(1, mail.LOVE_IS_IN_THE_AIR, "start"))
        assert post.post(102, "prize", subject="You won", body="") is None
    run(go())
    assert db.conn.total_changes == changes and realm.ports.sent == [] and realm.ports.remembered == []
    assert mails(db) == [(102, "letter", "sent")]


# -- the letter after a long absence -----------------------------------------------------------

def test_absence_letter_once(realm, db, clock):
    member(realm, 101, close=20)
    member(realm, 102, woman=True, stance="fond", close=40)
    member(realm, 103, close=90)                # The closest member writes.
    member(realm, 104, close=95)
    realm.ports.away_set.add(104)               # Away herself: never the writer.
    realm.ports.online = False
    away_since(db, clock.now() - 6 * DAY)
    post = mail.PostOffice(realm, rng=Never())

    async def go():
        await tick(post, realm, clock)
        assert mails(db) == []                  # Six days: no letter yet.
        await tick(post, realm, clock, 2 * DAY)  # Eight days away.
        assert mails(db, "sender,kind,state,subject,body,items,command_id") == [
            (103, "letter", "awaiting_text", "", "", "[]", None)]
        for _ in range(12):                     # Twenty days: still one.
            await tick(post, realm, clock, DAY)
        assert len(mails(db)) == 1
        # He comes back, then is away a week again: a new absence, a new letter.
        away_since(db, clock.now() + HOUR, started=clock.now())
        await tick(post, realm, clock, HOUR + 8 * DAY)
        assert mails(db) == [(103, "letter", "awaiting_text")] * 2
    run(go())
    assert realm.ports.sent == []               # Nothing goes while he is away, nor without text.


def test_mail_waits_for_text(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    away_since(db, clock.now() - 8 * DAY)
    post = mail.PostOffice(realm, rng=Never(7))

    async def go():
        await tick(post, realm, clock)
        [(mail_id,)] = mails(db, "id")
        realm.ports.online = True               # He logs in: the letter has no text yet.
        await tick(post, realm, clock)
        await tick(post, realm, clock)
        assert realm.ports.sent == [] and mails(db, "state") == [("awaiting_text",)]
        wanted = mail.texts_wanted(db.conn)
        assert [(w.id, w.sender, w.kind, w.items) for w in wanted] == [(mail_id, 102, "letter", ())]
        # The night's text: refused when it is too long, empty or names his real name.
        assert not mail.write_text(db, mail_id, subject="", body="hi")
        assert not mail.write_text(db, mail_id, subject="x" * 65, body="hi")
        assert not mail.write_text(db, mail_id, subject="Hey", body="x" * 513)
        named = HumanIdentity(1, "Hiscar", "Boss", "Jordan Vale", True, "he/him")
        assert not mail.write_text(db, mail_id, subject="Hey", body="Jordan Vale, come back", human=named)
        assert mail.write_text(db, mail_id, subject="Missed you", body="The guild is quiet.", human=named)
        assert not mail.write_text(db, mail_id, subject="Again", body="")  # Written once.
        await tick(post, realm, clock)
        [cmd] = realm.ports.sent
        assert (type(cmd), cmd.from_guid, cmd.subject, cmd.body, cmd.items, cmd.money) == (
            protocol.MailSend, 102, "Missed you", "The guild is quiet.", (), 0)
        assert 600 <= cmd.delay_s <= 3600
        assert mails(db, "state,command_id") == [("dispatched", cmd.id)]
        assert direct_rows(db) == [("mail", f"mail:{mail_id}", "mail_send", "acked", None, None)]
        await tick(post, realm, clock)
        assert mails(db, "state") == [("dispatched",)]  # The job is still to come.
        assert job(realm, cmd, value=0)
        await tick(post, realm, clock)
        assert mails(db, "state,item_guid") == [("sent", 0)]
        assert direct_rows(db)[0][3:] == ("acked", None, "0")
        for _ in range(5):
            await tick(post, realm, clock, HOUR)
        assert realm.ports.sent == [cmd]        # Sent once.
    run(go())


def test_letter_is_private(realm, db, clock):
    seed_test_cast(db)                          # Her stored persona: the writer's owner check.
    member(realm, 102, woman=True, stance="drawn", close=60)
    realm.ports.writer = MemoryWriter(db, Embedder(LLMScheduler(FakeLLM(), clock=clock), "emb"), clock)
    mail_id = ready(db, subject="Missed you")
    post = mail.PostOffice(realm, rng=Never(3))

    async def go():
        await tick(post, realm, clock)
        [cmd] = realm.ports.sent
        job(realm, cmd)
        await tick(post, realm, clock)
        await tick(post, realm, clock)          # Its memory is written once.
    run(go())
    [memory] = realm.ports.remembered
    assert (memory.owner, memory.scope, memory.scope_with, memory.key, memory.actors) == (
        102, "private", 1, f"realm-mail:{mail_id}", (1,))
    assert memory.text == 'I mailed Boss a letter: "Missed you".'
    row = db.conn.execute("SELECT owner,scope,scope_with FROM memory WHERE key=?",
                          (f"realm-mail:{mail_id}",)).fetchone()
    assert tuple(row) == (102, "private", 1)


def test_letter_not_held_by_raid(tmp_path, store, clock):
    """In the real service: his raid holds every body of the roster, the letter still goes."""
    from leeroy_mind.protocol import Hello
    from leeroy_mind.realm.service import attach_realm
    from realm.fakes import RealmApp, enabled

    app = RealmApp(tmp_path, store, clock)
    app.personas = seed_test_cast(store)
    app.world = SimpleNamespace(human_online=False, online=set(app.personas), afk=set(), group=None,
                                in_group=lambda guid: True)
    app.guild = SimpleNamespace(raid_night=SimpleNamespace(state=SimpleNamespace(name="WAITING_CALL"),
                                                           session=None, _closing=[]))
    enabled(tmp_path, True)
    realm = attach_realm(app)
    post = realm.parts["mail"]
    assert type(post) is mail.PostOffice and realm.on_event in app.handlers["mail_taken"]

    async def go():
        await app.connection(True)
        await app.event(Hello(0, "test", "1", 21, 1))
        assert realm.ports.raid_holds_all() and realm.ports.human_online()
        [hold] = realm.ports.raid_body_holds()
        assert {101, 102, 103} <= hold.guids    # Every body of his roster is held.
        mail_id = ready(store, sender=102)
        await post.tick()
        await asyncio.gather(*realm._children, return_exceptions=True)
        [cmd] = [c for c in app.sent if type(c) is protocol.MailSend]
        assert cmd.from_guid == 102 and realm.action_store.get(cmd.id) is None  # Never queued.
        assert realm.parts["direct"].state("mail", f"mail:{mail_id}") == ("acked", None, None)
        await app.event(protocol.RealmJob(int(clock.now() * 1000), cmd.id, "mail_send", "done", "", 102, 9001))
        clock.t += 30
        await post.tick()
        assert mails(store, "state,item_guid") == [("sent", 9001)]
        await realm.close()
    run(go())


def test_three_texts_a_day(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    with db.transaction():
        ids = [db.conn.execute("INSERT INTO realm_mail(ts,sender,kind,subject,body) VALUES (?,102,'gift','','')",
                               (T0 + n,)).lastrowid for n in range(5)]
    assert [w.id for w in mail.texts_wanted(db.conn)] == ids[:3]          # Three texts a night.
    assert [w.id for w in mail.texts_wanted(db.conn, limit=10)] == ids[:3]
    assert [w.id for w in mail.texts_wanted(db.conn, limit=1)] == ids[:1]
    for n, mail_id in enumerate(ids):
        assert mail.write_text(db, mail_id, subject=f"For you {n}", body="")
    post = mail.PostOffice(realm, rng=Never(1))

    async def go():
        for _ in range(12):
            await tick(post, realm, clock, 30)
            for cmd in realm.ports.sent:
                job(realm, cmd, value=0)
        assert [c.subject for c in realm.ports.sent] == ["For you 0", "For you 1", "For you 2"]
        clock.t = datetime(2026, 11, 4, 0, 5, tzinfo=timezone.utc).timestamp()  # The next day.
        for _ in range(6):
            await tick(post, realm, clock, 30)
            for cmd in realm.ports.sent:
                job(realm, cmd, value=0)
    run(go())
    assert [c.subject for c in realm.ports.sent] == [f"For you {n}" for n in range(5)]
    assert mails(db, "state") == [("sent",)] * 5


# -- gifts ------------------------------------------------------------------------------------

def test_gifts_rationed(realm, db, clock):
    for guid in (101, 102, 103, 104, 105):
        member(realm, guid, woman=True, stance=("fond", "drawn", "lust")[guid % 3], close=50)
    member(realm, 106, woman=True)                  # No attitude: never a gift.
    member(realm, 107, stance="none")               # Men stay friends.
    post = mail.PostOffice(realm, rng=Always(5))

    async def go():
        for _ in range(6 * 24):                     # Six days of the product week.
            await tick(post, realm, clock, HOUR)
    run(go())
    gifts = mails(db, "sender,kind,items")
    assert len(gifts) == 2 and len({sender for sender, _, _ in gifts}) == 2
    for sender, kind, items in gifts:
        [item] = json.loads(items)
        assert sender in {101, 102, 103, 104, 105} and kind == "gift"
        assert item["from"] == "new" and item["item_id"] in mail.FLASKS and 1 <= item["count"] <= 20

    async def next_week():
        clock.t = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp()
        await tick(post, realm, clock, HOUR)
    run(next_week())
    assert len(mails(db)) == 3                       # A new week, a new allowance.


def test_bank_gift_skipped_without_rights(realm, db, clock):
    member(realm, 102, woman=True, stance="lust", close=60)
    realm.bank = Bank(answers=("no_rights",))
    post = mail.PostOffice(realm, rng=Always(2))

    async def go():
        await tick(post, realm, clock)
        [(mail_id, items)] = mails(db, "id,items")
        assert json.loads(items) == [{"item_id": 47586, "count": 1, "from": "bags", "bank_tab": 3}]
        assert mail.write_text(db, mail_id, subject="Saw this and thought of you", body="")
        await tick(post, realm, clock)
        await tick(post, realm, clock)
    run(go())
    assert realm.bank.calls == [(f"mail:{mails(db, 'id')[0][0]}", 102, 47586, 1, 3)]
    assert mails(db, "state,reason") == [("skipped", "no_rights")]
    assert realm.ports.sent == [] and direct_rows(db) == []
    assert db.conn.execute("SELECT COUNT(*) FROM realm_bank").fetchone()[0] == 0


def test_bank_gift_sent_from_her_bags_after_the_withdrawal(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    realm.bank = Bank(answers=("pending", "pending", "done"))
    mail_id = ready(db, kind="gift", subject="For you", items=[
        {"item_id": 47586, "count": 1, "from": "bags", "bank_tab": 3}])
    post = mail.PostOffice(realm, rng=Never(4))

    async def go():
        await tick(post, realm, clock)
        await tick(post, realm, clock)
        assert realm.ports.sent == []               # Still in the bank.
        await tick(post, realm, clock)
        [cmd] = realm.ports.sent
        assert cmd.items == (protocol.MailItem(47586, 1, "bags"),)
        job(realm, cmd, value=9002)
        await tick(post, realm, clock)
    run(go())
    assert len(realm.bank.calls) == 3 and {c[0] for c in realm.bank.calls} == {f"mail:{mail_id}"}
    assert mails(db, "state,item_guid") == [("sent", 9002)]


def test_holiday_gift_once_a_year(realm, db, clock):
    member(realm, 101, close=95)
    member(realm, 102, woman=True, stance="fond", close=40)
    post = mail.PostOffice(realm, rng=Never(9))

    async def go():
        await post.on_event(protocol.GameEvent(1, mail.LOVE_IS_IN_THE_AIR, "start"))
        await post.on_event(protocol.GameEvent(2, mail.LOVE_IS_IN_THE_AIR, "start"))  # Once a year.
        await post.on_event(protocol.GameEvent(3, mail.HALLOWS_END, "stop"))
        await post.on_event(protocol.GameEvent(4, 24, "start"))                       # Brewfest: none.
        await post.on_event(protocol.GameEvent(5, mail.HALLOWS_END, "start"))
    run(go())
    rows = mails(db, "sender,kind,items")
    assert [(s, k) for s, k, _ in rows] == [(102, "holiday"), (101, "holiday")]
    love, hallow = (json.loads(items)[0] for _, _, items in rows)
    assert love["item_id"] in mail.HOLIDAY_ITEMS[mail.LOVE_IS_IN_THE_AIR] and love["from"] == "new"
    assert hallow["item_id"] in mail.HOLIDAY_ITEMS[mail.HALLOWS_END]


def test_birthday_gifts_at_most_three_on_the_day(realm, db, clock):
    for guid, close in ((101, 10), (102, 80), (103, 60), (104, 70), (105, 30)):
        member(realm, guid, close=close)
    realm.cfg = RealmConfig(enabled=True, his_birthday="11-04")
    post = mail.PostOffice(realm, rng=Never())

    async def go():
        await tick(post, realm, clock)          # The day before: decided, not yet due.
        await tick(post, realm, clock)
        assert sorted(s for s, in mails(db, "sender")) == [102, 103, 104]
        for mail_id, in mails(db, "id"):
            assert mail.write_text(db, mail_id, subject="Happy birthday", body="")
        for _ in range(3):
            await tick(post, realm, clock, HOUR)
        assert realm.ports.sent == []           # Not before the day.
        clock.t = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc).timestamp()
        for _ in range(4):
            await tick(post, realm, clock, 30)
            for cmd in realm.ports.sent:
                job(realm, cmd, value=0)
        clock.t = datetime(2027, 11, 3, 9, 0, tzinfo=timezone.utc).timestamp()
        realm.ports.online = False
        await tick(post, realm, clock)          # A year on: this year's three.
    run(go())
    assert sorted(c.from_guid for c in realm.ports.sent) == [102, 103, 104]
    assert len(mails(db)) == 6 and {k for k, in mails(db, "kind")} == {"birthday"}


# -- what he takes ------------------------------------------------------------------------------

def test_mail_taken_is_her_memory(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    mail_id = ready(db, kind="gift", subject="For your raids", items=[
        {"item_id": 46377, "count": 3, "from": "new"}])
    post = mail.PostOffice(realm, rng=Never(5))

    async def go():
        await tick(post, realm, clock)
        [cmd] = realm.ports.sent
        job(realm, cmd, value=9001)
        await tick(post, realm, clock)
        await post.on_event(protocol.MailTaken(1, 4242, 46377, 3))   # Not ours: nothing.
        assert mails(db, "state") == [("sent",)]
        await post.on_event(protocol.MailTaken(2, 9001, 46377, 3))
        await post.on_event(protocol.MailTaken(3, 9001, 46377, 3))   # Once.
        await realm.settle()
        await tick(post, realm, clock)
    run(go())
    assert mails(db, "state,item_guid") == [("taken", 9001)]
    assert db.conn.execute("SELECT taken FROM realm_mail").fetchone()[0] is not None
    sent, taken = realm.ports.remembered
    assert sent.text == 'I mailed Boss a gift (Flask of Endless Rage): "For your raids".'
    assert (taken.owner, taken.text, taken.scope, taken.scope_with, taken.key) == (
        102, "Boss took my Flask of Endless Rage.", "private", 1, f"realm-mail-taken:{mail_id}")
    assert len(realm.ports.sent) == 1           # Nothing is sent for it.


# -- exactly once ------------------------------------------------------------------------------

def test_dispatched_mail_is_never_sent_again(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    ready(db)
    hang = asyncio.Event()

    async def wire(cmd):
        await hang.wait()                       # On the wire; the ack never comes.

    realm.ports.wire = wire
    post = mail.PostOffice(realm, rng=Never(6))

    async def go():
        await post.tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        [cmd] = realm.ports.sent
        for task in realm.tasks:
            task.cancel()                       # A disable or restart while it is out.
        await realm.settle()
        assert mails(db, "state") == [("dispatched",)]
        # The next process: the ledger makes the intent unknown, never sent again.
        realm.direct = DirectLedger(db, process_id="next-process", clock=clock)
        realm.direct.recover(process_id="next-process")
        later = mail.PostOffice(realm, rng=Never(6))
        for _ in range(4):
            await tick(later, realm, clock, HOUR)
    run(go())
    assert len(realm.ports.sent) == 1
    assert mails(db, "state,reason") == [("failed", "recovered_dispatched")]


def test_a_mail_that_never_left_goes_once_later(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    mail_id = ready(db)
    refusals = [RealmDisabled("realm disabled")]

    async def before(cmd):
        if refusals:
            raise refusals.pop()                # Before the barrier: it never reached the wire.

    realm.ports.before = before
    post = mail.PostOffice(realm, rng=Never(8))

    async def go():
        await tick(post, realm, clock)
        assert realm.ports.sent == [] and mails(db, "state,command_id") == [("ready", None)]
        assert realm.direct.state("mail", f"mail:{mail_id}")[0] == "abandoned"
        await tick(post, realm, clock, 60)      # Tried again only after a pause.
        assert realm.ports.sent == []
        await tick(post, realm, clock, 600)
        [cmd] = realm.ports.sent
        job(realm, cmd)
        await tick(post, realm, clock)
    run(go())
    assert mails(db, "state") == [("sent",)]


def test_refused_ack_retried_then_failed(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    ready(db)
    realm.ports.ack = (False, "realm_off")      # Native never queued it.
    post = mail.PostOffice(realm, rng=Never(2))

    async def go():
        for _ in range(8):
            await tick(post, realm, clock, 601)
    run(go())
    assert len(realm.ports.sent) == mail.ACK_TRIES and len({c.id for c in realm.ports.sent}) == 3
    assert mails(db, "state,reason") == [("failed", "realm_off")]


def test_a_failed_job_is_final(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    ready(db)
    post = mail.PostOffice(realm, rng=Never(2))

    async def go():
        await tick(post, realm, clock)
        [cmd] = realm.ports.sent
        job(realm, cmd, state="failed", reason="mail_commit_failed")  # It may have been done.
        for _ in range(4):
            await tick(post, realm, clock, HOUR)
    run(go())
    assert len(realm.ports.sent) == 1
    assert mails(db, "state,reason") == [("failed", "mail_commit_failed")]


def test_a_busy_sender_waits(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    ready(db)
    post = mail.PostOffice(realm, rng=Never(2))

    async def go():
        realm.app.world.online.discard(102)     # Offline: native would refuse.
        await tick(post, realm, clock)
        realm.app.world.online.add(102)
        realm.ports.offline.add(102)            # In an offline raid.
        await tick(post, realm, clock)
        realm.ports.offline.clear()
        realm.ports.raid, realm.app.world.group = "RUNNING", {102}  # In his raid's instance.
        await tick(post, realm, clock)
        assert realm.ports.sent == []
        realm.ports.raid = "WAITING_CALL"       # Held by his raid's schedule only: it goes.
        await tick(post, realm, clock)
    run(go())
    assert len(realm.ports.sent) == 1


def test_post_from_another_part(realm, db, clock):
    member(realm, 102, woman=True, stance="fond", close=60)
    post = mail.PostOffice(realm, rng=Never(2))
    prize = post.post(102, "prize", items=(protocol.MailItem(8490, 1, "new"),),
                      subject="First place", body="Well fished.")
    minutes = post.post(102, "minutes")
    assert post.post(1, "prize", subject="Me to me") is None              # Never from him.
    assert post.post(102, "prize", subject="x" * 65) is None
    assert post.post(102, "Prize!", subject="ok") is None
    assert post.post(102, "prize", items=(protocol.MailItem(8490, 21, "new"),), subject="ok") is None
    assert mails(db, "id,kind,state") == [(prize, "prize", "ready"), (minutes, "minutes", "awaiting_text")]
