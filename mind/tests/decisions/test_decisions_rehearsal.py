"""D4 across a raid night, a bank handoff and the next muster on one synthetic guild database."""
import asyncio
from dataclasses import replace
from datetime import datetime, time, timedelta
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from conftest import make_persona
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.console.decisionsview import decisions_view
from leeroy_mind.decisions import ledger, loot_rules, polls
from leeroy_mind.decisions.positions import vote_at
from leeroy_mind.decisions.attach import attach_decisions
from leeroy_mind.decisions.lineup import formed, tonight
from leeroy_mind.decisions.seeds import decision_seeds
from leeroy_mind.guild.roster import Roster
from leeroy_mind.protocol import encode_command
from leeroy_mind.training.ledger import Ledger
from leeroy_mind.realm import protocol as realm
from leeroy_mind.realm.actions import ActionContext
from leeroy_mind.realm.direct import DirectLedger
from leeroy_mind.storage.store import Store
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.training import store as training_store
from tests.guild.conftest import fake  # noqa: F401
from tests.guild.test_raid_night import setup  # noqa: F401
from tests.guild.test_raid_night_lineup import form10, formed_members
from tests.training.fakes import FakeTrainingPorts
from tests.training.test_tg_listener import real_listener, status
from leeroy_mind.server import engine as server_engine
from tests.training.test_tg_store import context, record
from tests.training.test_tg_ledger import feed, header, member, run as open_training_raid
from .test_lineup import event
from .test_loot_rules import receipt


