"""The guild's mail to him: the letter after a long absence, gifts, and what other parts post.

Every mail is one realm_mail row and reaches native at most once, through the direct ledger
(owner "mail", ref "mail:<id>", a job expected: its value is the first item's guid, 0 with no
item). mail_send moves no body, so neither the body ledger nor his raid's hold ever holds it;
native fences the sender itself (Purpose::Mail). A row's states:

  awaiting_text  decided; the night (Task 19) has not written its text yet, so it waits.
  ready          written (write_text, or posted with its text). It goes while he is online,
                 once its ts has come, at most three a day, with a delivery delay of
                 600-3,600 s: a letter waiting at his login arrives 10-60 minutes into it.
  dispatched     its one intent is in the ledger (command_id); it may be on the wire.
  sent           native's job said done; item_guid is its first item's.
  taken          he took that item from his mail (mail_taken).
  failed         refused by native once it had the command, or unknown: never sent again.
  skipped        never sent, with its reason (a bank gift without his rights: no_rights).

A send that never reached native (abandoned before the wire, refused before transport, or
refused by native's ack, at most ACK_TRIES times) makes the row ready again for a later try.

What arrives:
  - the letter after a long absence: once per absence, 7 days after his last logout, from the
    member closest to him;
  - gifts from women with an attitude: a BoE from the guild bank (part "bank") or flasks created
    new; at most 2 a product week guild-wide and one a day;
  - holiday gifts when Love is in the Air or Hallow's End starts, once a year each;
  - birthday gifts when [realm] his_birthday is set: up to 3, decided the day before and due on
    the day (a dated row's ts is the moment it may go);
  - whatever another part posts (PostOffice.post): minutes, crafted goods, prizes.

The sender's memory of a mail and her memory of his taking it are private with him (keys
realm-mail:<id> and realm-mail-taken:<id>). No text, name or subject is ever logged.
"""
from __future__ import annotations

import json
import logging
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from leeroy_mind.guild.roster import closeness
from leeroy_mind.memory.writer import MemoryCandidate
from leeroy_mind.personas.screens import names_real_name
from leeroy_mind.personas.stances import is_woman
from leeroy_mind.protocol import Ack

from .calendar_time import product_period
from .config import LIMITS
from .protocol import GameEvent, MailItem, MailSend, MailTaken

log = logging.getLogger(__name__)

OWNER = "mail"
EVENT_TYPES = ("mail_taken", "game_event")
TICK_S = 10            # settle and send
PLAN_S = 60            # decide what arrives
RETRY_S = 600          # a send that never reached native waits this long before another try
ACK_TRIES = 3          # native's ack refusals before a mail is given up
GIFTS_PER_WEEK = 2
GIFT_DAY_P = 0.3       # the chance of a gift on a day the week still allows one
BOE_SHARE = 0.5        # of gifts, those from the bank when it has a BoE to give
BIRTHDAY_SENDERS = 3
ATTITUDES = frozenset({"fond", "drawn", "lust"})
LOVE_IS_IN_THE_AIR, HALLOWS_END = 8, 12  # AzerothCore game_event entries
FLASKS = (46377, 46376, 46379, 46378)
FEASTS = (43015, 34753)
HOLIDAY_ITEMS = {LOVE_IS_IN_THE_AIR: (22218, 22236, 22237, 22238, 22239), HALLOWS_END: (37584, 37585)}
GIFT_COUNT, HOLIDAY_COUNT = 3, 5
NAMES = {43015: "Fish Feast", 34753: "Great Feast", 46377: "Flask of Endless Rage",
         46376: "Flask of the Frost Wyrm", 46379: "Flask of Stoneblood", 46378: "Flask of Pure Mojo",
         22218: "Handful of Rose Petals", 22236: "Buttermilk Delight", 22237: "Dark Desire",
         22238: "Very Berry Cream", 22239: "Sweet Surprise", 37584: "Soothing Spearmint Candy",
         37585: "Chewy Fel Taffy"}
