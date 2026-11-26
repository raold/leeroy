"""The consent and receipt journal works across message delivery and process restart."""

from dataclasses import replace

import pytest

from leeroy_mind.godmode import heroics
from leeroy_mind.protocol import Member
from leeroy_mind.testing.wire import bridge_send
from shipped_off import off

NOW = 1780000000


def roster():
    return {
        g: Member(g, f"Member{g}", 1, cls, 0, 80, 3, True, 0, 1, spec)
        for g, cls, spec in ((11, 1, 2), (12, 5, 0), (13, 8, 1), (14, 4, 1), (15, 9, 1), (16, 3, 1), (17, 5, 0))
    }


def test_heroic_candidates_batch_all_owners_with_only_the_members_own_day_exception(store, clock, monkeypatch):
    from types import SimpleNamespace
    from conftest import seed_test_cast
    from leeroy_mind.bodies import BodyLedger, BodyHold, BodyEvidenceUnavailable
    people = seed_test_cast(store)
    bodies = BodyLedger(clock)
    reads = []
    def snapshot():
        reads.append(1)
        return (BodyHold("own", heroics.OWNER, 3, frozenset({101, 102}), True),
                BodyHold("day", "day:1", 4, frozenset({101, 102}), True),
                BodyHold("other", "training", 2, frozenset({102}), True))
    bodies.add_holder_source("fixture", snapshot)
    store.conn.execute("INSERT INTO day_plan(id,night,member,starts,ends,activity,status,action_ref) "
                       "VALUES(1,'fixture',101,1,2,'fishing','running','day:1')")
    store.conn.execute("INSERT INTO day_permit(plan,begin_id,guild,human,state) VALUES(1,'fixture',3,1,'open')")
    service = heroics.Heroics.__new__(heroics.Heroics)
    service.conn = store.conn
    service.realm = SimpleNamespace(bodies=bodies)
    service.context = lambda: (3, 1)
    service.app = SimpleNamespace(world=SimpleNamespace(online=set(people)))
    service.guild = SimpleNamespace(raid_night=SimpleNamespace(members_in_play=set),
                                   offline=SimpleNamespace(members_in_play=set))
    service.ports = SimpleNamespace(cast=lambda: dict.fromkeys(people), away=set, in_dungeon=set,
                                    persona=people.get, human_guid=1)
    monkeypatch.setattr(bodies, "others", lambda *a, **k: pytest.fail("per-member body refresh"))
    _, states = service.states((101, 102, 103))
    assert len(reads) == 1 and states[101].free and not states[102].free and states[103].free
    # An unrelated holder is not hidden behind her own higher-ranked reservation.
    monkeypatch.setattr(bodies, "others_of", lambda *a, **k: {101: set()})
    assert not service.states((102,))[1][102].free
    def unavailable(*a, **k):
        raise BodyEvidenceUnavailable("body evidence unavailable")
    monkeypatch.setattr(bodies, "others_of", unavailable)
    assert not any(c.free for c in service.states((101, 102))[1].values())


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

    assert LifeCfg().dungeons is True  # on by default (2026-11-25); the seeds below need it set
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

        @bridge_send
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


