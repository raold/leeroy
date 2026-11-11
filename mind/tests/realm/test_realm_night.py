"""The realm's night stage (plan Task 19) over actual Store rows, the real night runner and
budget, and a scripted FakeLLM: nothing here contacts Ollama, MySQL or the bridge."""
import json
import re
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind import board
from leeroy_mind.config import load_config
from leeroy_mind.memory.writer import MemoryWriter
from leeroy_mind.nightly import budget
from leeroy_mind.nightly.gpu import GpuArbiter
from leeroy_mind.nightly.llm import Reasoner
from leeroy_mind.nightly.runner import NightContext, NightRunner, Stage
from leeroy_mind.nightly.settings import NightlySettings, load_settings, night_bounds
from leeroy_mind.nightly.store import run_begin
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.realm import night
from leeroy_mind.realm.service import attach_realm
from leeroy_mind.testing.fake_llm import FakeLLM
from nightly.conftest import UTC, FakeAdmin, scripted
from realm.fakes import RealmApp, enabled
from test_realm_ledger import VECTOR, Embed, forum_rumor, ledger, persona, record
from test_realm_provenance import memory as stored_memory
from test_realm_live_proof import HUMAN, insert, roster  # noqa: F401 (the roster fixture)

NIGHT = "2026-10-29"
GUILD, GUILD_NAME = 50, "Testguild"   # His guild; 60, 80 and 90 are the rivals.
_TITLE = re.compile(r'"title":\s*"([^"]+)"')
ANSWERS = {
    "realm_beat": {"lines": ["LF2M for ICC 25 tonight, tanks and healers.",
                             "Lich King next week, mark it."]},
    "realm_post": {"text": "Called it, that kill was coming all week."},
    "realm_letter": {"subject": "Long time", "body": "Raids are not the same without you. Come back soon."},
    "realm_writeup": {"text": "Twelve of us fished the fountain for an hour; Belra won it by a single fish."},
}


def answer(text):
    """Every unit's ordinary reply, by its schema's title."""
    return dict(ANSWERS[_TITLE.search(text.rsplit("matching this schema:", 1)[-1]).group(1)])


class Rig:
    """A migrated mind.db (the realm roster: him as guid 10, the test cast, guilds 50 and 60),
    the Realm enabled in mind.toml, a blocklist and his guild's cached name, so every unit
    kind is ready; and NightContexts over it."""

    def __init__(self, store, root):
        self.store, self.root = store, root
        self.settings = load_settings(load_config(root / "missing.toml"), root)

    def ctx(self, replies=None, *, night_=NIGHT, settings=None, embed=None):
        llm = FakeLLM(script=scripted(replies or {}, answer),
                      models=frozenset({self.settings.reflection_model, self.settings.live_model,
                                        self.settings.embedding_model}))
        embedder = embed or Embed()
        return NightContext(
            store=self.store, night=night_, window=night_bounds(night_, UTC),
            settings=settings or self.settings, reasoner=Reasoner(llm, self.settings.reflection_model),
            embedder=embedder, writer=MemoryWriter(self.store, embedder, self.store.clock, human=lambda: HUMAN),
            arbiter=GpuArbiter(FakeAdmin([self.settings.embedding_model]), self.settings),
            clock=self.store.clock, ident=HUMAN, personas=PersonaCache(self.store))

    def count(self, table, where="1"):
        with self.store.transaction():
            return self.store.conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]

    def rows(self, sql, args=()):
        with self.store.transaction():
            return [dict(row) for row in self.store.conn.execute(sql, args)]

    # -- what a night reads --------------------------------------------------------------------

    def facts(self, n):
        """n new public facts with an event origin, today, each a battleground of its own."""
        now = self.store.clock.now()
        self.made = getattr(self, "made", 0) + n
        return [record(self.store, payload={"ts": int(now * 1000) - i * 1000}, received=now, captured=now)
                for i in range(self.made - n, self.made)]

    def rumors(self, n, first=901):
        for i in range(n):
            forum_rumor(self.store, first + i, last_told=1000.0 + i)

    def letters(self, n, sender=101):
        now = self.store.clock.now()
        with self.store.transaction():
            return [insert(self.store, "realm_mail", ts=now, sender=sender + i % 5, kind="absence",
                           subject="", body="", state="awaiting_text") for i in range(n)]

    def writeups(self, n):
        now = self.store.clock.now()
        with self.store.transaction():
            return [insert(self.store, "realm_event", kind="fishing", organiser=101, start=now - 3600,
                           end=now - 600, place="dal_fountain", result='{"winner":102}', state="scored")
                    for _ in range(n)]

    # -- his Board replies ---------------------------------------------------------------------

    def post(self, thread, author=101, kind="member", text="That pull was a mess, still got it."):
        """A forum post already on the board."""
        with self.store.transaction():
            return insert(self.store, "realm_post", ts=self.store.clock.now() - 7200, author=author,
                          author_kind=kind, thread=thread, text=text, fact=None)

    def delivered(self, sender=103, subject="Miss you", body="The guild is quiet without you."):
        """A letter in his mailbox."""
        with self.store.transaction():
            return insert(self.store, "realm_mail", ts=self.store.clock.now() - 86400, sender=sender,
                          kind="absence", subject=subject, body=body, state="sent")

    def reply(self, kind, parent, to, text, ts=None):
        """His reply from the console, waiting for the night."""
        with self.store.transaction():
            return insert(self.store, "board_reply", ts=ts or self.store.clock.now() - 3600, kind=kind,
                          parent=parent, to_guid=to, text=text, state="waiting")

    def replied(self, reply_id):
        (row,) = self.rows("SELECT state, reason, night, fact, answer_post, answer_mail FROM board_reply "
                           "WHERE id=?", (reply_id,))
        return row


