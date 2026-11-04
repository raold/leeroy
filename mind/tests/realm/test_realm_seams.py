"""Wave 4's seams: where two Realm parts, built apart against fakes, meet. Every test here runs
both parts for real (with the real service, Store and DirectLedger where a send is involved);
only the native side, the model and the clock are test boundaries."""
import uuid

from conftest import run, seed_test_cast
from leeroy_mind.protocol import Ack
from leeroy_mind.realm import bank, canon, protocol, rivals
from leeroy_mind.realm.direct import DirectLedger
from leeroy_mind.realm.ports import RealmDisabled
from leeroy_mind.realm.protocol import BankStock, GrantAchievement
from leeroy_mind.realm.race import Race
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.training.benchmark import allowed_bosses
from realm.fakes import RealmApp, enabled
from test_realm_direct import LIVE, PROCESS
from test_realm_race import DAY, T0, _training_ports, at, guild_kill
from test_realm_rivals import (approved as rivals_approved, connect, finish, grows, kinds, live_realm,
                               plan_of, pump, seed_roster, step_row, with_cards)


def _refused(ledger, cmd, owner, ref, error, *, expects_job=False):
    assert ledger.prepare(cmd, owner=owner, ref=ref, context=LIVE, expects_job=expects_job) == cmd.id
    ledger.before_send(cmd, context=LIVE)
    assert ledger.on_ack(cmd, Ack(1, cmd.id, False, error))
    return ledger.state(owner, ref)


# -- item 8: native's busy:<owner> survives the direct ledger --------------------------------------

def test_busy_reason_survives_the_ledger_for_canon_and_bank(store):
    ledger = DirectLedger(store, process_id=PROCESS, clock=store.clock)
    grant = GrantAchievement(uuid.uuid4().hex, 101, 2186)
    state = _refused(ledger, grant, "canon", "canon:2186:101:0", "busy:training_owner")
    # The column holds [a-z0-9_] only, so native's ':' is kept as '_'; the owner is still there.
    assert state == ("refused", "busy_training_owner", None)
    assert canon._outcome(*state[:2]) == "deferred"  # Canon defers it as it does gm_mode.
    stock = BankStock(uuid.uuid4().hex, 104, 3, 47586, 1)
    state = _refused(ledger, stock, "step", "step:bank_stock:" + "a" * 64 + ":0", "busy:operation_identity",
                     expects_job=True)
    assert state == ("refused", "busy_operation_identity", None)
    assert bank._verdict("bank_stock", state) == "send"  # Nothing moved: the bank asks again.
    # A reason native never gives (spaces, capitals) still reads as the ledger's own fallback.
    odd = GrantAchievement(uuid.uuid4().hex, 102, 2186)
    assert _refused(ledger, odd, "canon", "canon:2186:102:0", "Busy: Raid")[1] == "native_refused"


# -- item 10: a step item refused or lost before anything moved is sent again ----------------------

def _answering(app, kind, answers):
    """Native answers each command of this type with the next (ok, error); the last repeats."""
    send, queue = app.send, list(answers)

    async def answer(cmd, *, preserve_id=False):
        if type(cmd) is not kind:
            return await send(cmd, preserve_id=preserve_id)
        app.sent.append(cmd)
        ok, error = queue.pop(0) if len(queue) > 1 else queue[0]
        return Ack(0, cmd.id, ok, error)
    app.send = answer


