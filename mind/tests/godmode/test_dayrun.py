"""The day block must follow exact dispatch and completion evidence."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from leeroy_mind.godmode import dayrun
from leeroy_mind.guild.protocol import BotDo
from leeroy_mind.realm.action_policy import classify
from leeroy_mind.realm.protocol import RealmMove, RpgStatus, Duel

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


def test_his_pull_preempts_the_block(store):
    for guid, partner in ((101,102),(102,101)):
        store.conn.execute("INSERT INTO day_plan(night,member,starts,ends,activity,place,partners,status) "
                           "VALUES('2026-11-20',?,10,20,'duel_practice','duel_site',?,'planned')",
                           (guid, f"[{partner}]"))
    rows = store.conn.execute("SELECT * FROM day_plan ORDER BY id").fetchall()
    dayrun.preempt_rows(store, rows, "his_group")
    dayrun.preempt_rows(store, rows, "his_group")
    assert [tuple(r) for r in store.conn.execute("SELECT status,reason FROM day_plan")] == [
        ("preempted","his_group"), ("preempted","his_group")]
    assert store.conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 0


def test_plan_block_reaches_card_and_memory(store):
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

    def __init__(self, root, store, clock):
        import asyncio
        from conftest import seed_test_cast
        from leeroy_mind.memory.writer import MemoryWriter
        from leeroy_mind.realm.service import attach_realm
        from realm.fakes import RealmApp

        (root / "mind.toml").write_text("[realm]\nenabled=true\n[life]\nday_plans=true\n")
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
        self.gate = asyncio.Event()

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


def test_fishing_visit_then_proved_start_reaches_real_cards_and_one_memory(tmp_path, store, clock):
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
        assert "north pond" not in card(store.conn, 101, context=CardContext(channel="guild", now=clock.now())).render()
        await rig.realm.close()

    asyncio.run(go())


@pytest.mark.parametrize("higher", ["raid", "offline_raid", "string", "heroic"])
def test_original_owner_request_waits_for_its_own_seal_once(tmp_path, store, clock, higher):
    import asyncio
    from unittest.mock import AsyncMock
    from leeroy_mind.realm.actions import ActionRejected

    async def go():
        rig = Rehearsal(tmp_path, store, clock)
        move = await rig.begin(activity="inn", place="org_inn", members=(101, 102))
        send = AsyncMock(return_value="accepted")
        request = asyncio.create_task(rig.day.handoff("original", [102], higher, lambda: True, send))
        await asyncio.sleep(0)
        assert {r["status"] for r in rig.day.rows(1)} == {"preempted"}
        assert rig.realm.action_store.holds()
        for invalid in ({"serial": 8}, {"members": (101,)}, {"started_ms": 1}, {"permit": "b"*32},
                        {"guild": 1}, {"human": 2}):
            await rig.app.event(rig.receipt("closed", higher, **invalid))
            send.assert_not_awaited()
            assert rig.day.permit(1)["state"] == "revoking" and rig.realm.action_store.holds()
        await rig.tick()
        revoke = next(c for c in rig.app.sent if isinstance(c, dayrun.DayRevoke))
        assert revoke.origin_id == rig.day.permit(1)["begin_id"] and revoke.permit == PERMIT
        await rig.app.event(rig.receipt("closed", higher, released=(move.id,), succeeded=()))
        assert await asyncio.wait_for(request, 1) == "accepted"
        assert rig.realm.action_store.holds() == ()
        assert rig.realm.action_store.get(move.id).result_ok is False
        await rig.app.event(rig.receipt("closed", higher, released=(move.id,), succeeded=()))
        with pytest.raises(ActionRejected, match="duplicate"):
            await rig.day.handoff("original", [102], higher, lambda: True, send)
        send.assert_awaited_once()
        assert store.conn.execute("SELECT count(*) FROM memory").fetchone()[0] == 0
        await rig.realm.close()

    asyncio.run(go())