@pytest.fixture
def rig(roster, tmp_path):
    enabled(tmp_path, True)
    (tmp_path / "realm").mkdir()
    (tmp_path / "realm" / "blocklist.txt").write_text("blockedword\n", encoding="utf-8")
    with roster.transaction():
        roster.set_meta("realm:guild_name", json.dumps({"guild": GUILD, "name": GUILD_NAME, "at": 1.0}))
        for guild in (80, 90):
            insert(roster, "realm_guild", id=guild, role="gankers", faction="horde",
                   name=f"Rival{guild}", added=1000.0, status="active")
    return Rig(roster, tmp_path)


def by_kind(units):
    out = {}
    for unit in units:
        out.setdefault(unit.split(":", 1)[0], []).append(unit)
    return out


def full_night(rig):
    """3 rivals, 5 forum subjects (2 facts, 3 rumors), 6 letters and 4 write-ups."""
    rig.facts(2)
    rig.rumors(3)
    rig.letters(6)
    rig.writeups(4)


def heavy(monkeypatch, *kinds):
    """Tasks 27 and 28's units, as register_kind gives them: one due of each kind."""
    ran = []

    def make(kind):
        async def unit(ctx, name):
            ran.append(name)
            ctx.skip(night.STAGE, name)
        return lambda ctx: [f"{kind}:1"], unit
    for kind in kinds:
        units, unit = make(kind)
        monkeypatch.setitem(night.HEAVY, kind, (units, unit))
    return ran


def stage(ctx):
    return run(night.stage_realm(ctx))


def one(ctx, unit):
    """One unit as the stage runs it: its failure is the unit's, counted in ctx.failures."""
    return run(night.run_units(ctx, [unit]))


# -- the hook ----------------------------------------------------------------------------------

async def _nothing(ctx):
    return None


def _runner(app):
    runner = NightRunner([Stage("stories", _nothing, "reflection"), Stage("critic", _nothing, "reflection")],
                         lambda *a: None)
    app.night_runner = runner
    return runner


def test_stage_added_after_stories_when_enabled(tmp_path, store, clock, monkeypatch):
    app = RealmApp(tmp_path, store, clock)
    runner, calls = _runner(app), []
    original = runner.add_stage
    monkeypatch.setattr(runner, "add_stage", lambda *a, **k: calls.append((a, k)) or original(*a, **k))
    enabled(tmp_path, True)
    attach_realm(app)
    assert [s.name for s in runner.stages] == ["stories", "realm", "critic"]
    assert runner.stages[1].model == "reflection" and callable(runner.stages[1].fn)
    assert len(calls) == 1 and calls[0][0][0] == "realm" and calls[0][1] == {
        "after": "stories", "model": "reflection"}
    assert "realm" in budget.EXTENSION_STAGES and budget.UNIT_CALLS["realm"] == {"json": 1}
    assert budget.COUNTERS["realm"] is night.count_units  # Registered at import.


def test_no_stage_when_disabled(tmp_path, store, clock, rig):
    app = RealmApp(tmp_path, store, clock)
    runner = _runner(app)
    enabled(tmp_path, False)
    attach_realm(app)
    assert [s.name for s in runner.stages] == ["stories", "critic"]
    rig.letters(3)
    assert night.count_units(rig.ctx()) == 0  # Disabled at plan time: nothing counts.
    enabled(tmp_path, True)
    assert night.count_units(rig.ctx()) == 3 + 3  # Three letters and a beat for each rival.


def test_stage_rechecks_enabled(tmp_path, store, clock, rig):
    app = RealmApp(tmp_path, store, clock)
    runner = _runner(app)
    realm = attach_realm(app)
    added = runner.stages[1]
    assert added.name == "realm"
    rig.letters(3)
    enabled(tmp_path, False)  # Switched off after attach; the Realm itself has not reloaded.
    assert realm.enabled is True
    ctx = rig.ctx()
    run(added.fn(ctx))
    assert ctx.reasoner.calls == 0 and rig.count("realm_mail", "state='awaiting_text'") == 3
    assert rig.count("night_step", "stage='realm'") == 0
    enabled(tmp_path, True)
    realm._set_enabled(False)  # Its live switch off too counts.
    run(added.fn(rig.ctx()))
    assert rig.count("realm_mail", "state='awaiting_text'") == 3
    realm._set_enabled(True)
    run(added.fn(ctx := rig.ctx()))
    assert rig.count("realm_mail", "state='ready'") == 3 and ctx.reasoner.calls == 3 + 3


# -- what a night takes ------------------------------------------------------------------------

def test_units_capped_at_eleven(rig):
    full_night(rig)
    ctx = rig.ctx()
    units = by_kind(night.plan_units(ctx))
    assert {kind: len(names) for kind, names in units.items()} == {
        "beat": 3, "post": 3, "letter": 3, "writeup": 2}
    assert units["beat"] == ["beat:60", "beat:80", "beat:90"]  # Never his own guild.
    assert [u.split(":")[1] for u in units["post"]] == ["fact", "fact", "rumor"]
    assert night.count_units(ctx) == 11 == sum(night.CAPS[k] for k in ("beat", "post", "letter", "writeup"))


def test_counter_weighs_heavy_units(rig, monkeypatch):
    full_night(rig)
    heavy(monkeypatch, "meeting", "applicant")
    ctx = rig.ctx()
    assert night.count_units(ctx) == 11 + 7 + 6 == ctx.settings.realm_units == NightlySettings.realm_units
    with pytest.raises(ValueError):
        night.register_kind("beat", units=lambda ctx: [], run=_nothing)


def test_unready_public_units_wait(rig):
    """Without the blocklist or his guild's name the public units are not taken; letters are."""
    full_night(rig)
    (rig.root / "realm" / "blocklist.txt").unlink()
    assert by_kind(night.plan_units(rig.ctx())).keys() == {"letter"}
    assert night.count_units(rig.ctx()) == 3


