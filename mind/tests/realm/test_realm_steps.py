import asyncio
import hashlib
import json
import logging
import sqlite3
from types import SimpleNamespace

import pytest

from conftest import run
from leeroy_mind.realm import cli, protocol
from leeroy_mind.realm.actions import ActionContext
from leeroy_mind.realm.ports import RealmDisabled
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.realm.steps import Steps, approve, digest, write_plan
from realm.fakes import RealmApp


class FakeLedger:
    """F1's DirectLedger interface only: one row per (owner, ref), then its state."""

    def __init__(self):
        self.rows, self.prepared = {}, []

    def prepare(self, cmd, *, owner, ref, context, expects_job):
        if (owner, ref) in self.rows or not context.enabled:
            return None
        self.rows[(owner, ref)] = "prepared"
        self.prepared.append((owner, ref, cmd))
        return cmd.id

    def state(self, owner, ref):
        return self.rows.get((owner, ref))

    def settle(self, cmd):
        for owner, ref, sent in self.prepared:
            if sent.id == cmd.id:
                self.rows[(owner, ref)] = "acked"


class FakeRealm:
    def __init__(self, root, store, clock, ledger):
        self.app = SimpleNamespace(data_dir=root, clock=clock)
        self.ports = SimpleNamespace(conn=store.conn, send=self.send, now=clock.now)
        self.enabled, self.ledger, self.sent, self.parts = True, ledger, [], []
        self.on_sent = None

    def part(self, name):
        return self.ledger if name == "direct" else None

    def action_context(self):
        return ActionContext(self.enabled, True, True, 1, "test", 1, 21, 1)

    def add_part(self, part, *, event_types=()):
        self.parts.append((part, tuple(event_types)))

    async def send(self, cmd):
        if not self.enabled:
            raise RealmDisabled("realm disabled")
        self.sent.append(cmd)
        self.ledger.settle(cmd)
        if self.on_sent is not None:
            self.on_sent(cmd)


@pytest.fixture
def realm(tmp_path, store, clock):
    return FakeRealm(tmp_path, store, clock, FakeLedger())


def renamer(calls):
    async def apply(plan, progress):
        calls.append(plan)
        states = [await progress.send(i, protocol.GuildRename(f"rename-{i}", 21, name))
                  for i, name in enumerate(plan["names"])]
        return {"states": states}
    return apply


def approved(root, clock, payload, name="rivals"):
    path = write_plan(root, name, payload, local=clock.local())
    approve(root, path, now=clock.now())
    return path, json.loads(path.read_text(encoding="utf-8"))["sha256"]


def tick_after(steps, clock, seconds=60):
    run(clock.advance(seconds, step=seconds))
    run(steps.tick())


def rows(store):
    return [tuple(row) for row in store.conn.execute(
        "SELECT name,sha256,approved,applied,result FROM realm_step ORDER BY name,sha256")]


