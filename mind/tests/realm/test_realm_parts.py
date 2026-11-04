"""The Realm's parts registry, owned spawns, the direct part's routing, native's actor set and
the night hook, over the real service and Store with the small RealmApp surface."""
import asyncio
import logging
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace
import uuid

import pytest

from conftest import run, seed_test_cast
from leeroy_mind.nightly.runner import NightRunner, Stage
from leeroy_mind.protocol import Ack, Hello
from leeroy_mind.realm import parts, protocol, store as realm_store
from leeroy_mind.realm.actions import ActionContext
from leeroy_mind.realm.direct import DirectLedger
from leeroy_mind.realm.ports import RealmDisabled
from leeroy_mind.realm.service import attach_realm
from realm.fakes import RealmApp, enabled


@pytest.fixture
def app(tmp_path, store, clock):
    return RealmApp(tmp_path, store, clock)


def module(name, attach=None, **attrs):
    mod = ModuleType(f"leeroy_mind.realm.{name}")
    if attach is not None:
        mod.attach = attach
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def install(monkeypatch, **modules):
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, f"leeroy_mind.realm.{name}", mod)


async def connect(app, guild=21):
    await app.connection(True)
    await app.event(Hello(0, "test", "1", guild, app.human_guid))


async def settle(realm):
    for _ in range(20):
        children = tuple(realm._children) + tuple(realm.queue._tasks)
        if not children:
            return
        await asyncio.gather(*children, return_exceptions=True)


def rows(store):
    """Two rival guilds, their active actors, a retired one and three it must never send."""
    seed_test_cast(store)
    with store.transaction():
        for guild, role in ((50, "progression"), (60, "gankers"), (70, "drama")):
            store.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added,status) VALUES (?,?,?,?,?,?)",
                               (guild, role, "horde", "Synthetic", 1.0, "retired" if guild == 70 else "active"))
        for guid, guild, role, status in ((201, 50, "officer", "active"), (202, 60, "ganker", "active"),
                                          (203, 70, "officer", "retired"), (204, 50, "applicant", "active"),
                                          (103, 50, "officer", "active"),  # A cast member.
                                          (301, 60, "ganker", "active"),   # He kicked her once.
                                          (1, 50, "officer", "active")):   # Him.
            store.conn.execute("INSERT INTO realm_actor(guid,guild,name,role,added,status) VALUES (?,?,?,?,?,?)",
                               (guid, guild, "Rivalname", role, 1.0, status))
        store.conn.execute("INSERT INTO realm_kick(guid,ts,rank) VALUES (301,1.0,3)")


EXPECTED = ((201, 202, 204), (202,), (50, 60))


def actors_sent(app):
    return [(c.guids, c.gankers, c.guilds) for c in app.sent if type(c) is protocol.RealmActors]


# -- the registry ------------------------------------------------------------------------------

def test_parts_registry_attaches_present_modules(app, monkeypatch, caplog):
    seen = []

    class Part:
        def __init__(self, name):
            self.name = name

        def recover(self, *, process_id):
            return 0, 0

        async def on_event(self, ev):
            seen.append((self.name, ev))

    def attach_as(name, events):
        def attach(realm):
            assert realm.part(name) is None  # Not yet: it is set after attach returns.
            return Part(name), events
        return attach

    install(monkeypatch, direct=module("direct", attach_as("direct", ())),
            speech=module("speech", attach_as("speech", ("channel_chat", "zone"))),
            bank=module("bank", lambda realm: None),          # Declines.
            mail=module("mail", lambda realm: 1 / 0))         # Broken: left out, logged by type.
    def attach_itself(realm):  # A part that adds itself and returns only the part.
        part = Part("canon")
        realm.add_part(part, event_types=("achievement",))
        return part

    install(monkeypatch, canon=module("canon", attach_itself))
    # Absent here, whichever of them has been written since: None in sys.modules is not found.
    install(monkeypatch, **dict.fromkeys(("talk", "rivals", "race", "poach", "calendar")))
    caplog.set_level(logging.INFO, logger="leeroy_mind.realm.parts")
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    assert list(realm.parts) == ["direct", "steps", "speech", "canon"]  # Steps is the real module.
    assert realm.part("speech").name == "speech" and realm.part("bank") is None and realm.part("x") is None
    assert [p.name for p, _ in realm._parts] == ["direct", "steps", "speech", "canon"]  # Each once.
    assert realm.on_event in app.handlers["zone"]  # The part's own event type, registered once.
    assert all(len(handlers) == 1 for handlers in app.handlers.values())
    absent = [r.getMessage() for r in caplog.records if r.getMessage().startswith("realm_part_absent")]
    assert absent == ["realm_part_absent names=talk,rivals,race,poach,calendar"]
    assert "realm_part_failed name=mail error=ZeroDivisionError" in caplog.text
    assert "realm_part_declined name=bank" in caplog.text
    assert parts.PART_MODULES == ("direct", "steps", "speech", "talk", "rivals", "race", "poach",
                                  "canon", "bank", "calendar", "mail")

    async def go():
        await connect(app)
        await app.event(Hello(0, "test", "1", 21, 1))  # Not the part's type.
        assert seen == []
        ev = protocol.ChannelChat(1000, "trade", 101, "Arkon", 1637, "wts", False)
        await app.event(ev)
        assert seen == [("speech", ev)]
        await realm.close()
    run(go())