def test_degrade_cuts_realm_first(rig, monkeypatch):
    full_night(rig)
    monkeypatch.setattr(budget, "COUNTERS", {"realm": night.count_units,
                                             "coach": lambda c: c.settings.coach_units,
                                             "encounters": lambda c: c.settings.encounters_per_night})
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 100)
    ctx = rig.ctx()
    plan = run(budget.plan_night(ctx, stages=frozenset({"stories", "coach", "realm"})))
    assert plan["steps"][:3] == ["realm", "coach", "encounters"]
    assert ctx.settings.realm_units == 0 and "realm" not in plan["units"]  # The floor is 0.
    assert plan["extensions"] == ["coach", "realm"]
    roomy = rig.ctx()
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 10 ** 6)
    plan = run(budget.plan_night(roomy, stages=frozenset({"stories", "realm"})))
    assert plan["steps"] == [] and plan["units"]["realm"] == 11
    assert plan["estimate_s"] == 11 * 20 + 150 * 40  # The coach is not on this runner.


def test_realm_counter_ignored_when_stage_absent(rig, monkeypatch):
    full_night(rig)
    monkeypatch.setattr(budget, "COUNTERS", {"realm": night.count_units})
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: -1)  # Every step is tried.
    ctx = rig.ctx()
    plan = run(budget.plan_night(ctx, stages=frozenset({"stories"})))
    assert plan["units"] == {} and plan["estimate_s"] == 0 and plan["extensions"] == []
    assert "realm" not in plan["steps"] and ctx.settings.realm_units == 24  # Its step is skipped too.
    assert night.count_units(ctx) == 11  # Counted only where the stage is installed.


def test_cut_order_keeps_the_meeting_last(rig, monkeypatch):
    full_night(rig)
    heavy(monkeypatch, "meeting")
    ctx = rig.ctx()
    ctx.settings = replace(ctx.settings, realm_units=8)
    assert by_kind(night.plan_units(ctx)) == {"meeting": ["meeting:1"], "beat": ["beat:60"]}
    assert night.count_units(ctx) == 8
    ctx.settings = replace(ctx.settings, realm_units=12)  # Write-ups go first, then posts, then letters.
    assert {k: len(v) for k, v in by_kind(night.plan_units(ctx)).items()} == {
        "meeting": 1, "beat": 3, "letter": 2}
    ctx.settings = replace(ctx.settings, realm_units=6)  # The meeting does not fit: the rest still go.
    assert {k: len(v) for k, v in by_kind(night.plan_units(ctx)).items()} == {
        "beat": 3, "letter": 3}


def test_cut_units_wait_for_tomorrow(rig, monkeypatch):
    rig.letters(3)
    monkeypatch.setattr(budget, "COUNTERS", {"realm": night.count_units})
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 10)

    async def stories(ctx):
        return None

    def runner():
        r = NightRunner([Stage("stories", stories, None)], lambda n, w: rig.ctx(night_=n))
        r.add_stage("realm", night.stage_realm, after="stories", model=None)
        return r
    window = night_bounds(NIGHT, UTC)
    run_begin(rig.store, NIGHT, *window, now=rig.store.clock.now())
    run(runner().run(NIGHT, window))
    assert rig.count("realm_mail", "state='awaiting_text'") == 3  # Cut to 0 tonight: still waiting.
    assert rig.count("night_step", "stage='realm'") == 0
    monkeypatch.setattr(budget, "night_budget_s", lambda *args: 10 ** 6)
    tomorrow = "2026-10-30"
    window = night_bounds(tomorrow, UTC)
    run_begin(rig.store, tomorrow, *window, now=rig.store.clock.now())
    run(runner().run(tomorrow, window))
    assert rig.count("realm_mail", "state='ready'") == 3
    assert [r["body"] for r in rig.rows("SELECT body FROM realm_mail")] == [
        ANSWERS["realm_letter"]["body"]] * 3


def test_done_units_hold_the_nights_caps(rig):
    rig.facts(1)
    rig.rumors(5)
    ctx = rig.ctx()
    stage(ctx)
    assert rig.count("realm_post") == 3 and not ctx.failures
    again = rig.ctx()  # A resumed run of the same night: two more rumors wait, the cap is spent.
    assert by_kind(night.plan_units(again))["post"] == by_kind(night.plan_units(ctx))["post"]
    assert night.count_units(again) == 0
    stage(again)
    assert rig.count("realm_post") == 3 and again.reasoner.calls == 0
    tonight = set(by_kind(night.plan_units(ctx))["post"])
    tomorrow = set(by_kind(night.plan_units(rig.ctx(night_="2026-10-30")))["post"])
    assert len(tomorrow) == 3 and tomorrow.isdisjoint(tonight)  # Only subjects never posted.


# -- the units ---------------------------------------------------------------------------------

def test_beats_never_in_known(rig):
    ctx = rig.ctx({"realm_beat": [{"lines": ["LF2M for ICC 25 tonight.", f"{GUILD_NAME} are slow."]}]},
                  settings=replace(rig.settings, realm_units=1))
    stage(ctx)
    assert not ctx.failures and ctx.reasoner.calls == 1
    with rig.store.transaction():
        lines = night.beat_lines_in_unit(rig.store, 60, human=HUMAN)
        assert night.beat_lines_in_unit(rig.store, 80, human=HUMAN) == ()
    # The line naming his guild was dropped; the other is a private fact guild 60 alone knows.
    assert [(f.kind, f.text, f.source, f.public) for f in lines] == [
        ("beat", "LF2M for ICC 25 tonight.", "night", False)]
    assert rig.rows("SELECT guild, how FROM realm_knows") == [{"guild": 60, "how": "own"}]
    facts = ledger(rig.store)
    assert facts.known(60) == ()
    assert [row["id"] for row in facts.known_all(60)] == [lines[0].id]
    with rig.store.transaction():
        assert night.beat_lines_in_unit(rig.store, 60, human=HUMAN, night=NIGHT) == lines
        assert night.beat_lines_in_unit(rig.store, 60, human=HUMAN, night="2026-10-28") == ()