def test_a_step_item_refused_before_anything_moved_is_sent_again(tmp_path, store, clock):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    _answering(app, protocol.GuildRename, [(False, "realm_off"), (True, "")])
    plan = with_cards(plan_of())
    _, sha = rivals_approved(tmp_path, clock, plan)
    steps, ledger, ref = realm.part("steps"), realm.part("direct"), f"step:rivals:{sha}:0"
    lost = []
    send = realm.ports.send

    async def lost_before_the_barrier(cmd):
        if type(cmd) is protocol.RealmActors and not lost:
            lost.append(cmd)
            raise RealmDisabled("realm disabled")  # As the ports raise once a ticket goes stale.
        return await send(cmd)

    async def go():
        await connect(app)
        await steps.tick()                        # native's realm switch was off for a moment
        assert kinds(app) == ["GuildRename"] and step_row(store)[0] is None
        assert ledger.state("step", ref)[:2] == ("refused", "realm_off")
        realm.ports.send = lost_before_the_barrier
        await clock.advance(60, step=60)
        await steps.tick()                        # the rename again, then the actor set is lost
        assert kinds(app) == ["GuildRename", "GuildRename"]
        assert ledger.state("step", f"step:rivals:{sha}:1")[0] == "abandoned"  # never left prepared
        await clock.advance(60, step=60)
        await steps.tick()                        # the actor set again; the grows are enrolled
        await pump(realm, clock)
        await finish(app, clock, grows(app))
        await clock.advance(60, step=60)
        await steps.tick()
    run(go())
    first, second, actors, *grown = app.sent
    assert first.id != second.id and (first.name, second.name) == ("Rival Name", "Rival Name")
    assert type(actors) is protocol.RealmActors and actors is not lost[0] and len(grown) == 3
    applied, result = step_row(store)
    assert applied is not None and (result["rename"], result["actors"]) == ("acked", "acked")
    assert ledger.state("step", ref) == ("acked", None, None)


# -- item 9: native's admission refusals of a guild_grow are refusals, not unknowns -----------------

def test_a_grow_native_refuses_at_admission_frees_its_bodies_and_grows_again(tmp_path, store, clock):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    plan = with_cards(plan_of())
    _, sha = rivals_approved(tmp_path, clock, plan)
    seven = next(g for g in plan["guilds"] if g["guild_id"] == 7)
    below = seven["raiders"][0]["guid"]  # Re-rolled below 80 between the plan and its apply.
    _answering(app, protocol.GuildGrow, [(False, f"level:{below}"), (True, "")])
    steps = realm.part("steps")

    async def go():
        await connect(app)
        await steps.tick()
        await pump(realm, clock)
        refused = realm.action_store.get(rivals.grow_id(sha, 7, 0))
        assert (refused.state, refused.result_ok, refused.result_reason) == ("completed", False, "refused")
        held = {guid for hold in realm.action_store.holds() for guid in hold.guids}
        assert not held & {r["guid"] for r in seven["raiders"]}  # Its bodies are free again.
        await finish(app, clock, grows(app, {8, 11}))
        await clock.advance(60, step=60)
        await steps.tick()                       # the queue's ten-minute pause on a failed scope
        await pump(realm, clock)
        assert len(grows(app, {7})) == 1 and step_row(store)[0] is None
        await clock.advance(600, step=60)
        await steps.tick()                       # then the next attempt of guild 7's grow
        await pump(realm, clock)
        await finish(app, clock, grows(app, {7})[1:])
        await clock.advance(60, step=60)
        await steps.tick()
    run(go())
    assert [g.id for g in grows(app, {7})] == [rivals.grow_id(sha, 7, 0), rivals.grow_id(sha, 7, 1)]
    assert step_row(store)[1]["grow"] == {"7": "acked", "8": "acked", "11": "acked"}


# -- item 7: Training's benchmark gate reaches the real race part -----------------------------------

def test_training_reaches_the_real_race_part_and_falls_back_without_it(tmp_path, store, clock):
    seed_test_cast(store)
    enabled(tmp_path, True)
    realm = attach_realm(RealmApp(tmp_path, store, clock))
    at(clock, T0)
    guild_kill(store, T0 - DAY, boss=0)
    training = _training_ports(store, clock, realm)
    assert realm.race is realm.part("race") and type(realm.race) is Race
    assert training.benchmark_allowed(631, 0, 25, False) is True
    assert allowed_bosses(training, "icc25") == (0,)
    realm.parts.pop("race")  # A Realm without its race part: Training's own history, exactly.
    assert realm.race is None and training.benchmark_allowed(631, 0, 25, False) is None
    assert allowed_bosses(training, "icc25") == ()
    run(realm.close())
