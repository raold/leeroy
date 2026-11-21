"""D4 across a raid night, a bank handoff and the next muster on one synthetic guild database."""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from conftest import make_persona
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.console.decisionsview import decisions_view
from leeroy_mind.decisions import bank, ledger, loot_rules, polls
from leeroy_mind.decisions.attach import attach_decisions
from leeroy_mind.decisions.lineup import formed, tonight
from leeroy_mind.decisions.seeds import decision_seeds
from leeroy_mind.discord import store as discord
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
from tests.training.test_tg_listener import setup as training_listener, status
from tests.training.test_tg_store import context, record
from tests.training.test_tg_ledger import feed, header, member, run as open_training_raid
from .test_lineup import event
from .test_loot_rules import receipt


def test_decisions_rehearsal(setup, tmp_path, clock):
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
        app = MindApp(load_config(conf), tmp_path, llm=FakeLLM(), clock=clock, rng_seed=59)
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
        listener, run = training_listener(trainer)
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
        original_send = ports.send

        async def send(cmd):
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

        def post_seed(seed, speaker, channel):
            return discord.post(store, channel=channel, author=speaker, origin="mind", text=seed.fact,
                                now=clock.now())

        start = clock.now()
        at = start + 24*3600
        event(store, at)
        await tick("lineup")
        [line] = rows("lineup")
        assert line["status"] == "proposed" and json.loads(line["positions"])
        post_seed(next(s for s in seeds() if s.key == f"decision:{line['id']}:proposed"), 101, "raid-planning")
        clock._t += 2*3600
        await tick("lineup")
        assert rows("lineup")[0]["message"]
        await tick("bank")
        requests = [s for s in seeds() if s.kind == "bank_request"]
        assert len(requests) >= 2
        for seed in requests[:2]:
            with store.transaction():
                message = post_seed(seed, 104, "officer-chat")
                bank.record_post(app, seed, 104, message, clock.now())
        clock._t = at
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
        for voter in polls.voters_of(poll):
            assert polls.vote(store, poll["id"], voter, "loot_council", now=at)
        polls.close_due(store, now=at)
        await svc.rules_for("loot_rule").closed(app, polls.get_poll(conn, poll["id"]), "loot_council")
        await tick("loot_rule")
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
        assert [c["type"] for c in bridge.commands if c["type"] in ("bank_withdraw","mail_send")] == ["bank_withdraw","mail_send"]
        assert rows("bank")[0]["status"] == "in_effect"
        assert json.loads(rows("bank")[1]["effect_ref"])["failed"] == "last_stack"

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
        await listener.on_start(run)
        await listener.on_dc_status(status(trainer, name="Lord Marrowgar", entry=36612))
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
        for voter in polls.voters_of(poll):
            assert polls.vote(store, poll["id"], voter, "approve", now=clock.now())
        polls.close_due(store, now=clock.now())
        await svc.rules_for("rank").closed(app, polls.get_poll(conn, poll["id"]), "approve")
        await tick("rank")
        assert app.guild.roster._ranks_due
        await app.guild.roster.sync_ranks()
        assert any(c["type"] == "set_rank" and c["guid"] == 105 and c["rank"] == 4 for c in bridge.commands)
        assert ledger.get(conn, newcomer["id"])["status"] == "in_effect"
        result = await svc.handle(dict(action="overrule", id=rows("loot_rule")[-1]["id"]))
        assert result["undone"] and loot_rules.current_rule(conn).mode == "dkp"
        view = decisions_view(conn, now=clock.now(), enabled=True)["decisions"]
        assert {r["id"] for r in view} == {r["id"] for r in ledger.history(conn)}
        assert all(r["changed"] for r in view if r["status"] in ("in_effect", "failed", "overruled"))
        assert not ports.said and not any(c["type"] == "say" for c in bridge.commands)
        await listener.close()
        store.close()

    asyncio.run(play())