_META = "realm:mail:"
_KIND = re.compile(r"[a-z][a-z_]{0,31}")
_SOURCES = frozenset({"new", "bags"})
_U32 = 0xFFFFFFFF
_IN_INSTANCE = frozenset({"ASSEMBLING", "RUNNING"})


@dataclass(frozen=True, slots=True)
class MailNeed:
    """A mail waiting for its text, for the night: items are (item_id, count, name)."""
    id: int
    sender: int
    kind: str
    items: tuple[tuple[int, int, str], ...]
    ts: float


def _ref(mail_id: int) -> str:
    return f"mail:{mail_id}"


def _bounded(text, lo: int, hi: int) -> bool:
    if type(text) is not str or "\0" in text:
        return False
    try:
        return lo <= len(text.encode("utf-8")) <= hi
    except UnicodeError:
        return False


def _text_ok(subject, body, human=None) -> bool:
    """Native's bounds (subject 1-64 bytes, body 0-512), and never his real name."""
    if not _bounded(subject, 1, 64) or not _bounded(body, 0, 512):
        return False
    return human is None or not (names_real_name(subject, human) or names_real_name(body, human))


def _item(raw) -> dict | None:
    if type(raw) is not dict or raw.get("from") not in _SOURCES:
        return None
    item_id, count, tab = raw.get("item_id"), raw.get("count"), raw.get("bank_tab", 0)
    if (type(item_id) is not int or not 0 < item_id <= _U32 or type(count) is not int
            or not 1 <= count <= 20 or type(tab) is not int or not 0 <= tab <= 5
            or ("bank_tab" in raw and raw["from"] != "bags")):
        return None
    return raw


def _items(text) -> list[dict] | None:
    try:
        raw = json.loads(text)
    except (TypeError, ValueError):
        return None
    if type(raw) is not list or len(raw) > 4:
        return None
    items = [_item(entry) for entry in raw]
    return None if any(entry is None for entry in items) else items


def _name(item: dict) -> str:
    name = item.get("name")
    return name if type(name) is str and 0 < len(name) <= 64 else NAMES.get(item["item_id"], "gift")


def texts_wanted(conn, *, limit: int = LIMITS.mail_texts_per_day) -> list[MailNeed]:
    """The mails the night should write next, oldest first: at most three a day."""
    limit = max(0, min(int(limit), LIMITS.mail_texts_per_day))
    out = []
    for row in conn.execute("SELECT id,sender,kind,items,ts FROM realm_mail WHERE state='awaiting_text' "
                            "ORDER BY ts,id LIMIT ?", (limit,)).fetchall():
        items = _items(row[3])
        if items is not None:
            out.append(MailNeed(row[0], row[1], row[2],
                                tuple((i["item_id"], i["count"], _name(i)) for i in items), float(row[4])))
    return out


def write_text(store, mail_id: int, *, subject: str, body: str, human=None) -> bool:
    """The night's text for one mail waiting for it, which becomes ready; joins the caller's Store
    unit when inside one. False (nothing written) when the text is out of native's bounds or names
    his real name (with `human`), or the mail is not waiting for text."""
    if type(mail_id) is not int or not _text_ok(subject, body, human):
        return False
    with store.transaction():
        return store.conn.execute(
            "UPDATE realm_mail SET subject=?,body=?,state='ready' WHERE id=? AND state='awaiting_text'",
            (subject, body, mail_id)).rowcount == 1