def test_night_memories_inside_the_commit(rig, monkeypatch):
    for guid in (101, 102):
        persona(rig.store, guid, "Arkon" if guid == 101 else "Belra", motivation="competitor")
    (fact,) = rig.facts(1)
    seen = []
    original = MemoryWriter.put

    def put(self, row, **kw):
        seen.append((rig.store.in_unit, rig.store.conn.execute(
            "SELECT count(*) FROM night_step WHERE stage='realm'").fetchone()[0], row.owner))
        return original(self, row, **kw)
    monkeypatch.setattr(MemoryWriter, "put", put)
    embed = Embed()
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0), embed=embed)
    unit = f"post:fact:{fact.id}"
    one(ctx, unit)
    assert not ctx.failures and ctx.done(night.STAGE, unit)
    # Every way-back-in memory was written inside the unit, before its night_step row.
    assert seen == [(True, 0, 101), (True, 0, 102)]
    assert embed.calls == [(fact.text,)]  # Embedded once, before the commit.
    memories = rig.rows("SELECT owner, text, evidence, scope, importance, embedding FROM memory ORDER BY id")
    assert [(m["owner"], m["text"], json.loads(m["evidence"]), m["scope"], m["importance"]) for m in memories] == [
        (guid, fact.text, [f"realm:fact:{fact.id}"], "open", 4) for guid in (101, 102)]
    assert {m["embedding"] for m in memories} == {VECTOR}
    (post,) = rig.rows("SELECT author, author_kind, thread, text, fact FROM realm_post")
    assert post["thread"] == f"fact:{fact.id}" and post["text"] == ANSWERS["realm_post"]["text"]
    # A competitive member, or the rival officer (actor 201 of guild 60).
    assert (post["author_kind"], post["author"]) in {("member", 101), ("member", 102), ("actor", 201)}
    (origin,) = rig.rows("SELECT fact, policy, producer_key FROM realm_rule_origin")
    assert origin == {"fact": post["fact"], "policy": "forum_post_v1",
                      "producer_key": f"rule:forum_post_v1:fact:{fact.id}"}
    assert rig.rows("SELECT guild, how FROM realm_knows WHERE fact=? ORDER BY guild", (post["fact"],)) == [
        {"guild": g, "how": "forum"} for g in (50, 60, 80, 90)]
    assert rig.count("realm_notable") == 2

    # A commit that raises stores no memory and no post.
    with rig.store.transaction():
        rig.store.conn.execute("DELETE FROM realm_notable")  # Free every slot for the second fact.
        rig.store.conn.execute("CREATE TRIGGER no_step BEFORE INSERT ON night_step "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic commit failure'); END")
    (second,) = rig.facts(1)
    before = rig.count("memory"), rig.count("realm_post"), rig.count("realm_rule_origin")
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    unit = f"post:fact:{second.id}"
    one(ctx, unit)
    assert ctx.failures[night.STAGE] == 1 and not ctx.done(night.STAGE, unit)
    assert (rig.count("memory"), rig.count("realm_post"), rig.count("realm_rule_origin")) == before
    assert rig.count("realm_notable") == 0


def test_rumor_posts_write_no_memory(rig):
    persona(rig.store, 101, "Arkon", motivation="competitor")
    rig.rumors(1)
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=4))  # The rumor's post, after 3 beats.
    stage(ctx)
    assert not ctx.failures
    (post,) = rig.rows("SELECT thread, author, fact FROM realm_post")
    assert post["thread"] == "rumor:901" and post["author"] in (101, 201)  # Arkon, or a rival officer.
    assert rig.count("memory", "id != 901") == 0 and rig.count("realm_notable") == 0
    assert [f.id for f in ledger(rig.store).known(60)] == [post["fact"]]  # A public post every guild reads.


def test_unit_output_screened(rig):
    persona(rig.store, 101, "Arkon", motivation="competitor")
    (fact,) = rig.facts(1)
    (mail,) = rig.letters(1)
    bad_post = {"realm_post": [{"text": f"{GUILD_NAME} finally got one."}]}
    ctx = rig.ctx(bad_post, settings=replace(rig.settings, realm_units=0))
    one(ctx, f"post:fact:{fact.id}")
    assert ctx.failures[night.STAGE] == 1 and not ctx.done(night.STAGE, f"post:fact:{fact.id}")
    assert (rig.count("realm_post"), rig.count("realm_rule_origin"), rig.count("memory"),
            rig.count("realm_notable")) == (0, 0, 0, 0)
    named = {"realm_letter": [{"subject": "hey", "body": f"Hope you are well, {HUMAN.real_name}."}]}
    ctx = rig.ctx(named, settings=replace(rig.settings, realm_units=0))
    one(ctx, f"letter:{mail}")
    assert ctx.failures[night.STAGE] == 1
    assert rig.rows("SELECT state, body FROM realm_mail") == [{"state": "awaiting_text", "body": ""}]
    # Tomorrow the same post is tried again.
    assert f"post:fact:{fact.id}" in night.plan_units(rig.ctx(night_="2026-10-30"))


def test_a_letter_text_is_private(rig):
    (mail,) = rig.letters(1, sender=103)
    with rig.store.transaction():  # The sender's own memory of the letter, as the mail desk keeps it.
        mid = stored_memory(rig.store, 777, owner=103, text="I am writing him a letter.",
                            actors="[10]", scope="private", scope_with=HUMAN.guid)
    before = rig.rows("SELECT * FROM memory")
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    one(ctx, f"letter:{mail}")
    assert not ctx.failures
    assert rig.rows("SELECT subject, body, state, command_id FROM realm_mail") == [
        {"subject": "Long time", "body": ANSWERS["realm_letter"]["body"], "state": "ready",
         "command_id": None}]
    # The text lives only in the mail: no memory, fact or post, and hers stays private with him.
    assert rig.rows("SELECT * FROM memory") == before
    assert rig.rows("SELECT scope, scope_with FROM memory WHERE id=?", (mid,)) == [
        {"scope": "private", "scope_with": HUMAN.guid}]
    assert (rig.count("realm_fact"), rig.count("realm_post"), rig.count("realm_knows")) == (0, 0, 0)
    prompt = json.dumps(ctx.reasoner.llm.calls)
    assert HUMAN.real_name not in prompt and HUMAN.called in prompt