def test_a_present_module_with_a_broken_import_is_a_failure_not_an_absence(app, monkeypatch, caplog):
    def broken(name, *args, **kwargs):
        if name == "leeroy_mind.realm.speech":
            raise ModuleNotFoundError("No module named 'missing_dependency'", name="missing_dependency")
        return real(name, *args, **kwargs)
    real = parts.importlib.import_module
    monkeypatch.setattr(parts.importlib, "import_module", broken)
    caplog.set_level(logging.INFO, logger="leeroy_mind.realm.parts")
    realm = attach_realm(app)
    assert "speech" not in realm.parts
    assert "realm_part_failed name=speech error=ModuleNotFoundError" in caplog.text
    assert "speech" not in next(r.getMessage() for r in caplog.records
                                if r.getMessage().startswith("realm_part_absent"))


@pytest.mark.parametrize("on", [False, True])
def test_attach_makes_no_io(app, monkeypatch, on):
    attached = []

    def attach(realm):
        attached.append(realm)
        return SimpleNamespace(recover=lambda *, process_id: (0, 0)), ()

    install(monkeypatch, direct=module("direct", attach), night=module("night", stage_realm=lambda ctx: None))
    app.night_runner = NightRunner([Stage("stories", lambda ctx: None, "reflection")], lambda *a: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("attachment did I/O or started work")
    for target, name in ((asyncio, "create_task"), (asyncio, "to_thread"), (threading.Thread, "start"),
                         (subprocess, "run"), (subprocess, "Popen")):
        monkeypatch.setattr(target, name, forbidden)
    from leeroy_mind.library import mysql
    monkeypatch.setattr(mysql, "query", forbidden)
    enabled(app.data_dir, on)
    before = app.store.conn.total_changes
    realm = attach_realm(app)
    assert attached == [realm] and realm.enabled is on
    assert app.sent == [] and realm._children == set()
    assert app.store.conn.total_changes == before
    assert app.store.get_meta("realm:guild_name") is None
    app.engine.speak.assert_not_called()
    app.engine.say_as.assert_not_called()


# -- the night hook ----------------------------------------------------------------------------

async def _stage(ctx):
    return None


@pytest.mark.parametrize("on,night,expected", [
    (True, "stage", ["stories", "realm", "critic"]),
    (True, "attach", ["stories", "realm", "critic"]),
    (False, "stage", ["stories", "critic"]),
    (True, None, ["stories", "critic"]),
])
def test_night_installed_only_when_enabled(app, monkeypatch, caplog, on, night, expected):
    added = []
    if night == "stage":
        install(monkeypatch, night=module("night", stage_realm=_stage))
    elif night == "attach":
        install(monkeypatch, night=module("night", lambda realm: added.append(realm) or _stage))
    runner = NightRunner([Stage("stories", _stage, "reflection"), Stage("critic", _stage, "reflection")],
                         lambda *a: None)
    app.night_runner = runner
    calls = []
    original = runner.add_stage
    monkeypatch.setattr(runner, "add_stage", lambda *a, **k: calls.append((a, k)) or original(*a, **k))
    caplog.set_level(logging.INFO, logger="leeroy_mind.realm.parts")
    enabled(app.data_dir, on)
    realm = attach_realm(app)
    assert [s.name for s in runner.stages] == expected
    if "realm" in expected:
        assert calls == [(("realm", _stage), {"after": "stories", "model": "reflection"})]
        assert runner.stages[1].model == "reflection"
        assert added == ([realm] if night == "attach" else [])
        assert "realm_night_installed" in caplog.text
    else:
        assert calls == []
        reason = "disabled" if not on else "absent"
        assert [r.getMessage() for r in caplog.records if "realm_night" in r.getMessage()] == [
            f"realm_night_unavailable reason={reason}"]
    # A switch turned on later waits for a restart: nothing is added after attach.
    enabled(app.data_dir, True)
    realm.reload()
    assert [s.name for s in runner.stages] == expected


def test_night_unavailable_without_a_runner(app, monkeypatch, caplog):
    install(monkeypatch, night=module("night", stage_realm=_stage))
    caplog.set_level(logging.INFO, logger="leeroy_mind.realm.parts")
    enabled(app.data_dir, True)
    attach_realm(app)
    assert [r.getMessage() for r in caplog.records if "realm_night" in r.getMessage()] == [
        "realm_night_unavailable reason=no_runner"]


# -- spawn ---------------------------------------------------------------------------------------

def test_spawned_compose_does_not_block_tick(app, monkeypatch):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    sweeps, cancelled = [], []

    async def go():
        started = asyncio.Event()

        async def compose():
            started.set()
            try:
                await asyncio.Event().wait()  # A model call that never returns.
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        class Speech:
            def __init__(self):
                self.tasks = []

            async def tick(self):
                self.tasks.append(realm.spawn(compose))

        speech = Speech()
        realm.add_part(speech)
        monkeypatch.setattr(realm.guard, "sweep", lambda: sweeps.append(True) or 0)
        await asyncio.wait_for(realm.tick(), 1)
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(realm.tick(), 1)  # Still composing: the next tick runs anyway.
        assert sweeps == [True, True] and len(speech.tasks) == 2
        assert all(not t.done() for t in speech.tasks) and set(speech.tasks) <= realm._children
        await asyncio.wait_for(realm.close(), 1)  # Cancelled by the disable, drained by close.
        assert cancelled == [True, True] and realm._children == set()
        assert realm.spawn(compose) is None  # Closed: nothing starts.
    run(go())


def test_spawn_is_owned_by_its_generation(app):
    realm = attach_realm(app)

    async def go():
        ran = []

        async def work():
            ran.append(True)
        assert realm.spawn(work) is None  # Disabled: nothing starts.
        enabled(app.data_dir, True)
        realm.reload()
        task = realm.spawn(work)
        realm._invalidate()  # Before it ever ran: it never runs.
        await asyncio.gather(task, return_exceptions=True)
        assert ran == [] and task.cancelled()
        failing = realm.spawn(_boom)
        await asyncio.gather(failing, return_exceptions=True)
        assert realm._children == set()
        with pytest.raises(TypeError):
            realm.spawn(object())
        await realm.close()
    run(go())


async def _boom():
    raise LookupError("synthetic")


# -- the direct part -------------------------------------------------------------------------

class FakeDirect:
    def __init__(self, *owned):
        self.owned, self.calls, self.asked = set(owned), [], []

    def recover(self, *, process_id):
        return 0, 0

    def owns_command(self, cmd):
        self.asked.append(cmd.id)
        return cmd.id in self.owned

    def before_send(self, cmd, *, context):
        self.calls.append(("before_send", cmd.id, context))

    def on_ack(self, cmd, ack):
        self.calls.append(("ack", cmd.id, ack.ok))
        return cmd.id in self.owned

    def on_send_error(self, cmd, error_type):
        self.calls.append(("send_error", cmd.id, error_type))
        return cmd.id in self.owned

    def on_job(self, ev):
        self.calls.append(("job", ev.id, ev.value))
        return ev.id in self.owned


def test_attach_recovers_the_direct_ledger_once(app):
    """An earlier process's prepared intent is abandoned and its dispatched one is unknown
    by the time the Realm is attached, so neither blocks its ref nor is ever resent."""
    earlier = "earlier-process"
    ledger = DirectLedger(app.store, process_id=earlier, clock=app.clock)
    context = ActionContext(True, True, True, 1, earlier, 1, 50, 10)
    prepared = protocol.RealmSay(uuid.uuid4().hex, 201, "trade", "wts frostweave")
    sent = protocol.RealmSay(uuid.uuid4().hex, 201, "trade", "lf2m voa")
    ledger.prepare(prepared, owner="speech", ref="line:1", context=context, expects_job=False)
    ledger.prepare(sent, owner="speech", ref="line:2", context=context, expects_job=False)
    ledger.before_send(sent, context=context)
    realm = attach_realm(app)
    direct = realm.parts["direct"]
    assert direct.state("speech", "line:1") == ("abandoned", "recovered_prepared", None)
    assert direct.state("speech", "line:2") == ("unknown", "recovered_dispatched", None)
    assert direct.recover(process_id=realm._process_id) == (0, 0)  # Nothing left for a second pass.
    run(realm.close())


def test_direct_owned_command_bypasses__DIRECT(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    direct = realm.parts["direct"] = FakeDirect("grant", "lost")

    async def go():
        await connect(app)
        grant = protocol.GrantAchievement("grant", 101, 3117)  # Not in _DIRECT, not queued.
        ack = await realm.ports.send(grant)
        assert ack.ok and app.sent[-1] is grant
        assert direct.calls == [("before_send", "grant", realm.action_context()), ("ack", "grant", True)]
        assert "grant" not in realm._pending and "grant" not in realm._completed
        receipt = realm_store.recent_receipts(app.store.conn)[0]
        assert (receipt.command_id, receipt.kind, receipt.stage, receipt.ok) == ("grant", "grant_achievement", "ack", True)
        # A send error the part owns stays the part's; the in-memory path never sees it.
        lost = protocol.BankMoney("lost", 5)
        realm.on_send(lost)
        realm.on_send_error(lost, "NotConnected")
        assert direct.calls[-1] == ("send_error", "lost", "NotConnected") and "lost" not in realm._pending
        # Not the part's: the in-memory direct path, and a body command still needs the queue.
        money = protocol.BankMoney("money", 5)
        realm.on_send(money)
        assert "money" in realm._pending and direct.calls[-1] == ("send_error", "lost", "NotConnected")
        realm.on_ack(money, Ack(0, "money", True, ""))
        assert "money" in realm._completed and direct.calls[-1] == ("ack", "money", True)
        with pytest.raises(ValueError, match="queue enrollment"):
            realm.on_send(protocol.GrantAchievement("other", 101, 3117))
        # The queue's own command is never offered to the part.
        move = protocol.RealmMove("move", 101, "harbor")
        direct.owned.add("move")
        realm.queue.submit(move, owner="realm_day")
        realm.on_send(move)
        assert "move" not in direct.asked and realm.action_store.get("move").transport_state != "not_entered"
        # Disabled: nothing is offered to the part, but its acks still settle.
        enabled(app.data_dir, False)
        realm.reload()
        later = protocol.GrantAchievement("later", 101, 3117)
        direct.owned.add("later")
        with pytest.raises(RealmDisabled):
            realm.on_send(later)
        assert "later" not in direct.asked
        realm.on_ack(later, Ack(0, "later", False, "realm_off"))
        assert direct.calls[-1] == ("ack", "later", False)
        await realm.close()
    run(go())


def test_realm_job_value_routes_to_direct(app):
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    direct = realm.parts["direct"] = FakeDirect("post")

    async def go():
        await connect(app)
        job = protocol.RealmJob(1000, "post", "calendar_post", "done", "", 101, 777)
        await app.event(job)
        assert direct.calls == [("job", "post", 777)]
        receipt = realm_store.recent_receipts(app.store.conn)[0]
        assert (receipt.command_id, receipt.stage, receipt.value, receipt.ok) == ("post", "job", 777, True)
        # The in-memory direct path keeps its own results.
        count = protocol.ItemCount("count", 101, (5976,))
        realm.on_send(count)
        realm.on_ack(count, Ack(0, "count", True, ""))
        assert realm.on_job(protocol.RealmJob(2000, "count", "item_count", "done", "", 101, 3)) is True
        assert direct.calls[-1] == ("job", "count", 3) and "count" in realm._completed
        # Disabled, a job is still authority for the part, from the dispatcher or at shutdown.
        enabled(app.data_dir, False)
        realm.reload()
        late = protocol.RealmJob(3000, "post", "calendar_post", "failed", "calendar_outcome_unknown", 101, 9)
        assert realm.on_job(late) is True and direct.calls[-1] == ("job", "post", 9)
        assert app.receipts["realm_job"] == [realm.on_job]
        await realm.close()
    run(go())


# -- native's actor set --------------------------------------------------------------------------

def test_realm_actors_sent_once_per_hello(app):
    rows(app.store)
    enabled(app.data_dir, True)
    realm = attach_realm(app)

    async def go():
        await realm.tick()
        await app.event(Hello(0, "test", "1", 21, 1))  # Not connected: no valid hello.
        await realm.tick()
        await settle(realm)
        assert actors_sent(app) == []
        await connect(app)
        for _ in range(3):
            await realm.tick()
            await settle(realm)
        assert actors_sent(app) == [EXPECTED]
        await app.event(Hello(0, "test", "1", 21, 1))  # The same connection again.
        await realm.tick()
        await settle(realm)
        assert actors_sent(app) == [EXPECTED]
        await app.connection(False)
        await connect(app)  # A new connection: native may have restarted, and forgot them.
        await realm.tick()
        await settle(realm)
        assert actors_sent(app) == [EXPECTED, EXPECTED]
        receipts = realm_store.recent_receipts(app.store.conn)
        assert {(r.kind, r.stage, r.ok) for r in receipts} == {("realm_actors", "ack", True)}
        await realm.close()
    run(go())


def test_no_actor_set_while_disabled_or_empty(app):
    seed_test_cast(app.store)
    realm = attach_realm(app)

    async def go():
        await connect(app)
        await realm.tick()
        enabled(app.data_dir, True)
        await connect(app)  # Enabled now, but no rival rows: nothing to tell native.
        await realm.tick()
        await settle(realm)
        assert actors_sent(app) == [] and app.sent == []
        await realm.close()
    run(go())


def test_an_actor_set_that_never_left_is_sent_again(app):
    rows(app.store)
    enabled(app.data_dir, True)
    realm = attach_realm(app)

    async def go():
        await connect(app)
        refused = []

        async def not_connected(cmd, *, preserve_id):
            from leeroy_mind.bridge import NotConnected
            refused.append(cmd)
            raise NotConnected("synthetic")
        app.send = not_connected
        await realm.tick()
        await settle(realm)
        assert len(refused) == 1 and actors_sent(app) == []
        del app.send  # The fake bridge's own send again.
        await realm.tick()
        await settle(realm)
        assert actors_sent(app) == []  # Not before its pause.
        await app.clock.advance(60, step=60)
        await realm.tick()
        await settle(realm)
        assert actors_sent(app) == [EXPECTED]
        await realm.close()
    run(go())


def test_actor_probe_from_rows_every_600s(app):
    rows(app.store)
    enabled(app.data_dir, True)
    realm = attach_realm(app)
    answers = {"ok": False}

    async def bridge(cmd, *, preserve_id):
        app.sent.append(cmd)
        return Ack(0, cmd.id, answers["ok"], "" if answers["ok"] else "realm_off")
    app.send = bridge

    async def queue_tick():
        realm.queue.tick()
        await settle(realm)

    async def go():
        await connect(app)
        realm.queue.submit(protocol.RealmMove("move", 101, "harbor"), owner="realm_day")
        await queue_tick()
        assert realm.queue.off and actors_sent(app) == []  # Native said realm_off.
        app.sent.clear()
        await app.clock.advance(599, step=599)
        await queue_tick()
        assert app.sent == []
        await app.clock.advance(1, step=1)
        await queue_tick()
        assert actors_sent(app) == [EXPECTED] and len(app.sent) == 1
        await queue_tick()
        assert len(app.sent) == 1  # Still off: the next probe waits 600 s more.
        await app.clock.advance(600, step=600)
        answers["ok"] = True
        await queue_tick()
        assert actors_sent(app) == [EXPECTED, EXPECTED] and not realm.queue.off
        await app.clock.advance(600, step=600)
        await queue_tick()
        assert len(app.sent) == 2
        await realm.close()
    run(go())


def test_actor_set_refuses_more_than_native_takes(app, caplog):
    rows(app.store)
    with app.store.transaction():
        for guid in range(400, 416):
            app.store.conn.execute("INSERT INTO realm_actor(guid,guild,name,role,added) VALUES (?,?,?,?,?)",
                                   (guid, 50, "Rivalname", "officer", 1.0))
    realm = attach_realm(app)
    assert realm.actor_set() is None and "realm_actors_invalid actors=19 guilds=2" in caplog.text
    assert "realm_actor_excluded guid=103" in caplog.text and "realm_actor_excluded guid=301" in caplog.text
