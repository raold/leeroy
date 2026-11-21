"""The consent and receipt journal works across message delivery and process restart."""

from dataclasses import replace

import pytest

from leeroy_mind.godmode import heroics
from leeroy_mind.protocol import Member

NOW = 1780000000


def roster():
    return {
        g: Member(g, f"Member{g}", 1, cls, 0, 80, 3, True, 0, 1, spec)
        for g, cls, spec in ((11, 1, 2), (12, 5, 0), (13, 8, 1), (14, 4, 1), (15, 9, 1), (16, 3, 1), (17, 5, 0))
    }


def propose(store):
    cast = roster()
    return heroics.propose_group(store.conn, cast, {g: heroics.Candidate(True) for g in cast}, now=NOW)


def test_agreed_conversation_starts_the_job(store):
    group = propose(store)
    assert group["members"] == [11, 12, 13, 14, 15]
    assert group["map"] == 602
    start = heroics.offer(store, group, day="2026-05-28", now=NOW, guild=3, human=1)
    assert not heroics.offer(store, group, day="2026-05-28", now=NOW, guild=3, human=1)
    assert heroics.open_chat(store.conn, start, "conversation", NOW)
    for member in [11, 11, 12, 13, 14]:
        mid = store.conn.execute(
            "INSERT INTO discord_message(channel,author,origin,text,ts) "
            "VALUES('lfg-and-alts',?,'mind','synthetic',?)",
            (member, NOW),
        ).lastrowid
        heroics.record_post(store.conn, start, "conversation", member, mid, NOW)
    assert heroics.consent(store.conn, start)
    store.conn.execute("UPDATE discord_message SET deleted=? WHERE id=?", (NOW, mid))
    assert not heroics.consent(store.conn, start)


def test_his_login_preempts_the_heroic(store):
    group = propose(store)
    start = heroics.offer(store, group, day="d", now=NOW, guild=3, human=1)
    store.conn.execute("UPDATE member_heroic SET phase='running',sent=? WHERE id=?", (NOW, start))
    assert heroics.overlapping(store.conn, [18], 3, 1) == ""
    assert heroics.overlapping(store.conn, [12, 16], 3, 1) == start
    assert heroics.overlapping(store.conn, [12], 3, 2) == ""


def test_restart_reattaches_or_tears_down(store):
    start = heroics.offer(store, propose(store), day="d", now=NOW, guild=3, human=1)
    store.conn.execute("UPDATE member_heroic SET phase='sent',sent=? WHERE id=?", (NOW, start))
    assert heroics.holds(store.conn)[0].guids == frozenset(range(11, 16))
    store.conn.execute(
        "UPDATE member_heroic SET phase='terminal',terminal='cleanup_failed' WHERE id=?", (start,)
    )
    assert heroics.holds(store.conn)[0].retained
    store.conn.execute("UPDATE member_heroic SET returned=1 WHERE id=?", (start,))
    assert heroics.holds(store.conn) == ()


def test_results_become_memories_and_seeds(store):
    start = heroics.offer(store, propose(store), day="d", now=NOW, guild=3, human=1)
    assert list(heroics.seeds(store.conn, NOW, 3, 1))[0].kind == "lfg"
    store.conn.execute(
        "UPDATE member_heroic SET phase='terminal',terminal='done',kills=4,wipes=1,result_at=? WHERE id=?",
        (NOW, start),
    )
    (result,) = heroics.seeds(store.conn, NOW, 3, 1)
    assert result.kind == "heroic_result" and "4" in result.fact and "1 wipe" in result.fact


def test_dungeons_off_means_no_proposal(store):
    from leeroy_mind.config import LifeCfg

    assert LifeCfg().dungeons is False
    assert list(heroics.seeds(store.conn, NOW, 3, 1)) == []