def test_realm_switched_off_mid_unit_writes_nothing(rig):
    (mail,) = rig.letters(1)
    on = [True]

    def reply(prompt):  # The Realm goes off while the model writes.
        on[0] = False
        return dict(ANSWERS["realm_letter"])
    ctx = rig.ctx({"realm_letter": [reply]})
    run(night.run_units(ctx, [f"letter:{mail}"], live=lambda: on[0]))
    assert ctx.reasoner.calls == 1 and not ctx.failures and not ctx.done(night.STAGE, f"letter:{mail}")
    assert rig.rows("SELECT state, body FROM realm_mail") == [{"state": "awaiting_text", "body": ""}]


def test_writeup_is_kept_with_its_event(rig):
    (event,) = rig.writeups(1)
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    one(ctx, f"writeup:{event}")
    assert not ctx.failures
    (row,) = rig.rows("SELECT result FROM realm_event")
    # Repaired before the screen, as the live pipeline does (a semicolon is a tell).
    assert json.loads(row["result"]) == {"winner": 102, "writeup": ANSWERS["realm_writeup"]["text"].replace("; ", ". ")}
    assert night.plan_units(rig.ctx(night_="2026-10-30")) == ["beat:60", "beat:80", "beat:90"]


def test_whole_night_through_the_runner(rig, monkeypatch):
    """A full night: every kind once, the prompts carry no real name and never his guild's."""
    full_night(rig)
    for guid in (101, 102):
        persona(rig.store, guid, "Arkon" if guid == 101 else "Belra", motivation="competitor")
    monkeypatch.setattr(budget, "COUNTERS", {"realm": night.count_units})
    seen = []

    def make(n, w):
        ctx = rig.ctx(night_=n)
        seen.append(ctx)
        return ctx
    runner = NightRunner([Stage("stories", _nothing, "reflection")], make)
    runner.add_stage("realm", night.attach(type("R", (), {"enabled": True})()), after="stories")
    window = night_bounds(NIGHT, UTC)
    run_begin(rig.store, NIGHT, *window, now=rig.store.clock.now())
    run(runner.run(NIGHT, window))
    ctx = seen[-1]
    assert not ctx.failures and ctx.plan["units"] == {"realm": 11}
    assert rig.count("night_step", "stage='realm'") == 11 and ctx.reasoner.calls == 11
    assert (rig.count("realm_post"), rig.count("realm_mail", "state='ready'"),
            rig.count("realm_rule_origin", "policy='night_beat_v1'")) == (3, 3, 6)
    prompts = json.dumps([c["messages"] for c in ctx.reasoner.llm.calls if "messages" in c])
    assert HUMAN.real_name not in prompts and GUILD_NAME not in prompts
    assert rig.store.conn.execute("SELECT count(*) FROM night_stage WHERE stage='realm'").fetchone()[0] == 1


# -- his Board replies ---------------------------------------------------------------------

def test_his_replies_come_first_inside_the_caps(rig):
    parents = [rig.post(f"rumor:{900 + i}") for i in range(4)]
    stamps = (1003.0, 1001.0, 1000.0, 1002.0)  # Oldest first: the 3rd, the 2nd, then the 4th.
    replies = [rig.reply("post", parent, 101, f"Fair point number {i}.", ts=stamps[i])
               for i, parent in enumerate(parents)]
    facts = rig.facts(2)
    letter_reply = rig.reply("letter", rig.delivered(), 103, "Thanks, see you soon.")
    letters = rig.letters(3)
    ctx = rig.ctx()
    units = by_kind(night.plan_units(ctx))
    assert units["post"] == [f"post:reply:{replies[i]}" for i in (2, 1, 3)]
    assert units["letter"] == [f"letter:reply:{letter_reply}", f"letter:{letters[0]}", f"letter:{letters[1]}"]
    assert night.count_units(ctx) == 3 + 3 + 3  # Beats, posts and letters: each weighs 1.
    with rig.store.transaction():  # Withdrawn: tonight's own candidates fill the rest.
        for i in (1, 2, 3):
            board.settle(rig.store.conn, replies[i], state="withdrawn", night=None, now=1.0)
    assert by_kind(night.plan_units(rig.ctx()))["post"] == [
        f"post:reply:{replies[0]}", *(f"post:fact:{f.id}" for f in facts)]


def _fact(rig, key):
    (row,) = rig.rows("SELECT f.id, f.kind, f.text, f.public, o.inputs_json FROM realm_rule_origin o "
                      "JOIN realm_fact f ON f.id=o.fact WHERE o.producer_key=?", (f"rule:forum_post_v1:{key}",))
    knowers = rig.rows("SELECT guild, how FROM realm_knows WHERE fact=? ORDER BY guild", (row["id"],))
    return row | {"inputs": json.loads(row.pop("inputs_json")), "knows": knowers}


def _asked(ctx):
    """The one model call's system prompt and its question."""
    (call,) = [c for c in ctx.reasoner.llm.calls if "messages" in c]
    system, user = (m["content"] for m in call["messages"])
    assert HUMAN.real_name not in json.dumps(call)
    return system, json.loads(user.split("\n\nReply with only a JSON object", 1)[0])