@pytest.mark.parametrize("witnesses", [(11, 12, 13, 14, 15), (12, 14)])
@pytest.mark.parametrize("prior_kills", [0, 1])
def test_witnessed_wipe_survives_restart_before_next_heartbeat(tmp_path, store, clock, witnesses, prior_kills):
    import asyncio
    import json
    from leeroy_mind.protocol import Boss

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.agree()
        await rig.h.tick()
        await rig.app.event(rig.event("progress", stage="running", instance_id=47))
        neighbor = heroics.offer(store, propose(store), day="neighbor", now=clock.now(), guild=3, human=1)
        store.conn.execute("UPDATE member_heroic SET phase='running',sent=?,instance=48 WHERE id=?",
                           (clock.now(), neighbor))
        await clock.advance(1, 1)
        wipe = Boss(int(clock.now() * 1000), 602, 47, 2, "Loken", "wiped", witnesses)
        await rig.app.event(wipe)
        await rig.app.event(wipe)
        await rig.app.event(replace(wipe, instance_id=48, boss_id=0))
        await rig.app.event(replace(wipe, boss_id=1, members=(11, 99)))
        assert store.conn.execute("SELECT COUNT(*) FROM member_heroic_encounter").fetchone()[0] == 2
        await clock.advance(1, 1)
        await rig.app.event(replace(wipe, ts=int(clock.now()*1000), state="killed", boss_id=1))

        resumed = Rig(tmp_path, store, clock)
        await resumed.connect()
        resumed.ident = rig.ident
        terminal = replace(resumed.event("failed", reason="cleanup_failed:world_restart", instance_id=47, kills=prior_kills,
                                         map_id=602, revision=18, returned=False), ts=wipe.ts)
        await resumed.app.event(terminal)
        await resumed.app.event(terminal)
        await resumed.app.event(replace(terminal, wipes=1, revision=19, returned=True))
        assert not resumed.h.row(rig.ident)["returned"]  # Conflicting native terminal.
        await resumed.app.event(replace(terminal, revision=20, returned=True))
        row = resumed.h.row(rig.ident)
        assert (row["terminal"], row["wipes"], row["returned"]) == ("failed", 0, 1)
        memories = list(store.conn.execute("SELECT owner,text FROM memory WHERE key=? ORDER BY owner",
                                           ("heroic:" + rig.ident,)))
        assert [m["owner"] for m in memories] == (list(range(11, 16)) if prior_kills else list(witnesses))
        for m in memories:
            if m["owner"] in witnesses:
                assert "1 wipe" in m["text"] and "Loken" in m["text"]
            else:
                assert "1 boss kill" in m["text"] and "wipe" not in m["text"] and "Loken" not in m["text"]
        (event,) = store.conn.execute("SELECT guid,payload FROM game_event WHERE type='member_dungeon'")
        payload = json.loads(event["payload"])
        assert (payload["start"], payload["instance"], payload["kills"], payload["wipes"]) == (rig.ident, 47, prior_kills, 1)
        assert event["guid"] == min(witnesses) and payload["witnesses"] == list(witnesses)
        facts = {s.key: s for s in heroics.seeds(store.conn, clock.now(), 3, 1)}
        assert "1 wipe" in facts["heroic:" + rig.ident].fact
        assert set(facts["heroic:" + rig.ident].witnesses) == set(witnesses)
        assert "heroic:" + neighbor not in facts
        assert not any(":1:killed:" in key for key in facts)

    asyncio.run(go())


def test_split_encounter_witnesses_do_not_claim_whole_run_counts(tmp_path, store, clock):
    import asyncio
    import json
    from leeroy_mind.protocol import Boss

    async def go():
        members = (101, 102, 103, 104, 105)
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        group = dict(propose(store), members=list(members), leader=101)
        start = rig.ident = heroics.offer(store, group, day="split", now=clock.now(), guild=3, human=1)
        store.conn.execute("UPDATE member_heroic SET phase='running',instance=47,sent=? WHERE id=?",
                           (clock.now(), start))
        for boss, witnesses in [(0, members[:2]), (2, members[2:4])]:
            await clock.advance(1, 1)
            wipe = Boss(int(clock.now()*1000), 602, 47, boss, heroics.BOSSES[boss], "wiped", witnesses)
            await rig.app.event(wipe)
            await rig.app.event(wipe)
        terminal = replace(rig.event("failed", reason="cleanup_failed:world_restart", instance_id=47,
                                     kills=1, map_id=602, revision=12, returned=False), guids=members)
        await rig.app.event(terminal)
        await rig.app.event(terminal)
        await rig.app.event(replace(terminal, wipes=2, revision=13, returned=True))
        assert not rig.h.row(start)["returned"]
        await rig.app.event(replace(terminal, revision=14, returned=True))
        row = rig.h.row(start)
        assert (row["kills"], row["wipes"], row["returned"]) == (1, 0, 1)
        (event,) = store.conn.execute("SELECT payload FROM game_event WHERE type='member_dungeon'")
        payload = json.loads(event[0])
        assert (payload["start"], payload["instance"], payload["wipes"], payload["witnesses"]) == (start, 47, 2, [])
        facts = list(heroics.seeds(store.conn, clock.now(), 3, 1))
        assert {s.key for s in facts if s.key == "heroic:" + start} == set()
        assert len(facts) == 2
        memories = dict(store.conn.execute("SELECT owner,text FROM memory WHERE key=?", ("heroic:" + start,)))
        assert set(memories) == set(members)
        for boss, witnesses in [(0, members[:2]), (2, members[2:4])]:
            (seed,) = [s for s in facts if heroics.BOSSES[boss] in s.fact]
            assert tuple(seed.witnesses) == witnesses and tuple(seed.about) == witnesses
            for member, words in memories.items():
                assert (heroics.BOSSES[boss] in words) == (member in witnesses)
                assert "1 boss kill" in words and "0 wipes" not in words
                if member in witnesses:
                    assert "at least" in words and "1 wipe" in words
        assert "wipe" not in memories[105]

    asyncio.run(go())