def test_decisions_rehearsal(setup, tmp_path, clock, monkeypatch):
    async def play():
        ports, armory, night = setup
        seed = Store(ports.conn, clock=clock)
        seed.set_meta("human_guid", "1")
        seed.set_meta("guild_id", "8")
        people, bodies = {}, {}
        for g in range(101, 106):
            body = ports.members[g]
            title = {101: "Raid Lead", 104: "DKP"}.get(g, "")
            bodies[g] = replace(body, rank=1 if title else 3)
            people[g] = make_persona(g, body.name, raid_role={101:"tank", 102:"healer"}.get(g, "ranged"),
                                     guild_role="officer" if title else "raider", officer_title=title)
        ports.members, ports.personas = bodies, people
        seed.upsert_cast(list(bodies.values()), clock.now())
        # Configure only synthetic paths. Game reads, sends and training effects all go to fakes.
        conf = tmp_path / "mind.toml"
        conf.write_text('[server]\nenabled=true\n[guild]\nworldserver_conf="missing.conf"\n', encoding="utf-8")
        app = MindApp(load_config(conf), tmp_path, llm=FakeLLM(lambda model, turns: "I checked the bank." if "#bank-requests" in turns[-1]["content"] else "That tank coverage works for me."), clock=clock, rng_seed=59)
        app.server.config = replace(app.server.config, quiet_window=(time(10,11), time(10,12)))
        store, conn = app.store, app.store.conn
        ports.conn.close()
        ports.conn = conn
        app.world.members, app.world._cast_seen, app.personas = bodies, True, people
        app.world.is_officer = lambda g: g in {101, 104}
        app.guild = NS(ports=ports, armory=armory, cfg=night.cfg, raid_night=night,
                       offline=NS(active=False), roster=Roster(ports, night.cfg, armory))
        ports.enabled = True
        ports.app = app
        ports.lineup = lambda: tonight(conn, clock.now())
        ports.lineup_formed = lambda line, raid: formed(store, line.decision, raid, now=clock.now())
        training_dir = tmp_path / "training"
        training_dir.mkdir()
        trainer = FakeTrainingPorts(training_dir, clock)
        trainer.close()
        trainer.conn = conn
        listener, run, training_ports = real_listener(trainer)
        listener.ledger = Ledger(trainer)
        app.training = NS(listener=listener, applier=listener.applier, cfg=trainer.cfg)
        direct = DirectLedger(store, process_id="rehearsal", clock=clock)
        ctx = ActionContext(enabled=True, connected=True, presence_known=True, process_id="rehearsal",
                            generation=1, connection_epoch=1, guild=8, human=1)
        app.realm = NS(part=lambda _: direct, action_context=lambda: ctx, enabled=True, ports=NS())
        conf.write_text(conf.read_text() + '[decisions]\nenabled=true\nbank_restock_per_week=0\n', encoding="utf-8")
        svc = app.decisions = attach_decisions(app)
        await svc.on_hello(NS(features=("member_bank_mail", "training_enabled")))
        observe = app._handlers["cast"][-1]
        await observe(NS(members=[m for g, m in bodies.items() if g != 105]))

        bridge = FakeBridge()
        writer = NS(write=Mock(), drain=AsyncMock())
        fighting = False
        app.engine.raid.phase = lambda now: "combat" if fighting else "none"
        attempted_in_fight = []
        original_send = ports.send

        async def send(cmd):
            if fighting:
                attempted_in_fight.append(cmd)
            assert not fighting
            wire = json.loads(encode_command(cmd, int(clock.now()*1000)))
            await bridge._command(writer, wire)
            return await original_send(cmd)

        ports.send = send
        flask = 46377
        slots = [(0, flask, 20), (1, flask, 20), (2, 34753, 20)]
        armory.bank = lambda guild: {"tabs": [{"tab": 0, "slots": list(slots)}]}

        async def bank_send(cmd):
            assert svc.rules_for("bank").checks[cmd.id]()
            direct.before_send(cmd, context=ctx)
            ack = await send(cmd)
            assert direct.on_ack(cmd, ack)
            kind, value = ("bank_withdraw", slots.pop(0)[2]) if isinstance(cmd, realm.BankWithdraw) else ("mail_send", 77)
            assert direct.on_job(realm.RealmJob(0, cmd.id, kind, "done", "", 104, value))
            return ack

        app.realm.ports.send = bank_send
        def rows(kind):
            return conn.execute("SELECT * FROM guild_decision WHERE kind=? ORDER BY id", (kind,)).fetchall()

        async def tick(kind):
            await svc.rules_for(kind).tick(app, clock.now())

        def seeds():
            return decision_seeds(conn, now=clock.now(), personas=people, officers={101, 104})

        async def scheduled_votes(poll, *, owner=None):
            voters = polls.voters_of(poll) - {app.human_guid}
            svc._present = lambda guids, now: dict.fromkeys(guids, "online")
            if owner:
                reply = await svc.handle(dict(action="vote", poll=poll["id"], option=owner))
                assert reply["ok"]
            clock._t = max(vote_at(poll["id"], g, poll["opened"], poll["closes"]) for g in voters) + 1
            await svc.tick()
            assert {r[0] for r in conn.execute("SELECT member FROM guild_poll_vote WHERE poll=?", (poll["id"],))} == voters | ({app.human_guid} if owner else set())

        async def post_seed(seed, speaker):
            server = app.server.engine
            # Pin availability and model wording; exercise the actual server opener, Discord
            # pipeline, recorded line, code block and atomic callback for its chosen seed.
            server.next_start = 0
            app.server.lease.ready = lambda: True
            app.guild.members_in_play = lambda: set()
            monkeypatch.setattr(app.engine.live_review, "config", replace(app.engine.live_review.config, enabled=True))
            # One available officer and one ordinary member. The scheduler chooses the private opener.
            free = {speaker:people[speaker], 102:people[102]}
            observed = NS(personas=free, in_play=frozenset(), away=frozenset(), local=clock.local(),
                          human=1, officers=frozenset({speaker}), classes={}, ties={}, seeds=[seed],
                          states={g:NS(presence=NS(state="online")) for g in free})
            monkeypatch.setattr(server_engine.runtime, "snapshot", AsyncMock(return_value=observed))
            monkeypatch.setattr(server_engine, "presence_of", lambda *a, **kw: NS(state="online"))
            monkeypatch.setattr(server_engine.rhythm, "length", lambda rng: 1)
            server._observe = AsyncMock(return_value=observed)
            async def compose(turn, meta):
                answer = await app.llm.chat("fake", [{"role":"user", "content":turn.instruction}], options={})
                reason = app.engine._deliver_discord(turn,meta,people[turn.speaker],answer.text,[])
                assert reason is None, reason
            app.engine.speak_meta = compose
            assert not server._blocked(clock.now())
            await server.tick(clock.now())
            [conversation] = list(server._live.values())
            if seed.bank_announcement(conversation.channel):
                assert conversation.cast == [speaker] and conversation.maximum == 1
            await conversation.task
            assert len(conversation.messages) == 1, conversation.reason
            row = conn.execute("SELECT channel,text FROM discord_message WHERE id=?", (conversation.messages[0],)).fetchone()
            assert row["channel"] == seed.channels[0]
            assert row["text"].startswith("I checked the bank." if seed.kind in ("bank_request", "restock") else "That tank coverage works for me.")
            if seed.kind in ("bank_request","restock"):
                assert row["channel"] == "bank-requests" and seed.fact in row["text"].split("```")[1]

        start = clock.now()
        at = (datetime.fromtimestamp(start).replace(hour=0, minute=1, second=0) + timedelta(days=1)).timestamp()
        # Start at local noon the day before so proposal, posting and formation all have room.
        clock._t = start = at-12*3600
        event(store, at)
        await tick("lineup")
        [line] = rows("lineup")
        assert line["status"] == "proposed" and json.loads(line["positions"])
        await post_seed(next(s for s in seeds() if s.key == f"decision:{line['id']}:proposed"), 101)
        clock._t += 2*3600
        await tick("lineup")
        assert rows("lineup")[0]["message"]
        # A different withdrawal lacks its terminal job. It cannot monopolize the officer.
        orphan = ledger.propose(store,kind="bank",subject="bank:unresolved", value={"count":20},
                                positions=[],now=clock.now())
        ledger.settle(store,orphan,settled_by=104,now=clock.now())
        # The missing terminal job remains reserved alongside the independent requests.
        stuck = realm.BankWithdraw("e"*32,104,34753,20,0,102)
        ref = f"decision:{orphan}:withdraw"
        assert direct.prepare(stuck,owner="decision",ref=ref,context=ctx,expects_job=True)
        direct.before_send(stuck,context=ctx)
        from leeroy_mind.protocol import Ack
        direct.on_ack(stuck,Ack(0,stuck.id,True,""))
        await tick("bank")
        requests = [s for s in seeds() if s.kind == "bank_request"]
        assert len(requests) >= 2
        for seed in requests[:2]:
            await post_seed(seed, 104)
        # The same sole officer can publish completed weekly stock through the same scheduler.
        from leeroy_mind.decisions import restock
        week = clock.local().strftime("%G-W%V")
        store.set_meta(restock.PREFIX+week,json.dumps(dict(week=week,officer=104,no_maker=[],results={"0":"credited"},
            stacks=[dict(item=43015,count=10,tab=0,maker=102)])))
        await post_seed(next(s for s in seeds() if s.kind == "restock"), 104)
        assert json.loads(store.get_meta(restock.PREFIX+week))["message"]
        clock._t = at - 6*3600
        at = clock.now()
        await tick("lineup")
        assert any(c["type"] == "guild_add_roster" and c["members"] for c in bridge.commands)
        await form10(setup)
        assert set(formed_members(ports)) == set(json.loads(rows("lineup")[0]["value"])["members"])
        assert rows("lineup")[0]["status"] == "in_effect"
        raid = night.session.raid_id
        assert loot_rules.snapshot(conn, raid).mode == "dkp"

        # A recent poll initially prevents another. A charged close award opens one early.
        prior = polls.open_poll(store, channel="officer-chat", question="Rules?", options=loot_rules.MODES,
                                voters=[101, 104], closes=start-4*86400, now=start-5*86400,
                                kind="loot_rule", subject="loot")
        polls.close_due(store, now=start)
        polls.mark_handled(store, prior, now=start)
        assert not loot_rules.poll_due(conn, now=at, poll_days=14, human=1)
        receipt(store, f"guild:loot:{raid}:close", at, winner=101, runner=104, close=True)
        assert loot_rules.poll_due(conn, now=at, poll_days=14, human=1)
        fighting = True
        await tick("loot_rule")
        [poll] = polls.open_polls(conn)
        await scheduled_votes(poll, owner="loot_council")
        await svc.tick()
        assert polls.get_poll(conn, poll["id"])["handled"] is not None
        await tick("bank")
        assert rows("loot_rule")[-1]["status"] == "settled"
        assert not any(c["type"] == "bank_withdraw" for c in bridge.commands)
        fighting = False
        with conn:
            conn.execute("UPDATE guild_raid SET ended=? WHERE raid_id=?", (at+1, raid))
        app.guild.raid_night = NS(active=False)
        clock._t += 2
        await tick("loot_rule")
        assert loot_rules.current_rule(conn).mode == "loot_council" and loot_rules.rule_for_raid(conn, raid).mode == "dkp"

        for _ in range(6):
            await tick("bank")
        assert direct.state("decision",ref) == ("acked",None,None)
        from unittest.mock import patch
        # Once the actionable requests finish, an unresolved one must still permit fresh discovery.
        from leeroy_mind.decisions import bank
        with patch.object(bank,"read",wraps=bank.read) as read_again:
            await tick("bank")
            read_again.assert_awaited()
        assert [c["type"] for c in bridge.commands if c["type"] in ("bank_withdraw","mail_send")] == ["bank_withdraw","mail_send"]
        delivered = ledger.get(conn, int(requests[0].key.split(":")[1]))
        assert delivered["status"] == "in_effect"
        effect = json.loads(delivered["effect_ref"])
        assert effect["first_attachment_guid"] == 77 and "mail_id" not in effect
        assert json.loads(ledger.get(conn,int(requests[1].key.split(":")[1]))["effect_ref"])["failed"] == "last_stack"

        # Two actual training pull receipts from the raid; the lead settles its one menu change.
        for i in range(2):
            ts = int((at-600+i*200)*1000)
            rec = record(f"{ts}-77-0", outcome="wiped")
            rec.header = replace(rec.header, start_ms=ts, end_ms=ts+90000)
            training_store.put_pull(conn, rec, context(raid_id=raid, lead_guid=101, covariates={"healers":1}),
                                    {101:"tank"}, received=ts/1000+90)
        await tick("strategy")
        clock._t += 2*3600
        await tick("strategy")
        [strategy] = rows("strategy")
        assert strategy["status"] == "settled"
        observed = open_training_raid(trainer, human=False, instance=77)
        run.session.raid_id = observed.session.raid_id
        run.session.ctx.instance_id = 77
        assert loot_rules.snapshot(conn, run.session.raid_id).mode == "loot_council"
        training_ports.state = replace(training_ports.state, run_key=listener.run_key(run), instance_id=77)
        await listener.on_start(run)
        from leeroy_mind.talk.raid import RaidPhase
        phase = app.engine.raid.phase
        app.engine.raid.phase = lambda now: RaidPhase.WIPE_DEBRIEF
        prior_commands = list(training_ports.sent)
        await listener.on_dc_status(status(trainer, name="Lord Marrowgar", entry=36612))
        assert training_ports.sent == prior_commands and rows("strategy")[0]["status"] == "settled"
        app.engine.raid.phase = phase
        await listener.on_dc_status(status(trainer, name="Lord Marrowgar", entry=36612))
        assert any(getattr(c, "key", None) == "PullPlayerReleaseDelay" for c in training_ports.sent)
        h = header(trainer)
        await feed(listener.ledger, member(h), h)
        recorded = conn.execute("SELECT covariates FROM tg_pull WHERE id=?", (h.id,)).fetchone()
        assert json.loads(recorded[0])["guild_strategy"] == strategy["id"]
        with conn:
            conn.execute("UPDATE guild_raid SET ended=? WHERE raid_id=?", (clock.now()+60, run.session.raid_id))
        assert rows("strategy")[0]["status"] == "in_effect"

        await observe(NS(members=list(bodies.values())))
        await tick("rank")
        newcomer = next(r for r in rows("rank") if r["subject"] == "rank:105")
        poll = polls.get_poll(conn, newcomer["poll"])
        await scheduled_votes(poll)
        assert ledger.get(conn, newcomer["id"])["status"] == "proposed"
        clock._t = poll["closes"]
        await svc.tick()
        assert polls.get_poll(conn, poll["id"])["handled"] is not None
        assert app.guild.roster._ranks_due
        await app.guild.roster.sync_ranks()
        assert any(c["type"] == "set_rank" and c["guid"] == 105 and c["rank"] == 4 for c in bridge.commands)
        assert ledger.get(conn, newcomer["id"])["status"] == "in_effect"
        rank_reply = await svc.handle(dict(action="overrule", id=newcomer["id"]))
        assert not rank_reply["undone"] and "scheduled" in rank_reply["message"]
        result = await svc.handle(dict(action="overrule", id=rows("loot_rule")[-1]["id"]))
        assert result["undone"] and loot_rules.current_rule(conn).mode == "dkp"
        view = decisions_view(conn, now=clock.now(), enabled=True)["decisions"]
        assert {r["id"] for r in view} == {r["id"] for r in ledger.history(conn)}
        assert all(r["changed"] for r in view if r["status"] in ("in_effect", "failed", "overruled"))
        assert not attempted_in_fight and not ports.said and not any(c["type"] == "say" for c in bridge.commands)
        await listener.close()
        store.close()

    asyncio.run(play())