def test_a_forum_reply_becomes_a_forum_fact_and_its_author_answers(rig):
    parent = rig.post("rumor:901", author=101)
    rid = rig.reply("post", parent, 101, "Agreed, that pull was clean.")
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    one(ctx, f"post:reply:{rid}")
    assert not ctx.failures and ctx.done(night.STAGE, f"post:reply:{rid}") and ctx.reasoner.calls == 1
    everyone = [{"guild": g, "how": "forum"} for g in (50, 60, 80, 90)]
    his = _fact(rig, f"reply:{rid}")
    assert (his["kind"], his["text"], his["public"], his["knows"]) == (
        "forum_post", "Agreed, that pull was clean.", 1, everyone)
    assert his["inputs"] == {"night": NIGHT, "reply": rid, "to": parent, "author": HUMAN.guid,
                             "author_kind": "owner"}
    answer = _fact(rig, f"reply:{rid}:answer")
    assert (answer["kind"], answer["text"], answer["public"], answer["knows"]) == (
        "forum_post", ANSWERS["realm_post"]["text"], 1, everyone)
    (post,) = rig.rows("SELECT id, author, author_kind, thread, text, fact FROM realm_post WHERE id!=?", (parent,))
    assert post == {"id": post["id"], "author": 101, "author_kind": "member", "thread": "rumor:901",
                    "text": ANSWERS["realm_post"]["text"], "fact": answer["id"]}
    assert rig.replied(rid) == {"state": "answered", "reason": None, "night": NIGHT, "fact": his["id"],
                                "answer_post": post["id"], "answer_mail": None}
    assert rig.count("memory") == 0  # A rumor's thread: no way back in.
    system, asked = _asked(ctx)
    assert "replied to it" in system and "Arkon" in system
    assert asked["author"] == "Arkon" and asked["reply"] == "Agreed, that pull was clean."
    assert asked["post"] == "That pull was a mess, still got it." and "card" in asked


def test_a_reply_in_an_event_thread_writes_memories_as_posts_do(rig):
    for guid in (101, 102):
        persona(rig.store, guid, "Arkon" if guid == 101 else "Belra", motivation="competitor")
    (fact,) = rig.facts(1)
    parent = rig.post(f"fact:{fact.id}", author=201, kind="actor")
    rid = rig.reply("post", parent, 201, "We had it on the second pull.")
    embed = Embed()
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0), embed=embed)
    one(ctx, f"post:reply:{rid}")
    assert not ctx.failures
    (post,) = rig.rows("SELECT author, author_kind, thread FROM realm_post WHERE id!=?", (parent,))
    assert post == {"author": 201, "author_kind": "actor", "thread": f"fact:{fact.id}"}  # The parent's author.
    assert embed.calls == [(fact.text,)]  # Embedded once, before the commit.
    memories = rig.rows("SELECT owner, text, evidence, scope FROM memory ORDER BY id")
    assert [(m["owner"], m["text"], json.loads(m["evidence"]), m["scope"]) for m in memories] == [
        (guid, fact.text, [f"realm:fact:{fact.id}"], "open") for guid in (101, 102)]
    assert rig.count("realm_notable") == 2 and rig.replied(rid)["state"] == "answered"


def test_an_author_who_cannot_write_is_answered_from_the_pool(rig):
    parent = rig.post("rumor:901", author=202, kind="actor")  # A retired officer can no longer write.
    rid = rig.reply("post", parent, 202, "Nobody is reading this anyway.")
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    one(ctx, f"post:reply:{rid}")
    assert not ctx.failures and ctx.reasoner.calls == 1
    (post,) = rig.rows("SELECT author, author_kind, thread FROM realm_post WHERE id!=?", (parent,))
    assert post == {"author": 201, "author_kind": "actor", "thread": "rumor:901"}  # The only one in the pool.
    assert rig.replied(rid)["state"] == "answered"


def test_nobody_to_answer_leaves_his_post_unanswered(rig):
    parent = rig.post("rumor:901", author=202, kind="actor")  # A retired officer can no longer write.
    with rig.store.transaction():
        rig.store.conn.execute("UPDATE realm_actor SET status='retired' WHERE guid=201")
    rid = rig.reply("post", parent, 202, "Nobody is reading this anyway.")
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    one(ctx, f"post:reply:{rid}")
    assert not ctx.failures and ctx.reasoner.calls == 0
    his = _fact(rig, f"reply:{rid}")
    assert his["text"] == "Nobody is reading this anyway." and len(his["knows"]) == 4
    assert rig.replied(rid) == {"state": "posted", "reason": None, "night": NIGHT, "fact": his["id"],
                                "answer_post": None, "answer_mail": None}
    assert rig.count("realm_post") == 1 and rig.count("realm_rule_origin") == 1


def test_a_letter_reply_is_remembered_privately_and_answered_by_letter(rig):
    rid = rig.reply("letter", rig.delivered(sender=103), 103, "Back on Thursday, save me a spot.")
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    one(ctx, f"letter:reply:{rid}")
    assert not ctx.failures and ctx.reasoner.calls == 1
    (memory,) = rig.rows("SELECT owner, kind, text, key, evidence, scope, scope_with, importance, valence, "
                         "actors FROM memory")
    assert memory == {"owner": 103, "kind": "event",
                      "text": f'{HUMAN.called} wrote back to my letter: "Back on Thursday, save me a spot."',
                      "key": f"board-reply:{rid}", "evidence": json.dumps([f"board-reply:{rid}"]),
                      "scope": "private", "scope_with": HUMAN.guid, "importance": 5, "valence": 0.4,
                      "actors": json.dumps([HUMAN.guid])}
    (answer,) = rig.rows("SELECT id, sender, kind, subject, body, items, state FROM realm_mail WHERE kind='reply'")
    assert answer == {"id": answer["id"], "sender": 103, "kind": "reply", "subject": "Long time",
                      "body": ANSWERS["realm_letter"]["body"], "items": "[]", "state": "ready"}
    assert rig.replied(rid) == {"state": "answered", "reason": None, "night": NIGHT, "fact": None,
                                "answer_post": None, "answer_mail": answer["id"]}
    assert (rig.count("realm_fact"), rig.count("realm_post"), rig.count("realm_knows")) == (0, 0, 0)
    system, asked = _asked(ctx)
    assert "wrote back" in system and "Coros" in system
    assert asked["their_letter"] == {"subject": "Miss you", "body": "The guild is quiet without you."}
    assert asked["reply"] == "Back on Thursday, save me a spot." and asked["from"] == "Coros"
    # A long reply is cut to fit a memory, its closing quote kept.
    long = rig.reply("letter", rig.delivered(sender=104), 104, " ".join(["Thursday works for me."] * 17))
    one(ctx, f"letter:reply:{long}")
    (text,) = [r["text"] for r in rig.rows("SELECT text FROM memory WHERE owner=104")]
    assert len(text) <= 200 and text.endswith('"') and "Thursday works for me." in text


