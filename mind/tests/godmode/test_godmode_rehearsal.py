"""D6 through one MindApp, the guarded Discord pipeline and an isolated loopback world."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import random
from uuid import uuid4

from conftest import TEST_CAST, make_persona, seed_test_cast
from dbtemplate import open_migrated
from selfies.fakes import until
from server.test_server_rehearsal import FakeArbiter, VariedLLM
from tuning.test_edits import CASES, _setup, snapshot
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.discord import store as discord
from leeroy_mind.discord.presence import Presence
from leeroy_mind.godmode import heroics, intents
from leeroy_mind.llm.client import ChatResult
from leeroy_mind.personas.io import save_persona
from leeroy_mind.protocol import Member
from leeroy_mind.server import engine, runtime, state
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_clock import FakeClock
from leeroy_mind.testing.fake_llm import check_chat

START = datetime(2026, 10, 29, 20, tzinfo=timezone.utc)
BOOT = int(START.timestamp() * 1000) - 10000


class Script(VariedLLM):
    async def chat(self, model, messages, **kwargs):
        schema = kwargs.get("format")
        if isinstance(schema, dict) and schema.get("title") == "day_plans":
            check_chat(model, messages, **kwargs)
            answer = {
                "blocks": [
                    {"slot": "16", "activity": "inn", "place": "org_inn"},
                    {"slot": "18", "activity": "offline", "place": "none"},
                ]
            }
            return ChatResult(json.dumps(answer), "", 1, 0.0)
        return await super().chat(model, messages, **kwargs)


class World(FakeBridge):
    def __init__(self, clock, cast):
        self.clock = clock
        self.permits = {}
        self.fight = False
        self.ticket = None
        self.accepted = None
        super().__init__(
            [
                dict(
                    type="hello",
                    server="synthetic",
                    version="1",
                    guild_id=21,
                    human_guid=1,
                    ts=int(clock.now() * 1000),
                    started_ms=BOOT,
                    features=["day_plan_handoff_v1", heroics.FEATURE],
                )
            ],
            cast=cast,
        )

    def _is_known(self, kind):
        return kind in {"member_dungeon_inspect", "day_check_start_member_dungeon"} or super()._is_known(kind)

    async def push(self, row):
        await super().push(dict(row, ts=int(self.clock.now() * 1000)))

    async def job(self, ident, kind, **fields):
        data = dict(
            type="job",
            id=ident,
            kind=kind,
            state="done",
            reason="",
            stage="",
            guids=[],
            skipped=[],
            kills=0,
            wipes=0,
            instance_id=0,
            guid=0,
            level=0,
            avg_ilvl=0,
        )
        await self.push(data | fields)

    async def _command(self, writer, cmd):
        kind = cmd["type"]
        if kind == "string_do":
            if self.fight:
                self.commands.append(cmd)
                await self._write(writer, dict(type="ack", id=cmd["id"], ok=False, error="combat"))
                return
            await self.job(
                cmd["id"],
                kind,
                guids=[cmd["guid"]],
                guid=cmd["guid"],
                reason={
                    "sit": "sat",
                    "follow": "following",
                    "emote": "emoted",
                    "join_group": "joined",
                    "come_to_me": "came",
                    "duel_me": "challenged",
                }[cmd["kind"]],
            )
        elif kind == "day_begin":
            token = uuid4().hex
            self.permits[token] = dict(
                type="day_state",
                origin_id=cmd["id"],
                permit=token,
                serial=1,
                guild=21,
                human=1,
                members=cmd["members"],
                state="open",
                reason="",
                released=[],
                succeeded=[],
                started_ms=BOOT,
            )
            await self.push(self.permits[token])
        elif kind in ("realm_move", "rpg_status") and cmd.get("day_permit"):
            p = self.permits[cmd["day_permit"]]
            await self.push(
                dict(
                    type="realm_job",
                    id=cmd["id"],
                    kind=kind,
                    state="done",
                    reason="",
                    guid=cmd["guid"],
                    value=0,
                )
            )
            p["released"].append(cmd["id"])
            p["succeeded"].append(cmd["id"])
            await self.push(p)
        elif kind in ("rearm", "leave_pvp"):
            await self.push(
                dict(
                    type="realm_job",
                    id=cmd["id"],
                    kind=kind,
                    state="done",
                    reason="",
                    guid=0,
                    value=len(cmd["guids"]),
                )
            )
        elif kind == "bot_do":
            await self.job(cmd["id"], kind, guids=cmd["guids"])
        elif kind == "day_status":
            for p in self.permits.values():
                if p["origin_id"] == cmd["origin_id"]:
                    await self.push(p)
        elif kind == "day_revoke":
            await self.close_permit(cmd["permit"], cmd["reason"])
        elif kind == "member_dungeon_inspect":
            assert not self.accepted and len(cmd["members"]) == 4
        elif kind == "day_check_start_member_dungeon":
            self.ticket = cmd
            for p in self.permits.values():
                if p["origin_id"] in cmd["day_check"]["origins"]:
                    await self.close_permit(p["permit"], "heroic")
        elif kind == "start_member_dungeon":
            assert self.accepted is None and self.ticket["id"] == cmd["day_prepared"]
            assert self.ticket["day_check"]["request_id"] == cmd["id"]
            self.accepted = cmd
        await super()._command(writer, cmd)

    async def add_from_guild(self, token):
        p = self.permits[token]
        await self.close_permit(token, "guild_add")
        await self.push(
            dict(
                type="guild_group",
                reason="built",
                size=5,
                leader_guid=1,
                raid=False,
                map=0,
                instance_id=0,
                difficulty=0,
                members=p["members"],
                added=[dict(guid=g, role="damage") for g in p["members"]],
            )
        )

    async def close_permit(self, token, reason):
        p = self.permits[token]
        p.update(state="closed", reason=reason)
        await self.push(p)


def test_godmode_rehearsal(tmp_path, monkeypatch):
    from leeroy_mind.nightly import wiring

    monkeypatch.setattr(wiring, "GpuArbiter", FakeArbiter)
    for module in (state, engine):
        monkeypatch.setattr(module, "presence_of", lambda *args, **kw: Presence("online", False, 0))
    monkeypatch.setattr(engine, "answer_delay", lambda *args: 0)
    monkeypatch.setattr(engine.rhythm, "length", lambda rng: 2)

    async def go():
        clock = FakeClock(START)
        cast = [
            dict(
                guid=g,
                name=TEST_CAST[g][0] if g in TEST_CAST else "Tarin",
                race=2,
                gender=0,
                level=80,
                rank=3,
                online=True,
                map=0,
                zone=1,
                spec_tab=tab,
                **{"class": cls},
            )
            for g, cls, tab in ((101, 1, 2), (102, 5, 0), (103, 8, 1), (104, 4, 1), (105, 9, 1), (106, 3, 1))
        ]
        world = World(clock, cast)
        port = await world.start()
        conf = tmp_path / "mind.toml"
        conf.write_text(
            f"[bridge]\nport={port}\n[godmode]\nenabled=true\n[realm]\nenabled=true\n"
            "[life]\nday_plans=true\ndungeons=true\n[server]\nenabled=true\n[guards]\nenabled=true\n"
            "[claims]\nenabled=true\n[talk]\nopeners_per_hour=0\nchattiness=4\n"
        )
        store = Store(open_migrated(tmp_path / "mind.db"), clock=clock)
        people = seed_test_cast(store)
        for g in (102, 106):
            p = people[g] if g in people else make_persona(g, "Tarin")
            save_persona(
                store,
                replace(
                    p,
                    identity=replace(p.identity, player_gender="female"),
                    romance=replace(p.romance, stance="drawn", style="bold"),
                ),
                source="seed",
                ts=clock.now(),
                race="Orc",
                class_name="Priest",
            )
        store.upsert_cast([Member(1, "Ownerly", 2, 1, 0, 80, 0, True, 0, 1, 2)], clock.now())
        store.set_meta("human_guid", "1")
        mid = _setup(store)
        store.close()
        model = Script()
        cfg = load_config(conf)
        model.set_models({cfg.models.live, cfg.models.embedding, cfg.models.reflection})
        app = MindApp(cfg, tmp_path, clock=clock, llm=model, rng_seed=19)
        selected = set()
        real_snapshot = runtime.snapshot

        async def observed(*args, **kwargs):
            snap = await real_snapshot(*args, **kwargs)
            snap.seeds = [s for s in snap.seeds if s.kind in selected]
            return snap

        monkeypatch.setattr(runtime, "snapshot", observed)

        async def quiet(*args):
            pass

        monkeypatch.setattr(app.engine, "tick", quiet)
        app.server.engine.rng = random.Random(19)
        app.server.engine.rng.random = lambda: 0.5
        task = asyncio.create_task(app.run())
        conn = app.store.conn

        async def action(g, kind, **args):
            return await app.godmode.handle(dict(action="create", kind=kind, member=g, args=args))

        async def converse(kind):
            selected.clear()
            selected.add(kind)
            app.server.engine.next_start = 0
            app.engine._login_at = float("-inf")
            await app.server.lease.handback()
            await app.server.engine.tick(clock.now())
            for _ in range(800):
                if not app.server.engine.live():
                    return
                await clock.advance(1, 1)
                await asyncio.sleep(0.003)
            raise AssertionError("conversation did not finish")

        try:
            await until(
                lambda: (
                    app.realm.action_context().presence_known
                    and 106 in app.world.online
                    and (tmp_path / "control.json").exists()
                )
            )
            for kind, fields in CASES.items():
                before = snapshot(app.store)
                edit = await app.tuning.handle(dict(action="edit", verb="set", kind=kind, args=fields(mid)))
                assert edit["ok"] and snapshot(app.store) != before, (kind, edit)
                undone = await app.tuning.handle(dict(action="undo", change=edit["change"]))
                assert undone["ok"] and snapshot(app.store) == before, kind
            for kind in intents.GAME_KINDS:
                reply = await action(102, kind, **({"emote": "wave"} if kind == "emote" else {}))
                assert reply.get("state") == "taken", (kind, reply)
            world.fight = True
            reply = await action(102, "come_to_me")
            assert (reply["state"], reply["reason"]) == ("not_taken", "combat")
            world.fight = False

            await world.push(dict(type="logout", guid=1, human=True))
            await until(lambda: not app.world.human_online and app.guild._ready())
            ctx = app.night_runner.make_ctx("2026-10-28", (clock.now() - 86400, clock.now()))
            await next(s for s in app.night_runner.stages if s.name == "day_plans").fn(ctx)
            rows = conn.execute("SELECT member,partners FROM day_plan WHERE activity='inn'").fetchall()
            assert len(rows) == 6 and all(not ({g, *json.loads(others)} >= {101, 103}) for g, others in rows)
            day = app.realm.parts["day"]
            for _ in range(8):
                await day.tick()
                app.realm.queue.tick()
                await asyncio.sleep(0.03)
            assert world.permits and any(c["type"] == "realm_move" for c in world.commands)
            first = next(iter(world.permits.values()))
            await world.push(dict(type="login", guid=1, human=True))
            await until(lambda: app.world.human_online and app.guild._ready())
            await world.add_from_guild(first["permit"])
            await until(
                lambda: bool(
                    conn.execute(
                        "SELECT 1 FROM day_plan WHERE status='preempted' AND reason='guild_add'"
                    ).fetchone()
                )
            )

            await world.push(dict(type="logout", guid=1, human=True))
            await until(lambda: not app.world.human_online and app.guild._ready())
            # His login schedules a second Realm recall after 45 seconds. Finish that independent
            # action before starting the LFG scene so its actual reservation cannot race a guarded turn.
            await clock.advance(46, 1)
            for _ in range(120):
                await clock.advance(1, 1)
                if app.realm.recall._second_at is None and not app.realm.recall._busy and conn.execute(
                    "SELECT 1 FROM realm_action WHERE kind='leave_pvp' AND state='completed'"
                ).fetchone():
                    break
            else:
                raise AssertionError("the owner's Realm recall did not settle")
            await app.guild.heroics.tick()
            bad = await action(
                101,
                "react",
                message=discord.post(
                    app.store,
                    channel="general",
                    author=103,
                    origin="mind",
                    text="Synthetic invitation",
                    now=clock.now(),
                ),
                emoji="👍",
            )
            await app.godmode.tick(clock.now())
            assert intents.get(conn, bad["id"])["reason"] == "feud"
            good = await action(102, "post", channel="irl", about="the garden she planted")
            await converse("intent")
            assert intents.get(conn, good["id"])["status"] == "taken"
            await converse("lfg")
            await app.guild.heroics.tick()
            run = conn.execute("SELECT * FROM member_heroic").fetchone()
            assert world.accepted is not None, (run["phase"], run["reason"])
            group = [world.accepted["leader_guid"], *world.accepted["members"]]
            ident = world.accepted["id"]
            assert len(group) == 5 and len(heroics.speakers(conn, ident)) >= 4
            await world.job(
                ident,
                "member_dungeon",
                state="progress",
                stage="running",
                instance_id=81,
                guids=group,
                members_in_instance=5,
                last_progress_ts=int(clock.now() * 1000),
                boss_engaged=False,
            )
            await until(lambda: app.guild.members_in_play() == frozenset(group))
            await world.push(
                dict(
                    type="boss",
                    map=602,
                    instance_id=81,
                    boss_id=2,
                    boss_name="Loken",
                    state="wiped",
                    members=group,
                )
            )
            await world.job(
                ident,
                "member_dungeon",
                reason="cleared",
                instance_id=81,
                guids=group,
                kills=4,
                wipes=1,
                map_id=602,
                revision=12,
                returned=True,
            )
            await until(lambda: bool(app.guild.heroics.row(ident)["returned"]))
            assert (
                conn.execute(
                    "SELECT count(*) FROM memory WHERE key=? AND text LIKE '%Loken%'", ("heroic:" + ident,)
                ).fetchone()[0]
                == 5
            )
            assert not app.guild.members_in_play()
            assert any(
                seed.kind == "heroic_result" and "Loken" in seed.fact
                for seed in heroics.seeds(conn, clock.now(), 21, 1)
            )
            await clock.advance(7200, 7200)
            await day.tick()
            assert (
                conn.execute(
                    "SELECT count(*) FROM day_plan WHERE activity='offline' AND status='done'"
                ).fetchone()[0]
                == 6
            )
            assert not [c for c in world.commands if c["type"] == "say"]
        finally:
            await app.stop()
            await task
            await world.stop()
        assert not (tmp_path / "control.json").exists()

    asyncio.run(go())