class PostOffice:
    """The mail part: decides what arrives, sends what is written, and remembers it."""

    name = "mail"

    def __init__(self, realm, *, rng=None):
        self.realm = realm
        self.rng = rng if rng is not None else random.Random()
        self._next_tick = self._next_plan = None
        self._inflight = set()      # mail ids whose send child runs
        self._acks = {}             # mail id -> the ack its child saw
        self._not_before = {}       # mail id -> its next try, after a send that never reached native
        self._ack_refusals = {}     # mail id -> native's ack refusals so far
        self._remembered = set()    # memory keys written, or being written, by this process

    @property
    def _store(self):
        return self.realm.app.store

    @property
    def _conn(self):
        return self.realm.app.store.conn

    def _now(self) -> float:
        return self.realm.app.clock.now()

    def _day(self) -> str:
        return self.realm.app.clock.local().date().isoformat()

    def _meta(self, key):
        value = self._store.get_meta(_META + key)
        try:
            return None if value is None else json.loads(value)
        except ValueError:
            return None

    def _set_meta(self, key, value) -> None:
        self._store.set_meta(_META + key, json.dumps(value, sort_keys=True))

    def _insert(self, ts, sender, kind, items=(), subject="", body="", state="awaiting_text") -> int:
        return self._conn.execute(
            "INSERT INTO realm_mail(ts,sender,kind,subject,body,items,state) VALUES (?,?,?,?,?,?,?)",
            (float(ts), sender, kind, subject, body, json.dumps(list(items), sort_keys=True), state)).lastrowid

    # -- the part ---------------------------------------------------------------------------

    async def tick(self) -> None:
        if not self.realm.enabled:
            return
        now = self._now()
        if self._next_tick is not None and now < self._next_tick:
            return
        self._next_tick = now + TICK_S
        steps = [self._reconcile]
        if self._next_plan is None or now >= self._next_plan:
            self._next_plan = now + PLAN_S
            steps += [self._plan, self._catch_up_memories]
        steps.append(self._send_next)
        for step in steps:
            try:
                step()
            except Exception as error:  # Never the Realm loop's failure.
                log.warning("realm_mail_failed step=%s error=%s", step.__name__, type(error).__name__)

    async def on_event(self, ev) -> None:
        if not self.realm.enabled:
            return
        try:
            if type(ev) is MailTaken:
                self.on_mail_taken(ev)
            elif type(ev) is GameEvent:
                self._on_game_event(ev)
        except Exception as error:
            log.warning("realm_mail_event_failed error=%s", type(error).__name__)

    # -- what arrives -----------------------------------------------------------------------

    def _cast(self, human: int) -> dict:
        """The members who may write: on the roster with a persona, not him, not away."""
        ports = self.realm.ports
        away = set(ports.away())
        out = {}
        for member in ports.cast():
            guid = getattr(member, "guid", None)
            if type(guid) is int and 0 < guid <= _U32 and guid != human and guid not in away:
                persona = ports.persona(guid)
                if persona is not None:
                    out[guid] = persona
        return out

    @staticmethod
    def _attitude(persona) -> bool:
        return is_woman(persona) and persona.romance.stance in ATTITUDES

    def _closest(self, guids, human: int) -> list[int]:
        def score(guid):
            rel = self.realm.ports.relationship(guid, human)
            return closeness(rel.affinity, rel.trust, rel.familiarity)
        return sorted(guids, key=lambda guid: (-score(guid), guid))

    def _human(self) -> int:
        guid = self.realm.ports.human().guid
        return guid if type(guid) is int and 0 < guid <= _U32 else 0

    def _plan(self) -> None:
        human = self._human()
        cast = self._cast(human) if human else {}
        if not cast:
            return
        now = self._now()
        for plan in (self._plan_letter, self._plan_gift, self._plan_birthday):
            try:
                plan(now, human, cast)
            except Exception as error:
                log.warning("realm_mail_plan_failed plan=%s error=%s", plan.__name__, type(error).__name__)

    def _plan_letter(self, now, human, cast) -> None:
        """Once per absence: his newest session, closed a week or more ago."""
        row = self._conn.execute("SELECT id,ended FROM session ORDER BY started DESC,id DESC LIMIT 1").fetchone()
        if row is None or row[1] is None or now - float(row[1]) < LIMITS.absence_letter_after_s:
            return
        session = int(row[0])
        if self._meta("absence") == session:
            return
        sender = self._closest(cast, human)[0]
        with self._store.transaction():
            mail_id = self._insert(now, sender, "letter")
            self._set_meta("absence", session)
        log.info("realm_mail_letter mail=%d", mail_id)

    def _plan_gift(self, now, human, cast) -> None:
        """At most one a day, two a product week across the guild, each from a woman with an
        attitude who has not sent one this week."""
        day = self._day()
        if self._meta("gift_day") == day:
            return
        period = product_period(now)
        with self._store.transaction():
            self._set_meta("gift_day", day)
            senders = {r[0] for r in self._conn.execute(
                "SELECT sender FROM realm_mail WHERE kind='gift' AND ts>=? AND ts<?",
                (period.starts_at, period.ends_at))}
            count = self._conn.execute("SELECT COUNT(*) FROM realm_mail WHERE kind='gift' AND ts>=? AND ts<?",
                                       (period.starts_at, period.ends_at)).fetchone()[0]
            women = sorted(g for g, p in cast.items() if self._attitude(p) and g not in senders)
            if count >= GIFTS_PER_WEEK or not women or self.rng.random() >= GIFT_DAY_P:
                return
            sender = self.rng.choice(women)
            mail_id = self._insert(now, sender, "gift", self._gift_items())
        log.info("realm_mail_gift mail=%d", mail_id)

    def _gift_items(self) -> list[dict]:
        bank = self.realm.part("bank")
        if bank is not None and self.rng.random() < BOE_SHARE:
            try:
                offer = bank.gift_boe()
            except Exception as error:
                log.warning("realm_mail_bank_unavailable error=%s", type(error).__name__)
                offer = None
            if (type(offer) is tuple and len(offer) == 2 and all(type(v) is int for v in offer)
                    and 0 < offer[0] <= _U32 and 0 <= offer[1] <= 5):
                return [{"item_id": offer[0], "count": 1, "from": "bags", "bank_tab": offer[1]}]
        return [{"item_id": self.rng.choice(FLASKS), "count": GIFT_COUNT, "from": "new"}]

    def _plan_birthday(self, now, human, cast) -> None:
        """His birthday, today or tomorrow: up to three of the closest members, due on the day."""
        birthday = getattr(getattr(self.realm, "cfg", None), "his_birthday", "")
        if not birthday:
            return
        local = self.realm.app.clock.local()
        day = next((d for d in (local.date(), local.date() + timedelta(days=1))
                    if d.strftime("%m-%d") == birthday), None)
        if day is None or self._meta(f"birthday:{day.year}") is not None:
            return
        due = datetime.combine(day, time(0), tzinfo=local.tzinfo).timestamp()
        senders = self._closest(cast, human)[:BIRTHDAY_SENDERS]
        with self._store.transaction():
            for sender in senders:
                self._insert(due, sender, "birthday",
                             [{"item_id": self.rng.choice(FEASTS), "count": 1, "from": "new"}])
            self._set_meta(f"birthday:{day.year}", len(senders))
        log.info("realm_mail_birthday mails=%d", len(senders))

    def _on_game_event(self, ev) -> None:
        """A holiday's start: one gift a year, for Love is in the Air from the closest woman with
        an attitude (else the closest member), for Hallow's End from the closest member."""
        if ev.state != "start" or ev.event_id not in HOLIDAY_ITEMS:
            return
        key = f"holiday:{ev.event_id}:{self.realm.app.clock.local().year}"
        human = self._human()
        cast = self._cast(human) if human else {}
        if not cast or self._meta(key) is not None:
            return
        pool = list(cast)
        if ev.event_id == LOVE_IS_IN_THE_AIR:
            pool = [g for g, p in cast.items() if self._attitude(p)] or pool
        sender = self._closest(pool, human)[0]
        item = self.rng.choice(HOLIDAY_ITEMS[ev.event_id])
        with self._store.transaction():
            mail_id = self._insert(self._now(), sender, "holiday",
                                   [{"item_id": item, "count": HOLIDAY_COUNT, "from": "new"}])
            self._set_meta(key, mail_id)
        log.info("realm_mail_holiday mail=%d event=%d", mail_id, ev.event_id)

    def post(self, sender: int, kind: str, *, items=(), subject: str = "", body: str = "",
             due: float | None = None) -> int | None:
        """A mail another part hands in (the meeting's minutes, crafted goods, a prize): its row
        id, or None when refused. With a subject it is ready to go; without one it waits for the
        night's text. items are MailItems (at most 4). Nothing while the Realm is disabled."""
        if not self.realm.enabled:
            return None
        human = self.realm.ports.human()
        if (type(sender) is not int or not 0 < sender <= _U32 or sender == human.guid
                or type(kind) is not str or _KIND.fullmatch(kind) is None
                or type(items) not in (tuple, list) or len(items) > 4
                or (subject != "" and not _text_ok(subject, body, human))
                or (due is not None and type(due) not in (int, float))):
            return None
        rows = []
        for item in items:
            row = _item({"item_id": item.item_id, "count": item.count, "from": item.from_}
                        if type(item) is MailItem else None)
            if row is None:
                return None
            rows.append(row)
        with self._store.transaction():
            return self._insert(self._now() if due is None else due, sender, kind, rows, subject,
                                body if subject else "", "ready" if subject else "awaiting_text")

    # -- sending ----------------------------------------------------------------------------

    def _sent_today(self) -> int:
        value = self._meta("sent")
        return (value["n"] if type(value) is dict and value.get("day") == self._day()
                and type(value.get("n")) is int else 0)

    def _count(self, delta: int) -> None:
        self._set_meta("sent", {"day": self._day(), "n": max(0, self._sent_today() + delta)})

    def _sender_free(self, guid: int) -> bool:
        """Native would refuse her now: offline, or in an instance (an offline raid, Training,
        his raid's instance). Not his raid's schedule: mail holds no body."""
        world = self.realm.app.world
        online = getattr(world, "online", None)
        if online is not None and guid not in online:
            return False
        ports = self.realm.ports
        try:
            if guid in ports.offline_raid_members() or guid in ports.training_members():
                return False
            in_group = getattr(world, "in_group", None)
            return not (ports.raid_state() in _IN_INSTANCE and in_group is not None and in_group(guid))
        except Exception:
            return False

    def _send_next(self) -> None:
        """At most one mail a tick: the oldest ready one that is due and whose sender is free."""
        if not self.realm.ports.human_online() or self.realm.part("direct") is None:
            return
        context = self.realm.action_context()
        if not (context.enabled and context.connected and context.presence_known):
            return
        if self._sent_today() >= LIMITS.mail_texts_per_day:
            return
        now = self._now()
        human = self.realm.ports.human()
        for mail_id, sender, subject, body, raw in self._conn.execute(
                "SELECT id,sender,subject,body,items FROM realm_mail WHERE state='ready' AND ts<=? "
                "ORDER BY ts,id", (now,)).fetchall():
            if mail_id in self._inflight or self._not_before.get(mail_id, 0) > now:
                continue
            items = _items(raw)
            if items is None or not _text_ok(subject, body, human):
                self._skip(mail_id, "invalid_mail")
                continue
            if not self._sender_free(sender):
                continue
            withdrawn = self._withdraw(mail_id, sender, items)
            if withdrawn == "pending":
                continue
            if withdrawn != "done":
                self._skip(mail_id, withdrawn)
                continue
            self._dispatch(mail_id, sender, subject, body, items, context)
            return

    def _withdraw(self, mail_id, sender, items) -> str:
        """A bank gift's item into her bags first: "done", "pending" or the reason to skip it."""
        banked = [item for item in items if "bank_tab" in item]
        if not banked:
            return "done"
        bank = self.realm.part("bank")
        if bank is None or len(banked) > 1:
            return "no_bank"
        item = banked[0]
        try:
            state = bank.withdraw_gift(_ref(mail_id), sender, item["item_id"], item["count"], item["bank_tab"])
        except Exception as error:
            log.warning("realm_mail_bank_unavailable error=%s", type(error).__name__)
            return "pending"
        if state in ("done", "pending", "no_rights"):
            return state
        return "withdraw_failed"

    def _skip(self, mail_id, reason) -> None:
        with self._store.transaction():
            self._conn.execute("UPDATE realm_mail SET state='skipped',reason=? WHERE id=? AND state='ready'",
                               (reason, mail_id))
        log.info("realm_mail_skipped mail=%d reason=%s", mail_id, reason)

    def _dispatch(self, mail_id, sender, subject, body, items, context) -> None:
        direct = self.realm.part("direct")
        cmd = MailSend(uuid.uuid4().hex, sender, subject, body,
                       tuple(MailItem(i["item_id"], i["count"], i["from"]) for i in items), 0,
                       self.rng.randint(LIMITS.mail_delay_min_s, LIMITS.mail_delay_max_s))
        with self._store.transaction():
            if direct.prepare(cmd, owner=OWNER, ref=_ref(mail_id), context=context, expects_job=True) is None:
                taken = direct.state(OWNER, _ref(mail_id))
                if taken is not None and taken[0] in ("prepared", "dispatched", "acked", "unknown"):
                    # Its ref is already live: whatever it holds is never sent again.
                    self._conn.execute("UPDATE realm_mail SET state='failed',reason='ref_taken' "
                                       "WHERE id=? AND state='ready'", (mail_id,))
                return
            if self._conn.execute("UPDATE realm_mail SET state='dispatched',command_id=?,reason=NULL "
                                  "WHERE id=? AND state='ready'", (cmd.id, mail_id)).rowcount != 1:
                raise RuntimeError("mail row changed before its dispatch")
            self._count(1)
        self._inflight.add(mail_id)
        task = self.realm.spawn(lambda: self._send(mail_id, cmd))
        if task is None:
            self._finish(mail_id, cmd)
        else:
            task.add_done_callback(lambda done: self._finish(mail_id, cmd))

    async def _send(self, mail_id, cmd) -> None:
        try:
            self._acks[mail_id] = await self.realm.ports.send(cmd)
        except Exception as error:  # The ledger row records the outcome.
            log.debug("realm_mail_send_failed mail=%d error=%s", mail_id, type(error).__name__)

    def _finish(self, mail_id, cmd) -> None:
        """After the send child: close an intent that never reached the barrier, then settle."""
        self._inflight.discard(mail_id)
        ack = self._acks.pop(mail_id, None)
        try:
            direct = self.realm.part("direct")
            if direct is not None:
                direct.abandon_unsent(cmd)
            self._settle(mail_id, refused_at_ack=type(ack) is Ack and ack.ok is False)
        except Exception as error:
            log.warning("realm_mail_settle_failed error=%s", type(error).__name__)

    def _reconcile(self) -> None:
        for (mail_id,) in self._conn.execute(
                "SELECT id FROM realm_mail WHERE state='dispatched' ORDER BY id").fetchall():
            if mail_id not in self._inflight:
                self._settle(mail_id)

    def _settle(self, mail_id, *, refused_at_ack=False) -> None:
        """A dispatched row from its intent's state in the ledger."""
        direct = self.realm.part("direct")
        if direct is None:
            return
        found = direct.state(OWNER, _ref(mail_id))
        state, reason, value = found if found is not None else ("abandoned", None, None)
        if state in ("prepared", "dispatched") or (state == "acked" and value is None):
            return  # In flight, or native has it and its job is still to come.
        retry = state == "abandoned" or (state == "refused" and reason == "pretransport_refused")
        if state == "refused" and refused_at_ack and not retry:
            self._ack_refusals[mail_id] = self._ack_refusals.get(mail_id, 0) + 1
            retry = self._ack_refusals[mail_id] < ACK_TRIES
        with self._store.transaction():
            if self._conn.execute("SELECT 1 FROM realm_mail WHERE id=? AND state='dispatched'",
                                  (mail_id,)).fetchone() is None:
                return
            if state == "acked":
                self._conn.execute("UPDATE realm_mail SET state='sent',item_guid=?,reason=NULL WHERE id=?",
                                   (value if 0 <= value <= _U32 else 0, mail_id))
            elif retry:
                self._conn.execute("UPDATE realm_mail SET state='ready',command_id=NULL,reason=? WHERE id=?",
                                   (reason, mail_id))
                self._count(-1)
            else:
                self._conn.execute("UPDATE realm_mail SET state='failed',reason=? WHERE id=?",
                                   (reason or "unknown", mail_id))
        if state == "acked":
            log.info("realm_mail_sent mail=%d", mail_id)
            self._remember_sent(mail_id)
        elif retry:
            self._not_before[mail_id] = self._now() + RETRY_S
        else:
            log.info("realm_mail_failed mail=%d reason=%s", mail_id, reason or "unknown")

    # -- what he takes, and the memories ----------------------------------------------------

    def on_mail_taken(self, ev) -> int | None:
        """His taking a sent mail's first item: the mail is taken and she remembers it. Nothing
        is sent. The id of the mail, or None when it is none of ours."""
        if not self.realm.enabled or type(ev.item_guid) is not int or not 0 < ev.item_guid <= _U32:
            return None
        with self._store.transaction():
            row = self._conn.execute("SELECT id FROM realm_mail WHERE item_guid=? AND state='sent' "
                                     "ORDER BY id DESC LIMIT 1", (ev.item_guid,)).fetchone()
            if row is None:
                return None
            self._conn.execute("UPDATE realm_mail SET state='taken',taken=? WHERE id=? AND state='sent'",
                               (self._now(), row[0]))
        self._remember_taken(row[0])
        return row[0]

    def _row(self, mail_id):
        return self._conn.execute("SELECT sender,kind,subject,items FROM realm_mail WHERE id=?",
                                  (mail_id,)).fetchone()

    def _remember_sent(self, mail_id) -> None:
        row = self._row(mail_id)
        if row is None:
            return
        called = self.realm.ports.human().called
        items = _items(row[3]) or []
        what = f"a gift ({', '.join(_name(i) for i in items)})" if items else "a letter"
        self._remember(row[0], f'I mailed {called} {what}: "{row[2]}".', f"realm-mail:{mail_id}",
                       importance=4, valence=0.3)

    def _remember_taken(self, mail_id) -> None:
        row = self._row(mail_id)
        if row is None:
            return
        items = _items(row[3]) or []
        thing = _name(items[0]) if items else "gift"
        self._remember(row[0], f"{self.realm.ports.human().called} took my {thing}.",
                       f"realm-mail-taken:{mail_id}", importance=5, valence=0.5)

    def _remember(self, owner, text, key, *, importance, valence) -> None:
        """One private memory with him, keyed so a repeat updates it; written from an owned child."""
        human = self._human()
        if not human or key in self._remembered:
            return
        if self._conn.execute("SELECT 1 FROM memory WHERE owner=? AND key=?", (owner, key)).fetchone():
            self._remembered.add(key)
            return
        candidate = MemoryCandidate(owner, "event", text, int(self._now()), (human,), importance, valence,
                                    "game", (), key=key, scope="private", scope_with=human)
        self._remembered.add(key)
        task = self.realm.spawn(lambda: self.realm.ports.remember(candidate))
        if task is None:
            self._remembered.discard(key)
            return

        def done(finished):
            if finished.cancelled() or finished.exception() is not None:
                self._remembered.discard(key)  # Tried again at the next catch-up.
        task.add_done_callback(done)

    def _catch_up_memories(self) -> None:
        """Memories a disable or restart left unwritten, for the newest sent and taken mail."""
        for mail_id, state in self._conn.execute(
                "SELECT id,state FROM realm_mail WHERE state IN ('sent','taken') ORDER BY id DESC LIMIT 64").fetchall():
            self._remember_sent(mail_id)
            if state == "taken":
                self._remember_taken(mail_id)


def attach(realm):
    """The post office as a realm part, for mail_taken and game_event. No I/O."""
    return PostOffice(realm), EVENT_TYPES