def test_a_refused_reply_is_marked_and_not_retried(rig):
    named = rig.reply("post", rig.post("rumor:901"), 101, f"{GUILD_NAME} wiped again lol")
    real = rig.reply("letter", rig.delivered(sender=103), 103, f"It is {HUMAN.real_name}, back soon.")
    nobody = rig.reply("letter", rig.delivered(sender=106), 106, "See you around.")  # No persona.
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    units = [f"post:reply:{named}", f"letter:reply:{real}", f"letter:reply:{nobody}"]
    run(night.run_units(ctx, units))
    assert not ctx.failures and ctx.reasoner.calls == 0 and all(ctx.done(night.STAGE, u) for u in units)
    assert [(r["state"], r["reason"]) for r in map(rig.replied, (named, real, nobody))] == [
        ("refused", "guild_name"), ("refused", "real_name"), ("refused", "no_persona")]
    assert (rig.count("realm_rule_origin"), rig.count("memory"), rig.count("realm_mail", "kind='reply'")) == (0, 0, 0)
    with rig.store.transaction():
        assert board.waiting(rig.store.conn, "post", 9) == [] == board.waiting(rig.store.conn, "letter", 9)
    assert not set(units) & set(night.plan_units(rig.ctx(night_="2026-10-30")))


def test_a_reply_his_forum_fact_would_refuse_is_refused_before_the_model(rig):
    marks = rig.reply("post", rig.post("rumor:901", author=101), 101, "|cff00ff00|r")  # Markup only.
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    assert night._Night(ctx)._public("|cff00ff00|r", "member", night.POST_MAX) is None  # The forum screen passes it.
    one(ctx, f"post:reply:{marks}")
    assert not ctx.failures and ctx.reasoner.calls == 0 and ctx.done(night.STAGE, f"post:reply:{marks}")
    assert (rig.replied(marks)["state"], rig.replied(marks)["reason"]) == ("refused", "unsupported_text")
    assert (rig.count("realm_post"), rig.count("realm_rule_origin"), rig.count("realm_fact")) == (1, 0, 0)


def test_a_refused_answer_waits_for_another_night(rig):
    rid = rig.reply("post", rig.post("rumor:901", author=101), 101, "Agreed, that pull was clean.")
    lid = rig.reply("letter", rig.delivered(sender=103), 103, "Back on Thursday.")
    bad = {"realm_post": [{"text": f"{GUILD_NAME} should stay out of it."}],
           "realm_letter": [{"subject": "hey", "body": f"Good to hear, {HUMAN.real_name}."}]}
    ctx = rig.ctx(bad, settings=replace(rig.settings, realm_units=0))
    units = [f"post:reply:{rid}", f"letter:reply:{lid}"]
    run(night.run_units(ctx, units))
    assert ctx.failures[night.STAGE] == 2 and ctx.reasoner.calls == 2
    assert not any(ctx.done(night.STAGE, u) for u in units)
    assert rig.replied(rid)["state"] == rig.replied(lid)["state"] == "waiting"
    assert (rig.count("realm_post"), rig.count("realm_rule_origin"), rig.count("memory"),
            rig.count("realm_mail", "kind='reply'")) == (1, 0, 0, 0)
    assert set(units) <= set(night.plan_units(rig.ctx(night_="2026-10-30")))


REFUSED_ANSWER = {"text": f"{GUILD_NAME} should stay out of it."}  # the forum screen refuses it
BROKEN_ANSWER = {"subject": "", "body": ""}  # does not fit the schema: the model gave no answer


def test_an_answer_that_fails_three_nights_settles_his_reply_unanswered(rig):
    rid = rig.reply("post", rig.post("rumor:901", author=101), 101, "Agreed, that pull was clean.")
    lid = rig.reply("letter", rig.delivered(sender=103), 103, "Back on Thursday.")
    units = [f"post:reply:{rid}", f"letter:reply:{lid}"]
    settings = replace(rig.settings, realm_units=0)

    def bad_night(night_):
        ctx = rig.ctx({"realm_post": [REFUSED_ANSWER], "realm_letter": [BROKEN_ANSWER] * 2},
                      night_=night_, settings=settings)
        run(night.run_units(ctx, units))
        return ctx
    for night_ in ("2026-10-29", "2026-10-30"):
        for _ in range(2):  # a night resumed after his login counts once
            ctx = bad_night(night_)
            assert ctx.failures[night.STAGE] == 2 and not any(ctx.done(night.STAGE, u) for u in units)
        assert rig.replied(rid)["state"] == rig.replied(lid)["state"] == "waiting"
    ctx = bad_night("2026-10-31")
    assert not ctx.failures and all(ctx.done(night.STAGE, u) for u in units)
    assert [(r["state"], r["reason"], r["night"]) for r in map(rig.replied, (rid, lid))] == [
        ("refused", "no_answer", "2026-10-31")] * 2
    assert (rig.count("realm_post"), rig.count("realm_rule_origin"), rig.count("memory"),
            rig.count("realm_mail", "kind='reply'")) == (1, 0, 0, 0)
    assert rig.count("meta", "key GLOB 'board:reply:*'") == 0
    assert not set(units) & set(night.plan_units(rig.ctx(night_="2026-11-01")))


def test_an_answer_after_a_failed_night_forgets_it(rig):
    rid = rig.reply("post", rig.post("rumor:901", author=101), 101, "Agreed, that pull was clean.")
    settings = replace(rig.settings, realm_units=0)
    one(rig.ctx({"realm_post": [REFUSED_ANSWER]}, settings=settings), f"post:reply:{rid}")
    assert rig.count("meta", "key GLOB 'board:reply:*'") == 1
    ctx = rig.ctx(night_="2026-10-30", settings=settings)
    one(ctx, f"post:reply:{rid}")
    assert not ctx.failures and rig.replied(rid)["state"] == "answered"
    assert rig.count("meta", "key GLOB 'board:reply:*'") == 0


