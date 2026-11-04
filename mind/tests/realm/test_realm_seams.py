"""Wave 4's seams: where two Realm parts, built apart against fakes, meet. Every test here runs
both parts for real (with the real service, Store and DirectLedger where a send is involved);
only the native side, the model and the clock are test boundaries."""
import asyncio
import json
from types import SimpleNamespace
import uuid

from conftest import run, seed_test_cast
from leeroy_mind.memory.writer import WriteResult
from leeroy_mind.protocol import Ack
from leeroy_mind.realm import bank, canon, mail, night, protocol, rivals, speech as speech_module
from leeroy_mind.realm.direct import DirectLedger
from leeroy_mind.realm.ports import RealmDisabled
from leeroy_mind.realm.protocol import BankStock, GrantAchievement
from leeroy_mind.realm.race import Race
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.training.benchmark import allowed_bosses
from realm.fakes import RealmApp, enabled
from test_realm_canon import KILLS as CANON_KILLS, Harness as CanonHarness, approved_plan as canon_plan
from test_realm_direct import LIVE, PROCESS
from test_realm_mail import Always
from test_realm_night import Rig as NightRig
from test_realm_race import DAY, RIVALS, T0, _training_ports, at, benchmark, guild_kill, guild_pull
from test_realm_speech import (OTHERGUILD, TESTGUILD, VEX, FakeLLM as SpeechLLM, lines as speech_lines,
                               make_app as speech_app, place as speech_place, retire as speech_retire,
                               says as speech_says, speak as speech_speak, start as speech_start,
                               step as speech_step)
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


# -- item 5: the canon step seeds the real race ----------------------------------------------------

def test_the_canon_step_seeds_the_real_race(tmp_path, store, clock, monkeypatch):
    h = CanonHarness(tmp_path, store, clock, monkeypatch)
    canon_plan(h.root, h.store, h.clock)
    with store.transaction():
        for gid, role, faction, name, status in RIVALS:
            store.conn.execute("INSERT INTO realm_guild(id,role,faction,name,added,status) VALUES (?,?,?,?,?,?)",
                               (gid, role, faction, name, 1.0, status))

    async def go():
        realm = await h.start()
        assert type(realm.part("race")) is Race
        await h.drive(2)
        await h.close()
    run(go())
    [(result,)] = store.conn.execute("SELECT result FROM realm_step WHERE name='canon'").fetchall()
    assert json.loads(result)["race_seeded"] is True
    seeded = {(r[0], r[1], r[2], r[3]): (r[4], r[5]) for r in store.conn.execute(
        "SELECT guild,map,boss_id,difficulty,state,source FROM realm_progress")}
    canon_keys = {(k["map"], k["boss_id"], k["difficulty"]) for k in CANON_KILLS}
    by_canon = {key: value[0] for key, value in seeded.items() if value[1] == "canon"}
    # The progression rival holds every canon kill by canon; drama all but the newest.
    assert {key[1:] for key, state in by_canon.items() if key[0] == 50 and state == "killed"} == canon_keys
    assert by_canon[(60, 631, 12, 3)] == "progressing"
    assert not any(key[0] == 80 for key in seeded)  # A retired rival is never seeded.


# -- item 1: the post office's bank gifts go through the real bank ------------------------------------

def _mail_realm(tmp_path, store, clock):
    """The real service with every part, his raid quiet, the cast online and free to mail."""
    app = RealmApp(tmp_path, store, clock)
    app.personas = seed_test_cast(store)
    app.world = SimpleNamespace(human_online=False, online=set(app.personas), afk=set(), group=None,
                                in_group=lambda guid: False)
    app.guild = SimpleNamespace(raid_night=None)
    enabled(tmp_path, True)
    return app, attach_realm(app)


async def _settled(realm):
    for _ in range(20):
        children = tuple(realm._children)
        if not children:
            return
        await asyncio.gather(*children, return_exceptions=True)


def _of(app, kind):
    return [c for c in app.sent if type(c) is kind]


