"""A realistic Realm action journal: rows made by ActionStore's own API, then copied.

Sized like the live journal on 2026-11-22 (counts only): about 270 root rows, mostly
completed Recall defaults (40 members each) and rearms (up to 145 members), a few
leaves refused by name, grows, operator releases and two unknown defaults that still
hold their members. Every row is invented. One row of each shape goes through the
store's reducers; copies change only the command id, so they pass the same validation
(building all 270 through prepare() would re-read the journal at every insert).
"""
from pathlib import Path
import sqlite3

from leeroy_mind.guild.protocol import BotDo, Job, JobSkip
from leeroy_mind.protocol import Ack
from leeroy_mind.realm import protocol as p
from leeroy_mind.realm.action_policy import classify
from leeroy_mind.realm.action_store import ActionSpec, ActionStore

MIGRATION = Path(__file__).parents[2] / "leeroy_mind/storage/migrations/0010_realm_actions.sql"
CAST = tuple(range(1001, 1151))  # 150 invented member guids


def journal_conn():
    conn = sqlite3.connect(":memory:", autocommit=False)
    conn.executescript(MIGRATION.read_text(encoding="utf-8-sig"))
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    return conn


def _members(start, size):
    return tuple(sorted(CAST[(start + i) % len(CAST)] for i in range(size)))


class _Builder:
    def __init__(self, store):
        self.store, self.now, self.n = store, 1_000_000.0, 0

    def tick(self):
        self.now += 120.0
        return self.now

    def root(self, cmd, owner, rank):
        c = classify(cmd)
        record = self.store.prepare(ActionSpec(cmd.id, None, "root", c.policy, owner, rank, c.kind,
                                               c.payload, c.reserved_manifest, 50, 1), now=self.tick())
        return self.store.begin_dispatch(record.command_id, payload=record.payload, process_id="process",
                                         connection_epoch=7, now=self.tick(), started_ms=int(self.now * 1000))

    def ident(self, kind):
        self.n += 1
        return f"{kind}-{self.n:04d}"

    def defaults(self, size=40, *, skipped=2, settle=True):
        guids = _members(self.n * 7, size)
        r = self.root(BotDo(self.ident("defaults"), guids, "nc", "-bg,-lfg"), "realm_day", 4)
        if not settle:
            return r
        self.store.observe_ack(Ack(0, r.command_id, True, ""), now=self.tick())
        skips = tuple(JobSkip(g, "busy") for g in guids[:skipped])
        ok = tuple(g for g in guids if g not in {s.guid for s in skips})
        self.store.observe_bot_job(Job(0, r.command_id, "bot_do", "done", "", "", ok, skips,
                                       0, 0, 0, 0, 0, 0), now=self.tick())
        return r

    def rearm(self, size):
        guids = _members(self.n * 11, size)
        r = self.root(p.Rearm(self.ident("rearm"), guids), "realm_day", 4)
        self.store.observe_ack(Ack(0, r.command_id, True, ""), now=self.tick())
        self.store.observe_realm_job(p.RealmJob(0, r.command_id, "rearm", "done", "", 0, 0,
                                                skipped=guids[:3]), now=self.tick())
        return r

    def leave_refused(self, size, named=5):
        guids = _members(self.n * 13, size)
        r = self.root(p.LeavePvp(self.ident("leave"), guids), "raid", 0)
        error = "wg_private_state_unavailable:" + ",".join(str(g) for g in guids[:named])
        self.store.observe_ack(Ack(0, r.command_id, False, error), now=self.tick())
        return r

    def grow(self, size):
        guids = _members(self.n * 17, size)
        r = self.root(p.GuildGrow(self.ident("grow"), 77, guids), "realm_day", 4)
        self.store.observe_ack(Ack(0, r.command_id, True, ""), now=self.tick())
        self.store.observe_realm_job(p.RealmJob(0, r.command_id, "guild_grow", "done", "", 0, 3), now=self.tick())
        return r

    def released(self, size):
        """An unknown leave the operator released (`realm release`): durable, holds nobody."""
        guids = _members(self.n * 19, size)
        r = self.root(p.LeavePvp(self.ident("released"), guids), "raid", 0)
        self.store.observe_send_error(r.command_id, "Timeout", now=self.tick())
        return self.store.abandon(r.command_id, reason="reviewed", now=self.tick())

    def unknown(self, size=40):
        r = self.defaults(size, settle=False)
        self.store.observe_send_error(r.command_id, "Timeout", now=self.tick())
        return r


def _copy(store, record, count):
    """Insert count copies of a stored row under new command ids; nothing else changes."""
    conn = store.conn
    cursor = conn.execute("SELECT * FROM realm_action WHERE command_id=?", (record.command_id,))
    names = [c[0] for c in cursor.description if c[0] != "seq"]
    row = dict(zip([c[0] for c in cursor.description], cursor.fetchone()))
    old = f'"id":"{record.command_id}"'
    assert row["payload"].count(old) == 1
    with conn:
        for k in range(count):
            ident = f"{record.command_id}-c{k:03d}"
            values = {**row, "command_id": ident, "payload": row["payload"].replace(old, f'"id":"{ident}"')}
            conn.execute("INSERT INTO realm_action(" + ",".join(names) + ") VALUES("
                         + ",".join("?" for _ in names) + ")", tuple(values[n] for n in names))


def realistic_journal(conn=None):
    """Fill a journal like the live one; returns (store, the guids its roots hold)."""
    store = ActionStore(conn if conn is not None else journal_conn())
    b = _Builder(store)
    _copy(store, b.defaults(skipped=0), 37)
    _copy(store, b.defaults(), 111)
    _copy(store, b.rearm(145), 44)
    _copy(store, b.rearm(40), 44)
    _copy(store, b.leave_refused(145), 4)
    _copy(store, b.leave_refused(30), 7)
    _copy(store, b.grow(30), 3)
    _copy(store, b.released(145), 5)
    _copy(store, b.released(12), 4)
    held = set()
    for _ in range(2):
        held |= set(b.unknown().reserved_manifest)
    return store, frozenset(held)