def test_world_author_lapsing_after_model_waits_at_most_three_nights(rig,monkeypatch):
    rid=rig.reply("post",rig.post("rumor:901",author=201,kind="actor"),201,"That was a clean pull.")
    unit=f"post:reply:{rid}"
    live={"author":True}
    author=(201,"actor","Fixture",{"who":"a member of Test rivals","card":"Synthetic voice observations"})
    monkeypatch.setattr(night._Night,"_still_writes",lambda self,guid,kind:author if live["author"] else None)
    def lapse(prompt):
        live["author"]=False
        return dict(ANSWERS["realm_post"])
    for day in (29,30,31):
        live["author"]=True
        ctx=rig.ctx({"realm_post":[lapse]},night_=f"2026-10-{day}",settings=replace(rig.settings,realm_units=0))
        one(ctx,unit)
        assert rig.replied(rid)["state"] == ("waiting" if day < 31 else "refused")
        if day < 31:
            assert json.loads(rig.store.get_meta(f"board:reply:{rid}:failed")) == [f"2026-10-{n}" for n in range(29,day+1)]
    assert rig.replied(rid)["reason"] == "no_answer"
    assert rig.count("realm_post") == 1 and rig.count("realm_rule_origin") == 0


def test_an_answer_its_forum_fact_would_refuse_is_a_failed_night(rig):
    rid = rig.reply("post", rig.post("rumor:901", author=101), 101, "Agreed, that pull was clean.")
    ctx = rig.ctx({"realm_post": [{"text": "|cff00ff00|r"}]}, settings=replace(rig.settings, realm_units=0))
    assert night._Night(ctx)._public("|cff00ff00|r", "member", night.POST_MAX) is None  # The forum screen passes it.
    one(ctx, f"post:reply:{rid}")
    assert ctx.failures[night.STAGE] == 1 and rig.replied(rid)["state"] == "waiting"
    assert (rig.count("realm_post"), rig.count("realm_rule_origin")) == (1, 0)
    assert json.loads(rig.store.get_meta(f"board:reply:{rid}:failed")) == [NIGHT]


def test_a_reply_withdrawn_before_the_commit_is_left_alone(rig):
    rid = rig.reply("post", rig.post("rumor:901", author=101), 101, "Agreed, that pull was clean.")
    lid = rig.reply("letter", rig.delivered(sender=103), 103, "Back on Thursday.")

    def withdraw(reply_id, title):
        def answer_(prompt):  # He withdraws it from the console while the model writes.
            with rig.store.transaction():
                assert board.settle(rig.store.conn, reply_id, state="withdrawn", night=None, now=5.0)
            return dict(ANSWERS[title])
        return answer_
    ctx = rig.ctx({"realm_post": [withdraw(rid, "realm_post")], "realm_letter": [withdraw(lid, "realm_letter")]},
                  settings=replace(rig.settings, realm_units=0))
    run(night.run_units(ctx, [f"post:reply:{rid}", f"letter:reply:{lid}"]))
    assert not ctx.failures and ctx.reasoner.calls == 2
    assert rig.replied(rid) == {"state": "withdrawn", "reason": None, "night": None, "fact": None,
                                "answer_post": None, "answer_mail": None}
    assert rig.replied(lid)["state"] == "withdrawn"
    assert (rig.count("realm_post"), rig.count("realm_rule_origin"), rig.count("realm_knows"),
            rig.count("memory"), rig.count("realm_mail", "kind='reply'")) == (1, 0, 0, 0, 0)


def test_the_same_reply_is_never_answered_twice(rig):
    rid = rig.reply("post", rig.post("rumor:901", author=101), 101, "Agreed, that pull was clean.")
    lid = rig.reply("letter", rig.delivered(sender=103), 103, "Back on Thursday.")
    units = [f"post:reply:{rid}", f"letter:reply:{lid}"]
    ctx = rig.ctx(settings=replace(rig.settings, realm_units=0))
    for _ in range(2):
        run(night.run_units(ctx, units))
    again = rig.ctx(night_="2026-10-30", settings=replace(rig.settings, realm_units=0))
    run(night.run_units(again, units))  # Asked again on another night: already settled.
    assert not ctx.failures and not again.failures
    assert ctx.reasoner.calls == 2 and again.reasoner.calls == 0
    assert all(again.done(night.STAGE, u) for u in units)
    assert (rig.count("realm_post"), rig.count("realm_rule_origin"), rig.count("memory"),
            rig.count("realm_mail", "kind='reply'")) == (2, 2, 1, 1)
    assert rig.replied(rid)["night"] == rig.replied(lid)["night"] == NIGHT


def test_replies_wait_while_the_realm_is_off(rig):
    rid = rig.reply("post", rig.post("rumor:901", author=101), 101, "Agreed, that pull was clean.")
    lid = rig.reply("letter", rig.delivered(sender=103), 103, "Back on Thursday.")
    enabled(rig.root, False)
    ctx = rig.ctx()
    assert night.count_units(ctx) == 0
    stage(ctx)
    enabled(rig.root, True)
    run(night.stage_realm(off := rig.ctx(), live=lambda: False))  # Its own switch off counts too.
    assert ctx.reasoner.calls == 0 == off.reasoner.calls
    assert rig.replied(rid)["state"] == rig.replied(lid)["state"] == "waiting"
    (rig.root / "realm" / "blocklist.txt").unlink()  # The screen not ready: his forum reply waits.
    stage(ctx := rig.ctx())
    assert rig.replied(rid)["state"] == "waiting" and rig.replied(lid)["state"] == "answered"
    assert f"post:reply:{rid}" not in night.plan_units(ctx)