def test_a_bank_gift_is_withdrawn_once_through_the_real_bank(tmp_path, store, clock):
    app, realm = _mail_realm(tmp_path, store, clock)
    vault, post = realm.part("bank"), realm.part("mail")
    vault.ledger.record("in", "stock", 104, 47586, 1)   # one of the stock step's BoEs
    vault.ledger.record("in", "stock", 104, 47590, 1)
    assert vault.gift_boe() == (47586, 3)                # the BoEs tab
    post.rng = Always(1)                                  # the gift comes from the bank
    items = post._gift_items()
    assert items == [{"item_id": 47586, "count": 1, "from": "bags", "bank_tab": 3}]
    with store.transaction():
        first = post._insert(clock.now(), 102, "gift", items, "For you", "Saw this, thought of you.", "ready")
    assert vault.gift_boe() == (47590, 3)                 # the waiting gift has its BoE claimed
    with store.transaction():
        second = post._insert(clock.now(), 103, "gift", post._gift_items(), "Here", "Take it.", "ready")
    assert vault.gift_boe() is None

    async def tick(seconds=10):
        await clock.advance(seconds, step=seconds)
        await post.tick()
        await _settled(realm)

    async def go():
        await connect(app)
        app.world.human_online = True
        await tick()
        withdraw, refused = _of(app, protocol.BankWithdraw)  # each BoE first, into her bags
        assert (withdraw.guid, withdraw.item_id, withdraw.count, withdraw.tab) == (102, 47586, 1, 3)
        assert (refused.guid, refused.item_id) == (103, 47590) and _of(app, protocol.MailSend) == []
        await tick()                                       # their jobs are still to come: nothing again
        assert len(_of(app, protocol.BankWithdraw)) == 2
        await app.event(protocol.RealmJob(int(clock.now() * 1000), withdraw.id, "bank_withdraw", "done",
                                          "", 102, 1))
        await app.event(protocol.RealmJob(int(clock.now() * 1000), refused.id, "bank_withdraw", "failed",
                                          "no_rights", 103, 0))
        await tick()
        [mail] = _of(app, protocol.MailSend)
        assert mail.from_guid == 102 and mail.items == (protocol.MailItem(47586, 1, "bags"),)
        for _ in range(3):
            await tick()
        await realm.close()
    run(go())
    assert len(_of(app, protocol.BankWithdraw)) == 2 and len(_of(app, protocol.MailSend)) == 1
    states = dict(store.conn.execute("SELECT id,state FROM realm_mail").fetchall())
    assert states[first] == "dispatched" and states[second] == "skipped"
    assert store.conn.execute("SELECT reason FROM realm_mail WHERE id=?", (second,)).fetchone()[0] == "no_rights"
    assert [tuple(r) for r in store.conn.execute(
        "SELECT dir,reason,guid,item_id,count FROM realm_bank ORDER BY id")] == [
        ("in", "stock", 104, 47586, 1), ("in", "stock", 104, 47590, 1), ("out", "gift", 102, 47586, 1)]
    assert vault.ledger.balance(47586) == 0 and vault.ledger.balance(47590) == 1


# -- item 2: the night writes the post office's letters through its API -------------------------------

def test_the_night_writes_letters_through_the_post_office(tmp_path, store, clock):
    app, realm = _mail_realm(tmp_path, store, clock)
    post = realm.part("mail")
    remembered = []

    async def write(candidate, *, enforce_limits=True):
        remembered.append(candidate)
        return WriteResult(len(remembered), "inserted")
    app.writer = SimpleNamespace(write=write)
    assert post.post(999, "minutes") is None   # no stored persona: nobody could ever write it
    letter, wide = post.post(102, "absence"), post.post(103, "absence")
    assert [need.id for need in mail.texts_wanted(store.conn)] == [letter, wide]
    rig = NightRig(store, tmp_path)
    run(night.run_units(rig.ctx({"realm_letter": [{"subject": "Long time", "body": "Come back soon."}]}),
                        [f"letter:{letter}"]))
    # A subject of 40 characters may still be over the 64 bytes native takes: refused, it waits.
    run(night.run_units(rig.ctx({"realm_letter": [{"subject": "é" * 40, "body": "Come back soon."}]}),
                        [f"letter:{wide}"]))
    rows = {r[0]: tuple(r[1:]) for r in store.conn.execute("SELECT id,state,subject FROM realm_mail")}
    assert rows[letter] == ("ready", "Long time") and rows[wide] == ("awaiting_text", "")
    assert [need.id for need in mail.texts_wanted(store.conn)] == [wide]
    assert store.conn.execute("SELECT count(*) FROM memory WHERE key LIKE 'realm-mail%'").fetchone()[0] == 0

    async def go():
        await connect(app)
        app.world.human_online = True
        await clock.advance(10, step=10)
        await post.tick()
        await _settled(realm)
        [sent] = _of(app, protocol.MailSend)
        await app.event(protocol.RealmJob(int(clock.now() * 1000), sent.id, "mail_send", "done", "", 102, 0))
        await clock.advance(10, step=10)
        await post.tick()
        await _settled(realm)
        await realm.close()
    run(go())
    # The sender's one private memory of it is the post office's, never a second one from the night.
    assert [(c.owner, c.key, c.scope, c.scope_with) for c in remembered] == [
        (102, f"realm-mail:{letter}", "private", 1)]


# -- item 3: speech says the night's beats, screened again -------------------------------------------