def test_roles_free_and_directional_ties(store):
    cast = roster()
    states = {g: heroics.Candidate(True) for g in cast}
    store.conn.execute("INSERT INTO relationship(a,b,affinity) VALUES(16,11,80)")
    result = heroics.propose_group(store.conn, cast, states, now=NOW)
    assert 16 in result["members"]
    store.conn.execute("INSERT INTO feud(a,b,started,cause) VALUES(11,16,1,'test')")
    assert 16 not in heroics.propose_group(store.conn, cast, states, now=NOW)["members"]
    assert heroics.propose_group(store.conn, cast, states | {11: heroics.Candidate(False)}, now=NOW) is None
    cast[11] = replace(cast[11], level=79)
    assert heroics.propose_group(store.conn, cast, states, now=NOW) is None


class Rig:
    """The production guild lanes, ledger, day handoff and receipt reducer over an in-memory native."""

    def __init__(self, tmp_path, store, clock):
        from types import SimpleNamespace
        from conftest import make_persona
        from realm.fakes import RealmApp
        from leeroy_mind.guild.service import install
        from leeroy_mind.memory.writer import MemoryWriter
        from leeroy_mind.realm.service import attach_realm

        (tmp_path / "mind.toml").write_text("[life]\ndungeons=true\n")
        app = self.app = RealmApp(tmp_path, store, clock)
        self.store, self.clock = store, clock
        people = roster()
        store.upsert_cast(list(people.values()), clock.now())
        app.personas = {g: make_persona(g, m.name) for g, m in people.items()}
        app.world.online = set(people)
        app.world.group = None
        app.world.members = people
        app.raid = SimpleNamespace(phase=lambda now: "idle")
        app.engine.register_source = lambda source: None
        app.arbiter = None
        app.writer = MemoryWriter(store, None, clock)
        app.guild = install(app, armory=SimpleNamespace(available=False))
        self.realm = app.realm = attach_realm(app)
        self.h = app.guild.heroics
        self.replies = lambda cmd: (True, "")

    async def connect(self):
        from leeroy_mind.protocol import Ack, Hello

        await self.app.connection(True)
        await self.app.event(Hello(int(self.clock.now() * 1000), "test", "1", 3, 0, (heroics.FEATURE,), 123))

        async def send(cmd, *, preserve_id=False, expected_epoch=None):
            assert preserve_id and expected_epoch == self.app.transport_state.epoch
            self.app.sent.append(cmd)
            result = self.replies(cmd)
            if isinstance(result, Exception):
                raise result
            return Ack(0, cmd.id, *result)

        self.app.send = send

    async def agree(self):
        await self.h.tick()
        r = self.store.conn.execute("SELECT * FROM member_heroic").fetchone()
        assert r and r["phase"] == "offered"
        self.ident = r["id"]
        assert heroics.open_chat(self.store.conn, self.ident, "talk", self.clock.now())
        for g in (11, 12, 13, 14):
            self.post(g)
        from types import SimpleNamespace

        c = SimpleNamespace(id="talk", reason="agreed", seed=SimpleNamespace(key="lfg:" + self.ident))
        self.h.finish_chat(c)
        assert self.h.row(self.ident)["phase"] == "agreed"

    def post(self, g):
        now = self.clock.now()
        mid = self.store.conn.execute(
            "INSERT INTO discord_message(channel,author,origin,text,ts) "
            "VALUES('lfg-and-alts',?,'mind','synthetic',?)",
            (g, now),
        ).lastrowid
        heroics.record_post(self.store.conn, self.ident, "talk", g, mid, now)

    def event(self, state, **fields):
        from guild.fakes import job

        return job(
            "member_dungeon",
            state,
            id=self.ident,
            ts=int(self.clock.now() * 1000),
            guids=tuple(range(11, 16)),
            **fields,
        )