def test_midnight_preserves_a_still_valid_single_proposal(tmp_path, store, clock):
    import asyncio
    from datetime import timedelta

    async def go():
        almost_midnight = clock.local().replace(hour=23, minute=59, second=50)
        delta = (almost_midnight - clock.local()).total_seconds()
        await clock.advance(delta, delta)
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.agree()
        await clock.advance(20, 20)
        assert clock.local().date() == almost_midnight.date() + timedelta(days=1)
        await rig.h.tick()
        assert rig.h.row(rig.ident)["phase"] == "running"
        assert store.conn.execute("SELECT count(*) FROM member_heroic").fetchone()[0] == 1

    asyncio.run(go())


def test_running_presence_spans_real_native_heartbeats_then_expires(tmp_path, store, clock):
    import asyncio
    from leeroy_mind.server.state import read_heroic_presence as heroic_play

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.agree()
        await rig.h.tick()
        for interval in (0, 60):
            if interval:
                await clock.advance(interval, interval)
                assert rig.h.members_in_play() == heroic_play(store.conn, clock.now()) == frozenset(range(11, 16))
            await rig.app.event(rig.event("progress", stage="running", instance_id=47, members_in_instance=5,
                                         boss_engaged=False, last_progress_ts=int(clock.now() * 1000)))
        await clock.advance(heroics.HEARTBEAT_FRESH + 1, 1000)
        assert not rig.h.members_in_play() and not heroic_play(store.conn, clock.now())

    asyncio.run(go())


def test_released_slot_preserves_unreturned_bodies_and_permits_a_disjoint_proposal(tmp_path, store, clock):
    import asyncio, json
    from conftest import make_persona

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.agree()
        await rig.h.tick()
        group = {g + 10: replace(m, guid=g+10, name=m.name + "x") for g, m in roster().items()}
        store.upsert_cast(list(group.values()), clock.now())
        rig.app.world.members.update(group)
        rig.app.world.online.update(group)
        rig.app.personas.update({g: make_persona(g, m.name) for g,m in group.items()})
        await rig.app.event(rig.event("failed", reason="cleanup_failed:world_restart", map_id=602,
                                     returned=False, slot_held=True, revision=12))
        await clock.advance(86400, 86400)
        await rig.h.tick()
        assert store.conn.execute("SELECT COUNT(*) FROM member_heroic").fetchone()[0] == 1
        await rig.app.event(rig.event("failed", reason="cleanup_failed:world_restart", map_id=602,
                                     returned=False, slot_held=False, revision=13))
        await rig.h.tick()
        r = store.conn.execute("SELECT roster FROM member_heroic WHERE phase='offered'").fetchone()
        assert r and set(json.loads(r[0])).isdisjoint(range(11, 16))
        assert heroics.holds(store.conn)[0].guids == frozenset(range(11, 16))

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
        (tmp_path / "mind.toml").write_text(off(f"[life]\ndungeons={str(enabled).lower()}\n"))

    async def go():
        try:
            assert app.guild.heroics is None and "day" not in app.realm.parts
            prior = heroics.offer(app.store, propose(app.store), day="prior", now=1, guild=3, human=1)
            app.guild.configure_dungeons(False)
            assert app.store.conn.execute(
                "SELECT phase,reason FROM member_heroic WHERE id=?", (prior,)
            ).fetchone()[:] == ("talk_only", "disabled")
            assert app.guild.heroics is None and "day" not in app.realm.parts
            for reload in (app._reload_config, lambda: app.night_runner.make_ctx("2026-10-29", (1, 10))):
                for on in (True, False):
                    toggle(on)
                    reload()
                    assert app.guild.heroics.enabled is on and app.cfg.life.dungeons is on
                    assert app.file_cfg.life.dungeons is on and app.engine.cfg.life.dungeons is on
                    app.tuning.refresh()
                    assert app.engine.cfg.life.dungeons is on
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