def test_plan_hash_canonical(tmp_path, clock):
    path = write_plan(tmp_path, "rivals", {"b": [1, 2], "a": {"y": 1, "x": "é"}},
                      local=clock.local())
    assert path.parent == tmp_path / "realm" / "plans"
    assert path.name == "rivals-20261029-2000.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["step"] == "rivals" and doc["payload"] == {"a": {"x": "é", "y": 1}, "b": [1, 2]}
    body = {key: value for key, value in doc.items() if key != "sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert doc["sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest() == digest(doc)
    # Neither key order nor layout is the hash; the content is.
    reordered = json.loads(path.read_text(encoding="utf-8"),
                           object_pairs_hook=lambda pairs: dict(reversed(pairs)))
    assert list(reordered) != list(doc) and digest(reordered) == doc["sha256"]
    assert digest({**doc, "payload": {"a": {"x": "é", "y": 2}, "b": [1, 2]}}) != doc["sha256"]
    # Integer keys and tuples are normalised first, so the file reads back to its own hash;
    # a second plan in the same minute never overwrites the first.
    again = write_plan(tmp_path, "rivals", {10: (1, 2), 2: "x"}, local=clock.local())
    assert again.name == "rivals-20261029-2000-2.json"
    back = json.loads(again.read_text(encoding="utf-8"))
    assert back["payload"] == {"10": [1, 2], "2": "x"} and digest(back) == back["sha256"]
    for name, payload in [("rivals", {"x": float("nan")}), ("../rivals", {}), ("history", {})]:
        with pytest.raises(ValueError):
            write_plan(tmp_path, name, payload, local=clock.local())


def test_approve_writes_file_only(tmp_path, store, clock, capsys, monkeypatch):
    plan = write_plan(tmp_path, "rivals", {"guilds": [7, 8]}, local=clock.local())
    sha = json.loads(plan.read_text(encoding="utf-8"))["sha256"]
    db = tmp_path / "mind.db"
    before = db.stat().st_mtime_ns, db.stat().st_size
    def forbidden(*args, **kwargs):
        raise AssertionError("approve opened a database")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(cli, "_open", forbidden)
    argv = ["approve", str(plan), "--data-dir", str(tmp_path)]
    assert cli.realm_main(argv, clock=clock) == 0
    approval = tmp_path / "realm" / "approved" / plan.name
    assert json.loads(approval.read_text(encoding="utf-8")) == {"sha256": sha, "approved_at": clock.now()}
    assert json.loads(capsys.readouterr().out)["sha256"] == sha
    assert cli.realm_main(argv, clock=clock) == 0  # the same approval again is a no-op
    # A plan edited after it was written, or one outside the plans directory, is not approved.
    edited = write_plan(tmp_path, "rivals", {"guilds": [7]}, local=clock.local())
    doc = json.loads(edited.read_text(encoding="utf-8"))
    edited.write_text(json.dumps({**doc, "payload": {"guilds": [9]}}), encoding="utf-8")
    elsewhere = tmp_path / plan.name
    elsewhere.write_text(plan.read_text(encoding="utf-8"), encoding="utf-8")
    for path in (edited, elsewhere):
        assert cli.realm_main(["approve", str(path), "--data-dir", str(tmp_path)], clock=clock) == 1
        assert str(tmp_path) not in capsys.readouterr().out
    assert sorted(p.name for p in approval.parent.iterdir()) == [plan.name]
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before
    monkeypatch.undo()
    assert rows(store) == []


def test_tick_applies_once(tmp_path, store, clock, realm):
    calls = []
    steps = Steps(realm)
    steps.register("rivals", renamer(calls))
    path = write_plan(tmp_path, "rivals", {"names": ["Alpha", "Beta"]}, local=clock.local())
    run(steps.tick())  # nothing approved yet
    sha = json.loads(path.read_text(encoding="utf-8"))["sha256"]
    approve(tmp_path, path, now=clock.now())
    run(steps.tick())  # inside the minute since the last look: waits
    assert calls == [] and realm.sent == [] and rows(store) == []
    tick_after(steps, clock)
    assert calls == [{"names": ["Alpha", "Beta"]}]
    assert [cmd.name for cmd in realm.sent] == ["Alpha", "Beta"]
    assert [(owner, ref) for owner, ref, _ in realm.ledger.prepared] == [
        ("steps", f"step:rivals:{sha}:0"), ("steps", f"step:rivals:{sha}:1")]
    [(name, row_sha, approved_at, applied, result)] = rows(store)
    assert (name, row_sha, approved_at, applied) == ("rivals", sha, clock.now() - 60, clock.now())
    assert json.loads(result) == {"states": ["acked", "acked"]}
    tick_after(steps, clock)
    restarted = Steps(realm)  # a new mind: the realm_step row, not memory, says it is done
    restarted.register("rivals", renamer(calls))
    run(restarted.tick())
    assert len(calls) == 1 and len(realm.sent) == 2 and len(rows(store)) == 1


def test_edited_plan_refused(tmp_path, store, clock, realm, caplog):
    calls = []
    steps = Steps(realm)
    steps.register("rivals", renamer(calls))
    path, sha = approved(tmp_path, clock, {"names": ["Alpha"]})
    original = path.read_text(encoding="utf-8")
    doc = json.loads(original)
    forged = {**doc, "payload": {"names": ["Mallory"]}}
    forged["sha256"] = digest(forged)  # even with its own hash rewritten to match
    path.write_text(json.dumps(forged), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="leeroy_mind.realm.steps"):
        run(steps.tick())
        tick_after(steps, clock)
    assert calls == [] and realm.sent == [] and realm.ledger.prepared == []
    [(name, row_sha, _, applied, result)] = rows(store)
    assert (name, row_sha, applied) == ("rivals", sha, None)
    assert json.loads(result) == {"refused": "realm_step_hash_mismatch"}
    assert sum("realm_step_hash_mismatch" in r.getMessage() for r in caplog.records) == 1
    path.write_text(original, encoding="utf-8")  # the refusal is final
    tick_after(steps, clock)
    assert calls == [] and rows(store)[0][3] is None


def test_apply_resumes_from_progress(tmp_path, store, clock, realm):
    calls = []
    steps = Steps(realm)
    steps.register("rivals", renamer(calls))
    _, sha = approved(tmp_path, clock, {"names": ["Alpha", "Beta", "Gamma", "Delta"]})
    def stop_after_alpha(cmd):
        if cmd.name == "Alpha":
            realm.enabled = False
    realm.on_sent = stop_after_alpha
    run(steps.tick())
    assert [cmd.name for cmd in realm.sent] == ["Alpha"]
    assert rows(store)[0][3] is None and json.loads(rows(store)[0][4]) == {}
    # Delta was prepared once before (its outcome unknown): never sent again.
    realm.ledger.rows[("steps", f"step:rivals:{sha}:3")] = "unknown"
    realm.enabled, realm.on_sent = True, None
    restarted = Steps(realm)
    restarted.register("rivals", renamer(calls))
    run(restarted.tick())
    assert [cmd.name for cmd in realm.sent] == ["Alpha", "Beta", "Gamma"]
    refs = [ref for _, ref, _ in realm.ledger.prepared]
    assert refs == [f"step:rivals:{sha}:{i}" for i in range(3)]
    assert len(calls) == 2 and rows(store)[0][3] == clock.now()
    assert json.loads(rows(store)[0][4]) == {"states": ["acked", "acked", "acked", "unknown"]}


def test_disabled_never_applies(tmp_path, store, clock, realm, monkeypatch):
    calls = []
    realm.enabled = False
    steps = Steps(realm)
    steps.register("rivals", renamer(calls))
    approved(tmp_path, clock, {"names": ["Alpha"]})
    for _ in range(3):
        tick_after(steps, clock)
    assert calls == [] and realm.sent == [] and rows(store) == []
    # The real Realm, attached disabled: the part registers with no I/O and never applies.
    (tmp_path / "real").mkdir()
    app = RealmApp(tmp_path / "real", store, clock)
    def forbidden(*args, **kwargs):
        raise AssertionError("attach started asynchronous work")
    monkeypatch.setattr(asyncio, "create_task", forbidden)
    real = attach_realm(app)  # The parts registry attaches steps.
    monkeypatch.undo()
    part = real.part("steps")
    assert type(part) is Steps
    assert [p for p, _ in real._parts] == [part] and not (app.data_dir / "realm").exists()
    part.register("rivals", renamer(calls))
    approved(app.data_dir, clock, {"names": ["Alpha"]})
    for _ in range(3):
        run(clock.advance(60, step=60))
        run(real.tick())
        run(part.tick())
    assert real.enabled is False and calls == [] and app.sent == [] and rows(store) == []