def test_final_check_and_start_once_then_positive_receipts(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.protocol import Boss
    from leeroy_mind.guild.protocol import StartMemberDungeon

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.agree()
        await rig.h.tick()
        starts = [c for c in rig.app.sent if isinstance(c, StartMemberDungeon)]
        assert len(starts) == 3 and starts[0].dungeon_inspect
        assert starts[1].day_check.request_id == starts[2].id == rig.ident
        assert starts[2].day_prepared == starts[1].id
        assert rig.h.row(rig.ident)["phase"] == "running"
        assert not rig.app.guild.members_in_play()
        await rig.app.event(
            rig.event(
                "progress",
                stage="running",
                instance_id=47,
                members_in_instance=5,
                last_progress_ts=int(clock.now() * 1000),
                boss_engaged=False,
            )
        )
        assert rig.app.guild.members_in_play() == frozenset(range(11, 16))
        await rig.app.event(Boss(int(clock.now() * 1000), 602, 47, 2, "Loken", "wiped", (11, 12, 13, 14, 15)))
        terminal = rig.event(
            "done", instance_id=47, kills=4, wipes=1, reason="cleared", map_id=602, revision=24, returned=True
        )
        await rig.app.event(terminal)
        await rig.app.event(terminal)
        assert not rig.app.guild.members_in_play() and not heroics.holds(store.conn)
        assert rig.h.row(rig.ident)["returned"]
        facts = list(heroics.seeds(store.conn, clock.now(), 3, 1))
        assert "Loken" in facts[0].fact
        assert (
            store.conn.execute(
                "SELECT count(*) FROM memory WHERE key=? AND text LIKE '%Loken%'", ("heroic:" + rig.ident,)
            ).fetchone()[0]
            == 5
        )

    asyncio.run(go())


def test_driver_waits_one_minute_eight_times_without_cast_reservation(tmp_path, store, clock):
    import asyncio

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.agree()
        rig.replies = lambda c: (False, "driver_pending" if c.dungeon_driver_ticket else "day_driver_required")
        for attempt in range(8):
            await rig.h.tick()
            assert rig.h.row(rig.ident)["tries"] == attempt + 1
            assert not heroics.holds(store.conn)
            assert not rig.realm.bodies.claims_for(heroics.OWNER)
            await rig.h.tick()
            assert len(rig.app.sent) == (attempt + 1) * 2
            await clock.advance(60, 60)
        assert rig.h.row(rig.ident)["phase"] == "talk_only"
        assert {c.day_check.request_id for c in rig.app.sent} == {rig.ident}
        assert len({c.id for c in rig.app.sent}) == 16

    asyncio.run(go())


def test_login_and_validated_overlap_wait_for_exact_return(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.protocol import Login
    from leeroy_mind.guild.protocol import StringDo, MemberDungeonStatus

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.agree()
        await rig.h.tick()
        original = StringDo("b" * 32, 12, "come_to_me", target_guid=1)
        await rig.app.event(Login(int(clock.now() * 1000), 1, True))
        assert rig.h.row(rig.ident)["phase"] == "running" and not rig.h.lease
        assert not any(isinstance(c, MemberDungeonStatus) for c in rig.app.sent)
        calls = []

        async def send(cmd, **guard):
            calls.append((cmd, guard))
            return True

        task = asyncio.create_task(
            rig.realm.parts["day"].handoff(original, (12,), "string", lambda: True, send)
        )
        while not any(isinstance(c, MemberDungeonStatus) for c in rig.app.sent):
            await asyncio.sleep(0)
        check = next(c for c in rig.app.sent if isinstance(c, StringDo))
        assert check.day_check.heroic_start == rig.ident and check.day_check.owner_guid == 1
        assert not calls
        terminal = rig.event(
            "failed", map_id=602, reason="cleanup_failed:preempted", revision=8, returned=False
        )
        await rig.app.event(terminal)
        assert not calls and heroics.holds(store.conn)
        await rig.app.event(replace(terminal, returned=True, revision=9))
        assert await task
        assert calls == [(replace(original, day_prepared=check.id), {"expected_epoch": 1})]
        assert rig.h.row(rig.ident)["reason"] == "cleanup_failed:preempted"

    asyncio.run(go())


def test_uncertain_start_restart_queries_only_original(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.bridge import ConnectionLost
    from leeroy_mind.guild.protocol import MemberDungeonStatus, StartMemberDungeon

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.agree()
        rig.replies = lambda cmd: ConnectionLost("lost") if cmd.day_prepared else (True, "")
        await rig.h.tick()
        assert rig.h.row(rig.ident)["phase"] == "sent" and heroics.holds(store.conn)
        other = Rig(tmp_path, store, clock)
        await other.connect()
        await other.h.tick()
        assert [type(c) for c in other.app.sent] == [MemberDungeonStatus]
        assert other.app.sent[0].start_id == rig.ident
        assert not any(isinstance(c, StartMemberDungeon) for c in other.app.sent)
        other.replies = lambda cmd: (False, "unknown_dungeon")
        await other.h.status(rig.ident)
        assert not heroics.holds(store.conn)

    asyncio.run(go())


def test_substitute_must_post_and_drops_do_not_count(tmp_path, store, clock):
    import asyncio, json
    from types import SimpleNamespace
    from leeroy_mind.server.seeds import Seed

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.h.tick()
        r = store.conn.execute("SELECT * FROM member_heroic").fetchone()
        rig.ident = r["id"]
        party = json.loads(r["roster"])
        c = SimpleNamespace(
            id="talk",
            cast=party.copy(),
            seed=Seed("lfg:" + r["id"], "lfg", (heroics.CHANNEL,), "Go", tuple(party)),
        )
        assert heroics.open_chat(store.conn, rig.ident, c.id, clock.now())
        for g in party[:3]:
            rig.post(g)
        assert rig.h.dropped(c, 15)
        for g in party[:4]:
            rig.post(g)
        assert not heroics.consent(store.conn, rig.ident)
        rig.post(16)
        assert heroics.consent(store.conn, rig.ident) and 15 not in c.seed.about
        assert not rig.h.dropped(c, 16)
        c.reason = "guard_drop"
        rig.h.finish_chat(c)
        assert rig.h.row(rig.ident)["phase"] == "talk_only"

    asyncio.run(go())


def test_dungeons_reloads_nightly_and_session_both_directions(tmp_path):
    import asyncio
    from selfies.fakes import mind_app

    app = mind_app(tmp_path, toml="[life]\ndungeons=false\n")

    def toggle(enabled):
        (tmp_path / "mind.toml").write_text(f"[life]\ndungeons={str(enabled).lower()}\n")

    async def go():
        try:
            assert app.guild.heroics is None and "day" not in app.realm.parts
            for reload in (app._reload_config, lambda: app.night_runner.make_ctx("2026-10-29", (1, 10))):
                for on in (True, False):
                    toggle(on)
                    reload()
                    assert app.guild.heroics.enabled is on and app.cfg.life.dungeons is on
                    assert app.file_cfg.life.dungeons is on and app.engine.cfg.life.dungeons is on
                    assert not app.realm.parts["day"].admission
            assert sum(part is app.realm.parts["day"] for part, _ in app.realm._parts) == 1
        finally:
            await app.stop()

    asyncio.run(go())


def test_terminal_before_ack_sees_durable_original_and_wins(tmp_path, store, clock):
    import asyncio
    import sqlite3
    from contextlib import closing

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.agree()

        def response(cmd):
            if cmd.day_prepared:
                path = store.conn.execute("PRAGMA database_list").fetchone()[2]
                with closing(sqlite3.connect(path)) as other:
                    assert other.execute(
                        "SELECT phase FROM member_heroic WHERE id=?", (cmd.id,)
                    ).fetchone() == ("sent",)
                rig.h.job(rig.event("failed", map_id=602, returned=True, revision=3, reason="setup:grouping"))
            return True, ""

        rig.replies = response
        await rig.h.tick()
        assert rig.h.row(rig.ident)["phase"] == "terminal" and not rig.h.lease and not heroics.holds(store.conn)

    asyncio.run(go())