@pytest.mark.parametrize("lost_reply", [False, True])
def test_initial_receipt_failure_stays_sent_until_deferred_ack_or_absence(tmp_path, store, clock, monkeypatch,
                                                                        lost_reply):
    import asyncio
    from types import SimpleNamespace
    from leeroy_mind.bridge import ConnectionLost
    from leeroy_mind.guild.protocol import MemberDungeonStatus, StartMemberDungeon
    from leeroy_mind.protocol import Ack

    people = {g + 90: replace(m, guid=g + 90, name=f"Member{g + 90}") for g, m in roster().items() if g <= 15}
    monkeypatch.setitem(globals(), "roster", lambda: people)

    async def go():
        rig = Rig(tmp_path, store, clock)
        await rig.connect()
        await rig.h.tick()
        rig.ident = store.conn.execute("SELECT id FROM member_heroic").fetchone()[0]
        assert heroics.open_chat(store.conn, rig.ident, "talk", clock.now())
        for guid in (101, 102, 103, 104):
            rig.post(guid)
        rig.h.finish_chat(SimpleNamespace(id="talk", reason="agreed", seed=SimpleNamespace(key="lfg:" + rig.ident)))
        entered, complete = asyncio.Event(), asyncio.Event()
        original_send = rig.app.send

        async def send(cmd, *, preserve_id=False, expected_epoch=None):
            if isinstance(cmd, MemberDungeonStatus):
                error = "unknown_dungeon" if complete.is_set() else "dungeon_receipt_unproven"
                return Ack(0, cmd.id, False, error)
            if isinstance(cmd, StartMemberDungeon) and cmd.day_prepared:
                entered.set()
                await complete.wait()  # Native INSERT and independent readback are outstanding.
                if lost_reply:
                    raise ConnectionLost("synthetic disconnect before admission reply")
                return Ack(0, cmd.id, False, "dungeon_receipt_unavailable")
            return await original_send(cmd, preserve_id=preserve_id, expected_epoch=expected_epoch)

        rig.app.send = send
        task = asyncio.create_task(rig.h.tick())
        await asyncio.wait_for(entered.wait(), 2)
        assert rig.h.row(rig.ident)["phase"] == "sent"
        assert heroics.holds(store.conn)
        await rig.h.status(rig.ident)
        assert rig.h.row(rig.ident)["phase"] == "sent"  # Pending native insertion is not proof of absence.
        complete.set()
        await task
        if lost_reply:
            assert rig.h.row(rig.ident)["phase"] == "sent"
            await rig.h.status(rig.ident)  # Exact-ID absence after a failed insert or restart before commit.
        assert rig.h.row(rig.ident)["phase"] == "talk_only"
        assert rig.h.row(rig.ident)["returned"] and not heroics.holds(store.conn)

    asyncio.run(go())
