"""The day block must follow exact dispatch and completion evidence."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from leeroy_mind.godmode import dayrun
from leeroy_mind.guild.protocol import BotDo, ClearLockout, StringDo
from leeroy_mind.realm.action_policy import classify
from leeroy_mind.realm.protocol import RealmMove, RpgStatus, Duel
from leeroy_mind.testing.wire import bridge_send

PERMIT = "a" * 32


def test_execution_uses_only_realm_moves_and_skips_refusals():
    move = RealmMove("first", 101, "north_pond", day_permit=PERMIT)
    assert classify(move).reserved_manifest == (101,)
    fishing = BotDo("cast", (101,), "do", "go fishing", day_permit=PERMIT)
    assert classify(fishing).policy == "ordinary"
    with pytest.raises(ValueError):
        classify(replace(fishing, param="other"))
    with pytest.raises(ValueError):
        classify(replace(fishing, day_permit=""))
    assert dayrun.disposition(SimpleNamespace(state="completed", result_ok=False,
        result_reason="refused")) == "skipped"
    assert dayrun.disposition(SimpleNamespace(state="unknown", result_ok=False,
        result_reason="job_failed")) == "pending"


def test_plan_never_moves_into_his_group_or_instance():
    with pytest.raises(ValueError):
        classify(RealmMove("move", 101, "near_human", day_permit=PERMIT))
    with pytest.raises(ValueError):
        classify(RealmMove("move", 101, "ironforge", social=True, day_permit=PERMIT))
    with pytest.raises(ValueError):
        classify(RpgStatus("rpg", 101, "travel flight", day_permit=PERMIT))
    assert classify(Duel("duel", 101, 102, day_permit=PERMIT)).reserved_manifest == (101,102)


@pytest.mark.parametrize("hostility", ["rival", "feud"])
def test_hostile_day_pair_never_reaches_native_admission(tmp_path, store, clock, hostility):
    import asyncio
    async def go():
        rig = Rehearsal(tmp_path,store,clock)
        await rig.connect()
        rig.plan(activity="duel_practice", place="org_duel", members=(101,102))
        if hostility == "rival":
            store.conn.execute("INSERT OR REPLACE INTO relationship(a,b,affinity) VALUES(102,101,-80)")
        else:
            store.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES(101,102,1,'synthetic')")
        await rig.tick()
        assert not any(isinstance(command,dayrun.DayBegin) for command in rig.app.sent)
        assert store.conn.execute("SELECT reason FROM day_plan").fetchone()[0] == "changed_tie"
    asyncio.run(go())


def test_planned_block_has_no_activity_claim(store):
    from leeroy_mind.awareness.card import card, CardContext
    store.conn.execute("INSERT INTO day_plan(night,member,starts,ends,activity,place,status) "
                       "VALUES('2026-11-20',101,10,20,'fishing','north_pond','planned')")
    for channel in ("guild", "discord:general"):
        context = CardContext(channel=channel, now=15, day_plans=True)
        assert "north pond" not in card(store.conn,101,context=context).render().lower()
    assert dayrun.disposition(SimpleNamespace(state="dispatched", result_ok=None,
        result_reason=None)) == "pending"


class Rehearsal:
    """Real part registration, BodyLedger, queue dispatch, journal, cards and MemoryWriter."""

    def __init__(self, root, store, clock, *, enabled=True, realm_on=True):
        import asyncio
        from conftest import seed_test_cast
        from leeroy_mind.memory.writer import MemoryWriter
        from leeroy_mind.realm.service import attach_realm
        from leeroy_mind.guild.protocol import register
        from realm.fakes import RealmApp

        register()  # guild life's install registers its types (bot_do, job) before the Realm attaches
        (root / "mind.toml").write_text(f"[realm]\nenabled={str(realm_on).lower()}\n[life]\nday_plans={str(enabled).lower()}\n")
        self.people = seed_test_cast(store)
        self.app = app = RealmApp(root, store, clock)
        self.store, self.clock = store, clock
        app.world.group = None
        app.guild = None
        app.raid = SimpleNamespace(phase=lambda now: "idle")
        app.writer = MemoryWriter(store, None, clock)
        self.realm = app.realm = attach_realm(app)
        self.day = self.realm.parts["day"]
        self.hello_boot = 1770000000123
        self.released = []


    async def connect(self):
        from leeroy_mind.protocol import Hello
        await self.app.connection(True)
        await self.app.event(Hello(0, "test", "1", 21, 1, (dayrun.FEATURE,), self.hello_boot))

    async def tick(self):
        import asyncio
        await self.day.tick()
        self.realm.queue.tick()
        for _ in range(16):
            await asyncio.sleep(0)

    def plan(self, activity="fishing", place="north_pond", members=(101,)):
        import json
        from leeroy_mind.godmode.dayplan import slots_for
        night = "2026-10-28"
        start, end = slots_for(self.store.conn, night)["16"]
        assert start <= self.clock.now() < end
        for g in members:
            self.store.conn.execute("INSERT INTO day_plan(night,member,starts,ends,activity,place,partners,status) "
                "VALUES(?,?,?,?,?,?,?,'planned')", (night, g, start, end, activity, place,
                json.dumps([other for other in members if other != g])))

    def receipt(self, state="open", reason="", **changes):
        from leeroy_mind.realm.protocol import DayState
        p = self.day.permit(1)
        values = dict(ts=int(self.clock.now() * 1000), origin_id=p["begin_id"], permit=PERMIT, serial=7,
                      guild=21, human=1, members=tuple(r["member"] for r in self.day.rows(1)),
                      state=state, reason=reason, released=tuple(self.released),
                      started_ms=self.hello_boot, succeeded=tuple(self.released))
        return DayState(**(values | changes))

    async def begin(self, **kwargs):
        await self.connect()
        self.plan(**kwargs)
        await self.tick()
        assert [c for c in self.app.sent if isinstance(c,dayrun.DayBegin)]
        await self.app.event(self.receipt())
        await self.tick()
        return next(c for c in self.app.sent if isinstance(c,RealmMove))


@pytest.mark.parametrize("complete", [True, False])
def test_day_cohort_checks_one_batch_and_requires_every_member(tmp_path, store, clock, monkeypatch, complete):
    import asyncio
    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        await rig.connect()
        rig.plan(members=(101, 102))
        batches = []
        original = rig.realm.bodies.holders_of
        def batch(members):
            batches.append(tuple(members))
            return original(members) if complete else {101: None}
        monkeypatch.setattr(rig.realm.bodies, "holders_of", batch)
        monkeypatch.setattr(rig.realm.bodies, "holder", lambda _: pytest.fail("per-member body refresh"))
        row = store.conn.execute("SELECT * FROM day_plan ORDER BY id LIMIT 1").fetchone()
        rig.day.start(row, clock.now(), 0)
        assert batches == [(101, 102)]
        permit = rig.day.permit(1)
        if complete:
            assert permit["state"] == "opening"
        else:
            assert permit is None
            assert [tuple(r) for r in store.conn.execute("SELECT status,reason FROM day_plan")] == [
                ("preempted", "held"), ("preempted", "held")]
        await rig.realm.close()
    asyncio.run(go())


def test_plan_block_reaches_card_and_memory(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.awareness.card import CardContext, card
    from leeroy_mind.guild.protocol import Job
    from leeroy_mind.realm.protocol import RealmJob
    from leeroy_mind.server.seeds import gather
    from leeroy_mind.talk.guild_topics import gather as game_topics

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin()
        assert rig.realm.action_store.get(move.id).transport_state == "settled"
        ctx = dict(now=clock.now(), names={101: "Example"}, day_plans=True)
        def text(channel):
            return card(store.conn, 101, context=CardContext(channel=channel, **ctx)).render(4000)
        assert "north pond" not in text("guild")
        await rig.app.event(RealmJob(0, move.id, "realm_move", "done", "", 101, 0))
        await rig.tick()
        assert not any(isinstance(c, BotDo) for c in rig.app.sent)
        rig.released.append(move.id)
        await rig.app.event(rig.receipt())
        await rig.tick()
        fishing = next(c for c in rig.app.sent if isinstance(c, BotDo))
        assert "Arrived at north pond" in text("guild")
        assert "Went fishing" not in text("discord:general")
        job = Job(0, fishing.id, "bot_do", "done", "", "", (101,), (), 0, 0, 0, 0, 0, 0)
        await rig.app.event(job)
        rig.released.append(fishing.id)
        await rig.app.event(rig.receipt())
        await rig.tick()
        await rig.app.event(job)
        await rig.tick()
        for channel in ("guild", "discord:general"):
            assert "Went fishing at north pond" in text(channel)
        [(kind, memory, scope)] = store.conn.execute("SELECT kind,text,scope FROM memory WHERE owner=101").fetchall()
        assert (kind, memory, scope) == ("event", "Went fishing at north pond.", "open")
        args = dict(now=clock.now(), local=clock.local(), cast=rig.people, human=1)
        seeds = gather(store.conn, **args, officers=frozenset(), human_name="", trial_rank=4, day_plans=True)
        day = next(seed for seed in seeds if seed.kind == "day")
        assert day.openers == (101,) and "went fishing" in day.fact
        assert any(t.owner == 101 and "went fishing" in t.topic for t in game_topics(store.conn, **args, day_plans=True))
        assert not any(t.key.startswith("day:") for t in game_topics(store.conn, **args))
        from leeroy_mind.godmode.dayrun import seed_valid
        assert seed_valid(store.conn, day.key, clock.now())
        rig.day.close(1, "preempted", "string")
        assert not seed_valid(store.conn, day.key, clock.now())
        assert "north pond" not in card(store.conn, 101, context=CardContext(channel="guild", now=clock.now())).render()
        await rig.realm.close()

    asyncio.run(go())


@pytest.mark.parametrize("higher", ["raid", "offline_raid", "string", "heroic"])
def test_his_pull_preempts_the_block(tmp_path, store, clock, higher):
    import asyncio
    from unittest.mock import AsyncMock
    from leeroy_mind.realm.actions import ActionRejected

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin(activity="inn", place="org_inn", members=(101, 102))
        send = AsyncMock(return_value="accepted")
        request = asyncio.create_task(rig.day.handoff(ClearLockout("original", 533, (102,)), [102], higher, lambda: True, send))
        await asyncio.sleep(0)
        assert {r["status"] for r in rig.day.rows(1)} == {"preempted"}
        assert rig.realm.action_store.holds()
        for invalid in ({"serial": 8}, {"members": (101,)}, {"started_ms": 1}, {"permit": "b"*32},
                        {"guild": 1}, {"human": 2}):
            await rig.app.event(rig.receipt("closed", higher, **invalid))
            send.assert_not_awaited()
            assert rig.day.permit(1)["state"] == "revoking" and rig.realm.action_store.holds()
        await rig.tick()
        check = next(c for c in rig.app.sent if isinstance(c, ClearLockout) and c.day_check)
        assert check.day_check.origins == (rig.day.permit(1)["begin_id"],)
        await rig.app.event(rig.receipt("closed", higher, released=(move.id,), succeeded=()))
        assert await asyncio.wait_for(request, 1) == "accepted"
        assert rig.realm.action_store.holds() == ()
        assert rig.realm.action_store.get(move.id).result_ok is False
        await rig.app.event(rig.receipt("closed", higher, released=(move.id,), succeeded=()))
        with pytest.raises(ActionRejected, match="duplicate"):
            await rig.day.handoff(ClearLockout("original", 533, (102,)), [102], higher, lambda: True, send)
        send.assert_awaited_once()
        assert store.conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 0
        await rig.realm.close()

    asyncio.run(go())


@pytest.mark.parametrize("accepted", [True, False])
def test_real_string_handoff_validates_before_revoking_then_uses_original_job(tmp_path, store, clock, accepted, caplog):
    import asyncio
    from leeroy_mind.godmode import intents
    from leeroy_mind.godmode.service import Godmode
    from leeroy_mind.guild.protocol import Job, StringDo
    from leeroy_mind.protocol import Ack

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin()
        app = rig.app
        app.connected = True
        app.personas = rig.people
        app.store.set_meta("human_guid", "1")
        service = Godmode(app, None)
        def intent():
            return intents.create(store, member=101, kind="come_to_me", args={}, human=None, now=clock.now())
        app.world.human_online = False
        assert (await service.pull(intent()))["reason"] == "human_offline"
        assert rig.day.permit(1)["state"] == "open"
        app.world.human_online = True
        original_bridge_send = app.send
        for native_error, code in (("off", "strings_off"), ("in_instance", "in_instance"),
                                   ("unknown type: day_check_string_do", "unsupported"),
                                   ("bad field: guid", "invalid"), ("body details in diagnostic", "refused")):
            async def refuse(cmd, **kwargs):
                if isinstance(cmd, StringDo) and cmd.day_check:
                    return Ack(0, cmd.id, False, native_error)
                return await original_bridge_send(cmd, **kwargs)
            app.send = refuse
            ident = intent()
            assert (await service.pull(ident))["reason"] == intents.get(store.conn, ident)["reason"] == code
            assert "body details in diagnostic" not in caplog.text
            assert rig.day.permit(1)["state"] == "open"
        @bridge_send
        async def send(cmd, *, preserve_id, expected_epoch=None):
            assert expected_epoch in (None, rig.day.epoch)
            assert preserve_id
            app.sent.append(cmd)
            if isinstance(cmd, StringDo) and cmd.day_check is None:
                if not accepted:
                    return Ack(0, cmd.id, False, "in_instance")
                service.on_job(Job(0, cmd.id, "string_do", "done", "came", "", (101,), (), 0, 0, 0, 101, 0, 0))
            return Ack(0, cmd.id, True, "")
        app.send = send
        task = asyncio.create_task(service.pull(intent()))
        await asyncio.sleep(0)
        assert rig.day.permit(1)["state"] == "revoking"
        assert not any(isinstance(c, StringDo) and c.day_check is None for c in app.sent)
        await app.event(rig.receipt("closed", "string", released=(move.id,), succeeded=()))
        result = await asyncio.wait_for(task, 1)
        assert (result["state"], result["reason"]) == (("taken", "came") if accepted else ("not_taken", "in_instance"))
        [original] = [c for c in app.sent if isinstance(c, StringDo) and c.day_check is None]
        [check] = [c for c in app.sent if isinstance(c, StringDo) and c.day_check]
        assert original.day_prepared == check.id and check.day_check.request_id == original.id
        assert store.conn.execute("SELECT request_id FROM day_handoff WHERE state='sent'").fetchone()[0] == original.id
        await rig.realm.close()

    asyncio.run(go())


@pytest.mark.parametrize("block", ["his_group", "combat", "native_instance"])
def test_real_fences_skip_the_block_without_retry_or_claim(tmp_path, store, clock, block):
    import asyncio
    from leeroy_mind.protocol import Ack, Group

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        await rig.connect()
        rig.plan()
        if block == "his_group":
            rig.app.world.group = Group(0, 1, False, (1, 101))
        elif block == "combat":
            rig.app.raid.phase = lambda now: "combat"
        else:
            original = rig.app.send
            async def send(cmd, **kwargs):
                ack = await original(cmd, **kwargs)
                return Ack(0, cmd.id, False, "busy:instance") if isinstance(cmd, RealmMove) else ack
            rig.app.send = send
        await rig.tick()
        if block == "native_instance":
            await rig.app.event(rig.receipt())
        for _ in range(4):
            await rig.tick()
        moves = [c for c in rig.app.sent if isinstance(c, RealmMove)]
        assert len(moves) == int(block == "native_instance")
        assert rig.day.rows(1)[0]["status"] in {"skipped", "preempted"}
        assert not store.conn.execute("SELECT 1 FROM memory").fetchone()
        await rig.realm.close()

    asyncio.run(go())


def test_skipped_fishing_preserves_the_arrival_without_an_invented_start(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.guild.protocol import Job, JobSkip
    from leeroy_mind.realm.protocol import RealmJob
    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin()
        rig.released.append(move.id)
        await rig.app.event(rig.receipt())
        await rig.app.event(RealmJob(0, move.id, "realm_move", "done", "", 101, 0))
        await rig.tick()
        fishing = next(c for c in rig.app.sent if isinstance(c, BotDo))
        await rig.app.event(Job(0, fishing.id, "bot_do", "done", "", "", (), (JobSkip(101, "no_bobber"),),
                                0, 0, 0, 0, 0, 0))
        await rig.app.event(rig.receipt(released=(move.id, fishing.id)))
        await rig.tick()
        assert rig.day.rows(1)[0]["status"] == "skipped"
        assert [r[0] for r in store.conn.execute("SELECT text FROM memory")] == ["Arrived at north pond."]
        await rig.realm.close()
    asyncio.run(go())


def test_new_connection_is_cleanup_only_and_new_boot_keeps_unknown_held(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.protocol import Hello
    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin()
        await rig.app.connection(False)
        await rig.app.connection(True)
        await rig.app.event(Hello(1, "test", "1", 21, 1, (), rig.hello_boot + 1))
        await rig.tick()
        assert rig.day.permit(1)["state"] == "unresolved"
        assert rig.realm.action_store.get(move.id).state not in {"completed", "not_sent"}
        await rig.app.event(rig.receipt("closed", "context_changed", released=(move.id,), succeeded=()))
        await rig.tick()
        assert rig.realm.action_store.holds()
        assert sum(isinstance(c, RealmMove) for c in rig.app.sent) == 1
        assert not store.conn.execute("SELECT 1 FROM memory").fetchone()
        await rig.realm.close()
    asyncio.run(go())


@pytest.mark.parametrize("realm_on", [True, False])
def test_same_boot_mind_restart_reconciles_original_steps_without_replaying_the_waiter(tmp_path, store, clock, realm_on):
    import asyncio
    from unittest.mock import AsyncMock
    async def go():
        old = Rehearsal(tmp_path, store, clock)
        move = await old.begin()
        send = AsyncMock()
        request = asyncio.create_task(old.day.handoff(ClearLockout("old-request", 533, (101,)), [101], "raid", lambda: True, send))
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await old.realm.close()
        new = Rehearsal(tmp_path, store, clock, enabled=False, realm_on=realm_on)
        assert not new.day.admission
        await new.connect()
        assert not new.realm.bodies.claims_for("day:1")
        assert new.realm.action_store.holds()
        resumed = asyncio.create_task(new.day.handoff(ClearLockout("new-request", 533, (101,)), [101], "raid", lambda: True, send))
        await asyncio.sleep(0)
        await new.app.event(new.receipt("closed", "raid", released=(move.id,), succeeded=()))
        await asyncio.wait_for(resumed, 1)
        send.assert_awaited_once()
        assert not new.realm.action_store.holds()
        assert all(not isinstance(c, (RealmMove, RpgStatus, Duel, BotDo)) for c in new.app.sent)
        assert dict(store.conn.execute("SELECT request_id,state FROM day_handoff")) == {
            "old-request": "expired", "new-request": "sent"}
        await new.realm.close()
    asyncio.run(go())


def test_seal_while_writer_is_inflight_waits_for_actual_settlement(tmp_path, store, clock):
    import asyncio
    from unittest.mock import AsyncMock
    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        gate = asyncio.Event()
        original = rig.app.send
        async def delayed(cmd, **kw):
            result = await original(cmd, **kw)
            if isinstance(cmd, RealmMove):
                await gate.wait()
            return result
        rig.app.send = delayed
        move = await rig.begin()
        assert rig.realm.action_store.get(move.id).transport_state == "inflight"
        send = AsyncMock()
        request = asyncio.create_task(rig.day.handoff(StringDo("pull", 101, "come_to_me"), [101], "string", lambda: True, send))
        await asyncio.sleep(0)
        await rig.app.event(rig.receipt("closed", "string", released=(move.id,), succeeded=()))
        assert rig.realm.action_store.holds()
        send.assert_not_awaited()
        gate.set()
        await rig.tick()
        await rig.tick()
        await asyncio.wait_for(request, 1)
        send.assert_awaited_once()
        assert not rig.realm.action_store.holds()
        await rig.realm.close()
    asyncio.run(go())


def test_twenty_away_includes_opening_and_unresolved_permits(tmp_path, store, clock):
    import asyncio
    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        await rig.connect()
        for guid in range(101, 122):
            rig.plan(members=(guid,))
        await rig.tick()
        assert store.conn.execute("SELECT count(*) FROM day_permit").fetchone()[0] == 20
        assert len([cmd for cmd in rig.app.sent if isinstance(cmd, dayrun.DayBegin)]) == 20
        assert store.conn.execute("SELECT reason FROM day_plan WHERE member=121").fetchone()[0] == "away_cap"
        await rig.realm.close()
    asyncio.run(go())


@pytest.mark.parametrize("interrupted_intent", [False, True])
def test_new_boot_without_a_physical_step_allows_the_original_owner_request(tmp_path, store, clock, interrupted_intent):
    import asyncio
    from unittest.mock import AsyncMock
    from leeroy_mind.protocol import Hello
    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        await rig.connect()
        rig.plan()
        await rig.tick()
        await rig.app.event(rig.receipt())
        assert not store.conn.execute("SELECT 1 FROM day_step").fetchone()
        if interrupted_intent:
            # Crash after journaling the intended ID, before ActionQueue's actual writer barrier.
            store.conn.execute("INSERT INTO day_step(id,plan,kind,member) VALUES(?,1,'move',101)", ("b"*32,))
        await rig.app.connection(False)
        await rig.app.connection(True)
        await rig.app.event(Hello(1, "test", "1", 21, 1, (dayrun.FEATURE,), rig.hello_boot + 1))
        await rig.tick()
        send = AsyncMock(return_value=True)
        cmd = ClearLockout("new-request", 533, (101,))
        assert await rig.day.handoff(cmd, [101], "raid", lambda: True, send)
        send.assert_awaited_once_with(cmd)
        assert not store.conn.execute("SELECT 1 FROM memory").fetchone()
        await rig.realm.close()
    asyncio.run(go())


def test_late_native_release_expires_before_owner_send(tmp_path, store, clock):
    import asyncio
    from unittest.mock import AsyncMock, patch
    from leeroy_mind.realm.actions import ActionRejected
    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin()
        send = AsyncMock()
        loop = asyncio.get_running_loop()
        original_time = loop.time
        cmd = ClearLockout("pull", 533, (101,))
        task = asyncio.create_task(rig.day.handoff(cmd, [101], "raid", lambda: True, send))
        await asyncio.sleep(0)
        assert rig.day.permit(1)["state"] == "revoking"
        with patch.object(loop, "time", lambda: original_time() + 31):
            await rig.app.event(rig.receipt("closed", "raid", released=(move.id,), succeeded=()))
            with pytest.raises(ActionRejected, match="expired"):
                await task
            send.assert_not_awaited()
        await rig.realm.close()
    asyncio.run(go())


def test_only_participant_can_choose_the_evidenced_day_topic(store):
    import random
    from conftest import seed_test_cast
    from leeroy_mind.talk.guild_topics import GuildTopic, choose, game_topics
    people = seed_test_cast(store)
    topic = GuildTopic("game", "day:1", "Arrived.", 101)
    used = {t.key for person in people.values() for t in game_topics(person)}
    assert choose([topic], {101: 1}, people, random.Random(3), used) == (101, topic)
    assert choose([topic], {102: 1}, people, random.Random(3), used) is None


def test_one_arrival_cannot_testify_about_a_partner_who_left(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.realm.protocol import RealmJob
    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        first = await rig.begin(activity="inn", place="org_inn", members=(101,102))
        async def prove(move):
            rig.released.append(move.id)
            await rig.app.event(RealmJob(0, move.id, "realm_move", "done", "", move.guid, 0))
            await rig.app.event(rig.receipt())
            await rig.tick()
        await prove(first)
        second = next(c for c in rig.app.sent if isinstance(c, RealmMove) and c.guid == 102)
        assert [tuple(r) for r in store.conn.execute("SELECT owner,text FROM memory")] == [(101, "Arrived at org inn.")]
        await prove(second)
        await clock.advance(2)
        await rig.tick()
        status = next(c for c in rig.app.sent if isinstance(c, RpgStatus))
        await rig.app.event(RealmJob(0,status.id,"rpg_status","failed","day_place_changed",101,0))
        await rig.tick()
        assert [tuple(r) for r in store.conn.execute("SELECT owner,text FROM memory ORDER BY owner")] == [
            (101, "Arrived at org inn."), (102, "Arrived at org inn.")]
        await rig.realm.close()
    asyncio.run(go())


def test_explicit_operator_release_reconciles_a_dispatched_unknown_on_the_new_boot(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.protocol import Hello
    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin()
        journal = rig.realm.action_store
        journal.observe_send_error(move.id, "ConnectionLost", now=clock.now())
        assert journal.get(move.id).state == "unknown"
        await rig.app.connection(False)
        await rig.app.connection(True)
        await rig.app.event(Hello(1, "test", "1", 21, 1, (), rig.hello_boot + 1))
        assert rig.day.permit(1)["state"] == "unresolved"
        journal.abandon(move.id, reason="verified old body is gone", now=clock.now())
        await rig.tick()
        assert rig.day.permit(1)["state"] == "closed"
        assert not journal.holds() and not store.conn.execute("SELECT 1 FROM memory").fetchone()
        await rig.realm.close()
    asyncio.run(go())


@pytest.mark.parametrize("begin_outcome", ["open", "ack_lost", "refused", "deadline", "reconnect"])
def test_opening_owner_waits_for_native_origin_before_any_check_or_original(tmp_path, store, clock, begin_outcome):
    import asyncio
    from leeroy_mind.bridge import AckTimeout
    from leeroy_mind.protocol import Ack
    from leeroy_mind.realm.actions import ActionRejected

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        await rig.connect()
        rig.plan()
        entered, answer = asyncio.Event(), asyncio.Event()
        sent = []
        async def bridge(cmd, **guard):
            sent.append(cmd)
            if isinstance(cmd, dayrun.DayBegin):
                entered.set()
                await answer.wait()
                if begin_outcome == "refused":
                    return Ack(0, cmd.id, False, "day_scope")
                if begin_outcome == "ack_lost":
                    raise AckTimeout("begin")
            return Ack(0, cmd.id, True, "")
        rig.app.send = bridge
        await rig.day.tick()
        cmd = StringDo("original", 101, "come_to_me")
        # Enter handoff before the spawned DayBegin has executed its writer callback.
        async def events():
            await entered.wait()
            assert len(sent) == 1
            assert not store.conn.execute("SELECT 1 FROM day_step").fetchone()
            answer.set()
            if begin_outcome == "deadline":
                await clock.advance(31)
                rig.day.changed()
            elif begin_outcome == "reconnect":
                await rig.app.connection(False)
                await rig.connect()
                rig.day.changed()
            elif begin_outcome != "refused":
                for _ in range(4):
                    await asyncio.sleep(0)
                assert not any(isinstance(c, StringDo) for c in sent)
                await rig.app.event(rig.receipt())
                await rig.day.tick()
                assert not store.conn.execute("SELECT 1 FROM day_step").fetchone()
                while not any(isinstance(c, StringDo) for c in sent):
                    await asyncio.sleep(0)
                await rig.app.event(rig.receipt("closed", "string"))
        responses = asyncio.create_task(events())
        try:
            async with asyncio.timeout(2):
                if begin_outcome in {"deadline", "reconnect"}:
                    with pytest.raises(ActionRejected, match="expired"):
                        await rig.day.handoff(cmd, (101,), "string", lambda: True, bridge)
                else:
                    assert (await rig.day.handoff(cmd, (101,), "string", lambda: True, bridge)).ok
                await responses
        finally:
            answer.set()
            responses.cancel()
            await asyncio.gather(responses, return_exceptions=True)
            await rig.realm.close()
        owner_writes = [c for c in sent if isinstance(c, StringDo)]
        if begin_outcome in {"open", "ack_lost"}:
            first, last = owner_writes
            assert first.day_check.request_id == last.id == cmd.id and last.day_prepared == first.id
        elif begin_outcome == "refused":
            assert owner_writes == [cmd]
        else:
            assert not owner_writes
        assert not any(isinstance(c, (RealmMove, RpgStatus, Duel, BotDo)) for c in sent)
    asyncio.run(go())


@pytest.mark.parametrize("lost", ["check", "original"])
def test_string_only_enters_late_mailbox_for_an_original_write(tmp_path, store, clock, lost):
    import asyncio
    from leeroy_mind.bridge import AckTimeout
    from leeroy_mind.godmode import intents
    from leeroy_mind.godmode.service import Godmode
    from leeroy_mind.protocol import Ack

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin()
        app = rig.app
        app.connected = app.world.human_online = True
        app.personas = rig.people
        app.store.set_meta("human_guid", "1")
        service = Godmode(app, None)
        ident = intents.create(store, member=101, kind="come_to_me", args={}, human=None, now=clock.now())
        writes = []
        previous = app.send
        async def send(cmd, **guard):
            if not isinstance(cmd, StringDo):
                return await previous(cmd, **guard)
            writes.append(cmd)
            if not cmd.day_check or lost == "check":
                raise AckTimeout("no acknowledgement")
            await app.event(rig.receipt("closed", "string", released=(move.id,), succeeded=()))
            return Ack(0, cmd.id, True, "")
        app.send = send
        reply = await service.pull(ident)
        if lost == "check":
            assert (reply["state"], reply["reason"]) == ("not_taken", "day_check_unknown")
            assert len(writes) == 1 and not service._inflight
            assert rig.day.permit(1)["state"] == "verifying" and rig.realm.action_store.holds()
            await rig.tick()
            assert any(isinstance(c, dayrun.DayStatus) for c in app.sent)
            await app.event(rig.receipt("closed", "string", released=(move.id,), succeeded=()))
            assert not rig.realm.action_store.holds() and len(writes) == 1
        else:
            assert (reply["state"], reply["reason"]) == ("pending", "unknown")
            assert len(writes) == 2 and set(service._inflight) == {writes[-1].id}
        await rig.realm.close()
    asyncio.run(go())


def test_queued_day_step_pauses_at_writer_during_owner_check_and_survives_a_refusal(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.protocol import Ack
    from leeroy_mind.realm.actions import ActionRejected

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        await rig.connect()
        rig.plan()
        await rig.tick()
        await rig.app.event(rig.receipt())
        await rig.day.tick()  # Enroll the move; select it for dispatch before the owner arrives.
        rig.realm.queue.tick()
        ident, = store.conn.execute("SELECT id FROM day_step").fetchone()
        entered = asyncio.Event()
        release = asyncio.Event()
        previous = rig.app.send
        async def send(cmd, **kw):
            if isinstance(cmd, StringDo):
                entered.set()
                await release.wait()
                return Ack(0, cmd.id, False, "off")
            return await previous(cmd, **kw)
        rig.app.send = send
        async def confirm():
            await entered.wait()
            for _ in range(8):
                await rig.tick()
            assert not any(isinstance(c, RealmMove) for c in rig.app.sent)
            assert rig.realm.action_store.get(ident).state == "prepared"
            release.set()
        guard = asyncio.create_task(confirm())
        try:
            async with asyncio.timeout(2):
                with pytest.raises(ActionRejected, match="off"):
                    await rig.day.handoff(StringDo("original", 101, "come_to_me"), [101], "string", lambda: True, send)
                await guard
            await rig.tick()
            assert [c.id for c in rig.app.sent if isinstance(c, RealmMove)] == [ident]
        finally:
            release.set()
            guard.cancel()
            await asyncio.gather(guard, return_exceptions=True)
            await rig.realm.close()
    asyncio.run(go())


@pytest.mark.parametrize("reason", ["ended", "context_changed", "reboot"])
@pytest.mark.parametrize("phase", [None, "arrival", "activity"])
def test_verifying_terminal_settles_plan_listener_and_seed_but_preserves_proved_memory(tmp_path, store, clock,
                                                                                      reason, phase):
    import asyncio
    from leeroy_mind.bridge import AckTimeout
    from leeroy_mind.guild.protocol import Job
    from leeroy_mind.realm.actions import ActionRejected
    from leeroy_mind.realm.protocol import RealmJob

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin()
        released, succeeded = [move.id], []
        if phase:
            await rig.app.event(RealmJob(0, move.id, "realm_move", "done", "", 101, 0))
            succeeded.append(move.id)
            await rig.app.event(rig.receipt(released=tuple(released), succeeded=tuple(succeeded)))
            await rig.tick()
            fish = next(cmd for cmd in rig.app.sent if isinstance(cmd, BotDo))
            released.append(fish.id)
            if phase == "activity":
                await rig.app.event(Job(0, fish.id, "bot_do", "done", "", "", (101,), (), 0, 0, 0, 0, 0, 0))
                succeeded.append(fish.id)
                await rig.app.event(rig.receipt(released=tuple(released), succeeded=tuple(succeeded)))
                await rig.tick()
        def memory():
            return [row[0] for row in store.conn.execute("SELECT text FROM memory WHERE owner=101")]
        facts = memory()
        assert bool(facts) == bool(phase)
        async def lose(check, **guard):
            assert check.day_check
            raise AckTimeout("check")
        rig.app.send = lose
        with pytest.raises(ActionRejected, match="day_check_unknown"):
            await rig.day.handoff(StringDo("owner", 101, "come_to_me"), (101,), "string", lambda: True, lose)
        terminal_reason = "context_changed" if reason == "reboot" else reason
        await rig.app.event(rig.receipt("closed", terminal_reason, released=("b" * 32,), succeeded=()))
        assert rig.day.permit(1)["state"] == "verifying" and 1 in rig.day.listeners
        if phase != "activity":
            assert rig.realm.action_store.holds()
        if reason == "reboot":
            await rig.app.connection(False)
            rig.hello_boot += 1
            await rig.connect()
        else:
            await rig.app.event(rig.receipt("closed", reason, released=tuple(released), succeeded=tuple(succeeded)))
        row = rig.day.rows(1)[0]
        eligible = bool(phase and reason == "ended")
        assert (row["status"], row["reason"]) == ("done" if eligible else "skipped", terminal_reason)
        unresolved = reason == "reboot" and phase != "activity"
        assert rig.day.permit(1)["state"] == ("unresolved" if unresolved else "closed")
        assert bool(rig.realm.action_store.holds()) == unresolved and 1 not in rig.day.listeners
        assert dayrun.seed_valid(store.conn, "day:1", clock.now()) == eligible
        assert bool(list(dayrun.seeds(store.conn, clock.now(), {101: "Example"}))) == eligible
        assert dayrun.card_lines(store.conn, 101, clock.now(), {101: "Example"}) == facts
        assert memory() == facts
        await rig.realm.close()
    asyncio.run(go())


@pytest.mark.parametrize("end", ["local", "native"])
def test_expiry_settles_each_participant_from_her_own_evidence(tmp_path, store, clock, end):
    import asyncio
    from leeroy_mind.realm.protocol import RealmJob

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        first = await rig.begin(activity="inn", place="org_inn", members=(101,102))
        rig.released.append(first.id)
        await rig.app.event(RealmJob(0, first.id, "realm_move", "done", "", 101, 0))
        await rig.app.event(rig.receipt())
        await rig.tick()
        moves = [c.id for c in rig.app.sent if isinstance(c, RealmMove)]
        assert len(moves) == 2 and moves[0] == first.id
        await clock.advance(rig.day.rows(1)[0]["ends"] - clock.now(), step=86400)
        if end == "local":
            await rig.tick()
        else:
            await rig.app.event(rig.receipt("revoking", "ended"))
        assert [(r["member"], r["status"], r["phase"]) for r in rig.day.rows(1)] == [
            (101, "done", "arrival"), (102, "skipped", None)]
        assert [tuple(r) for r in store.conn.execute("SELECT owner,text FROM memory")] == [(101, "Arrived at org inn.")]
        assert rig.realm.action_store.holds() and not rig.day.listeners
        await rig.app.event(rig.receipt("closed", "ended", released=tuple(moves)))
        assert rig.day.permit(1)["state"] == "closed" and not rig.realm.action_store.holds()
        await rig.realm.close()
    asyncio.run(go())


@pytest.mark.parametrize("boundary", ["begin", "queued_move", "issued_move"])
def test_disabling_stops_new_writes_and_reconciles_the_entered_command(tmp_path, store, clock, boundary):
    import asyncio

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        await rig.connect()
        rig.plan()
        await rig.day.tick()  # Opening row, before the spawned begin enters BridgeClient.send.
        if boundary != "begin":
            await rig.tick()
            await rig.app.event(rig.receipt())
            await rig.day.tick()
            rig.realm.queue.tick()  # Select the enrolled move.
        if boundary == "issued_move":
            await rig.tick()
        rig.realm.configure_day_plans(False)
        await rig.tick()
        moves = [cmd.id for cmd in rig.app.sent if isinstance(cmd, RealmMove)]
        assert len(moves) == int(boundary == "issued_move")
        if boundary == "begin":
            assert not any(isinstance(cmd, dayrun.DayBegin) for cmd in rig.app.sent)
        else:
            ident, = store.conn.execute("SELECT id FROM day_step").fetchone()
            if not moves:
                assert rig.realm.action_store.get(ident).state == "not_sent"
            else:
                assert rig.realm.action_store.holds()
            await rig.app.event(rig.receipt("closed", "context_changed", released=tuple(moves), succeeded=()))
        assert rig.day.permit(1)["state"] == "closed" and not rig.realm.action_store.holds()
        assert all(r["status"] == "skipped" for r in rig.day.rows(1))
        await rig.realm.close()
    asyncio.run(go())


def test_enabling_after_authenticated_greeting_uses_the_current_connection(tmp_path, store, clock):
    import asyncio
    from conftest import seed_test_cast
    from leeroy_mind.protocol import Hello
    from leeroy_mind.realm.service import attach_realm
    from realm.fakes import RealmApp

    async def go():
        (tmp_path / "mind.toml").write_text("[realm]\nenabled=true\n")
        seed_test_cast(store)
        app = RealmApp(tmp_path, store, clock)
        app.world.group = app.guild = None
        app.raid = SimpleNamespace(phase=lambda now: "idle")
        realm = app.realm = attach_realm(app)
        await app.connection(True)
        await app.event(Hello(0, "test", "1", 21, 1, (dayrun.FEATURE,), 1234))
        assert "day" not in realm.parts
        realm.configure_day_plans(True)
        day = realm.parts["day"]
        assert day.context_valid() and (day.boot, day.epoch) == (1234, app.transport_state.epoch)
        start, end = dayrun.slots_for(store.conn, "2026-10-28")["16"]
        store.conn.execute("INSERT INTO day_plan(night,member,starts,ends,activity,place,status) "
                           "VALUES('2026-10-28',101,?,?,'inn','org_inn','planned')", (start,end))
        await day.tick()
        for _ in range(4):
            await asyncio.sleep(0)
        assert len([c for c in app.sent if isinstance(c, dayrun.DayBegin)]) == 1
        await app.connection(False)
        await app.connection(True)
        assert not day.context_valid()  # No greeting for the replacement writer.
        await realm.close()
    asyncio.run(go())