BEATS = ["LF2M for ICC 25 tonight, tanks and healers.", "Lich King next week, mark it."]


def test_speech_says_the_nights_beats_and_screens_them_again(tmp_path, store, clock, monkeypatch):
    s = speech_app(tmp_path, store, clock, monkeypatch, llm=SpeechLLM("composed by the live model"))
    speech_place(store, VEX, "uc_bank")
    speech_retire(store, OTHERGUILD)
    rig = NightRig(store, tmp_path)
    run(night.run_units(rig.ctx({"realm_beat": [{"lines": BEATS}]}), [f"beat:{TESTGUILD}"]))
    with store.transaction():
        assert [f.text for f in night.beat_lines_in_unit(store, TESTGUILD, human=rig.ctx().ident)] == BEATS
    # After the night, a word of the first beat joins the blocklist: speech screens it again.
    (tmp_path / "realm" / "blocklist.txt").write_text("blockedword\ntanks\n", encoding="utf-8")

    async def go():
        await speech_start(s)
        assert await speech_speak(s) == "trade"   # the first beat is dropped, a composed line goes
        assert [line.text for line in speech_says(s, "trade")] == ["composed by the live model"]
        assert len(s.llm.calls) == 1
        await clock.advance(200, step=200)
        assert await speech_speak(s) == "trade"   # the second beat, as the night wrote it
        assert [line.text for line in speech_says(s, "trade")][1:] == [BEATS[1]]
        assert len(s.llm.calls) == 1                # no model call for a beat
        await s.realm.close()
    run(go())
    said = [(r["text"], r["state"], r["reason"]) for r in speech_lines(store)]
    assert said == [(BEATS[0], "dropped", "blocklist"), ("composed by the live model", "sent", None),
                    (BEATS[1], "sent", None)]


# -- item 4: a benchmark loss makes its rival boast ------------------------------------------------

def test_a_benchmark_loss_makes_the_rival_boast_in_trade(tmp_path, store, clock, monkeypatch):
    s = speech_app(tmp_path, store, clock, monkeypatch, llm=SpeechLLM("too slow, as always"))
    speech_place(store, VEX, "uc_bank")
    speech_retire(store, OTHERGUILD)
    at(clock, T0)
    guild_kill(store, T0 - DAY, boss=0)
    guild_pull(store, "his:1", T0 - 10 * DAY, 300.0, boss=0)
    race = s.realm.part("race")

    async def go():
        await speech_start(s)
        assert race.on_benchmark(benchmark(1, clock.now(), guild=TESTGUILD, kills=((0, 250.0),))) == "counted"
        await speech_step(s)                      # the boast, at the speech part's next tick
        [line] = speech_says(s, "trade")
        assert (line.guid, line.text) == (VEX, "too slow, as always")
        prompt = s.llm.calls[0]["messages"][-1]["content"]
        assert "faster than us" in prompt and "Lord Marrowgar" in prompt
        await speech_step(s, 3)                   # once per loss
        assert len(speech_says(s, "trade")) == 1
        await s.realm.close()
    run(go())
    assert [(r["kind"], r["state"]) for r in speech_lines(store)] == [("rival", "sent")]


# -- item 6: the cards rivals writes are the cards speech reads -----------------------------------

def test_speech_reads_the_cards_the_rivals_step_wrote(tmp_path, store, clock):
    seed_roster(store)
    app, realm = live_realm(tmp_path, store, clock)
    plan = with_cards(plan_of())
    rivals_approved(tmp_path, clock, plan)
    progression = next(g for g in plan["guilds"] if g["role"] == "progression")
    officer = progression["actors"][0]

    async def go():
        await connect(app)
        await realm.part("steps").tick()      # the step writes its rows and cards first
        await realm.close()
    run(go())
    spec = speech_module._Spec("rival", "officer", officer["guid"], officer["name"], progression["guild_id"],
                               progression["name"], "trade", 0, 0, "Say one line in Trade.")
    prompt = realm.part("speech")._messages(spec)[-1]["content"]
    assert "About you: habits: short sentences" in prompt and f"name: {officer['name']};" in prompt
    assert f"Your guild: name: {progression['name']}; role: {rivals.PERSONALITY['progression']}" in prompt
    cards = tmp_path / "realm" / "cards"
    assert (cards / f"guild-{progression['guild_id']}.json").is_file()
    # A guild card he wrote by hand is never overwritten by a later apply.
    mine = cards / f"guild-{progression['guild_id']}.json"
    mine.write_text('{"name": "his own words"}\n', encoding="utf-8")
    realm.part("rivals")._write_cards(plan["cards"], [officer["guid"]], plan["guilds"])
    assert mine.read_text(encoding="utf-8") == '{"name": "his own words"}\n'
