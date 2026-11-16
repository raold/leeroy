"""The guild stops orbiting him (owner's "pass the Turing test" push, 2026-11-15: 82% of the
members' typed game lines were aimed at him, every login drew 2-5 greeters, "there he is" was
said 23 times, five members asked how his day was within two hours):

- the openers' clock runs at 2 an hour, and only [talk] openers_to_him of them go to him; the
  rest start a conversation among the members about the guild's own business
  (talk/guild_topics.py), and no opener to him is an interview question;
- 0-2 members greet his login (none about 30% of the time), a later one may type a plain hello,
  and a greeting phrase the guild used lately rests (talk/greetings.py);
- a woman reads that she may flirt with him only on a turn that is his, a man that the women only
  have eyes for him only on a turn that involves him (prompt.FLIRT_RULE_AWAY)."""
import random
import re
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from conftest import HUMAN, make_persona, run
from engine_rig import engine_clock, make_rig, numbered, save_member
from leeroy_mind.config import ConfigError, load_config
from leeroy_mind.human import HumanIdentity
from leeroy_mind.protocol import Login, Logout, Zone
from leeroy_mind.server.seeds import Seed
from leeroy_mind.talk import greetings, guild_topics
from leeroy_mind.talk.engine import TO_HIM_TOPICS
from leeroy_mind.talk.prompt import FLIRT_RULE, FLIRT_RULE_AWAY, comms_rules
from leeroy_mind.talk.reactions import LOGIN_QUIET_S
from leeroy_mind.talk.turns import Turn, TurnKind

ZAN = HumanIdentity(guid=1, character_name="Testmage", nickname="Zan", real_name="Quillon",
                    is_guild_master=True, pronouns="he")


@pytest.fixture
def clock():
    return engine_clock()


async def play(r, seconds, *, step=4.0):
    """He plays: a tick every `step`, his zone now and then so he never reads as idle."""
    end, i = r.clock.now() + seconds, 0
    while r.clock.now() < end:
        await r.advance(step)
        if i % 45 == 0:
            r.world.apply(Zone(ts=0, guid=HUMAN, zone_id=10, zone_name="Dalaran"), r.clock.now())
        await r.engine.tick(r.clock.now())
        i += 1


def opened(r) -> list[tuple[int, str, str, str, int]]:
    """(guid, kind, channel, why, to_guid) of every opener the clock started, said."""
    return [(u["guid"], u["kind"], u["channel"], u["why"], u["to_guid"]) for u in r.store.conn.execute(
        "SELECT guid, kind, channel, why, to_guid FROM utterance WHERE dropped IS NULL AND "
        "(why = 'burst:to_him' OR why LIKE 'burst:guild:%') ORDER BY id")]


def share(r, value: float) -> None:
    cfg = r.engine.cfg
    r.engine.apply_config(replace(cfg, talk=replace(cfg.talk, openers_to_him=value)))


# -- mind.toml -------------------------------------------------------------------------------

def test_the_openers_default_to_two_an_hour_and_a_share_to_him(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert (cfg.talk.openers_per_hour, cfg.talk.openers_to_him) == (2, 0.35)
    (tmp_path / "ok.toml").write_text("[talk]\nopeners_to_him = 1\n", encoding="utf-8")
    assert load_config(tmp_path / "ok.toml").talk.openers_to_him == 1
    for bad in ("-0.1", "1.5", '"half"', "true"):
        (tmp_path / "bad.toml").write_text(f"[talk]\nopeners_to_him = {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=re.escape("talk.openers_to_him")):
            load_config(tmp_path / "bad.toml")


# -- the openers ----------------------------------------------------------------------------

def test_no_opener_to_him_is_an_interview_question(store, clock, tmp_path):
    assert not any("day went" in t or "up to" in t for t in TO_HIM_TOPICS)
    r = make_rig(store, clock, tmp_path)
    topics = {r.engine._to_him_topic(r.engine.personas.get(g), clock.now())
              for g in (101, 102, 103) for _ in range(12)}
    assert topics and not any(re.search(r"how (his|your) day|what he's up to|up to tonight", t)
                              for t in topics)


def guild_business(store, now):
    """Something for the guild to talk about: last night's raid, and Arkon's life beat."""
    store.conn.execute(
        "INSERT INTO guild_raid(raid_id,started,ended,raid_key,size,heroic,offline,lead_guid,"
        "tank_guid,loot_guid,kills,wipes) VALUES (1,?,?,'icc',10,0,0,102,101,104,4,2)",
        (now - 7200, now - 3600))
    store.conn.execute("INSERT INTO life_thread(id,guid,status) VALUES (1,101,'active')")
    store.conn.execute("INSERT INTO life_beat(thread_id,night,text,ts) VALUES "
                       "(1,'2026-10-29','my sourdough starter finally did something',?)",
                       (now - 600,))
    store.conn.commit()


@pytest.mark.parametrize("to_him", [0.35, 0.0, 1.0])
def test_openers_go_to_him_only_a_share_of_the_time(store, clock, tmp_path, to_him):
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"), seed=3)
        guild_business(store, clock.now())
        r.engine.bits.p_fire = 0.0
        r.engine.whispers.set_rate(0.0)
        r.engine.budget.set_to_him_rate(12.0)
        share(r, to_him)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await play(r, 4 * 3600, step=6.0)
        await r.engine.end_session()
        lines = opened(r)
        him = [x for x in lines if x[3] == "burst:to_him"]
        guild = [x for x in lines if x[3].startswith("burst:guild:")]
        assert lines
        assert all(x[2] == "guild" and x[1] == "open" for x in guild)
        assert all(x[4] == 0 for x in guild)  # nobody's addressed: the guild talks among itself
        if to_him == 0.0:
            assert not him and guild
        elif to_him == 1.0:
            assert him and not guild
        else:
            assert guild and him and len(him) < len(guild)
    run(main())


def test_a_guild_opener_starts_a_conversation_among_the_members(store, clock, tmp_path):
    """Its topic is guild business (the prompt brings it up), others answer it as a social
    burst does, and a member's own life beat is brought up only by that member."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("thought {n} here"), seed=5)
        guild_business(store, clock.now())
        r.engine.bits.p_fire = 0.0
        r.engine.whispers.set_rate(0.0)
        r.engine.budget.set_to_him_rate(12.0)
        share(r, 0.0)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await play(r, 3 * 3600, step=6.0)
        await r.engine.end_session()
        subjects = Counter(why.split(":")[2] for _, _, _, why, _ in opened(r))
        assert len(subjects) >= 2 and set(subjects) <= set(guild_topics.SUBJECT_WEIGHT)
        prompts = r.prompts()
        assert any("how the last raid went (Last raid: Icecrown Citadel 10" in p for p in prompts)
        life = [p for p in prompts if "my sourdough starter" in p and "Bring up something" in p]
        assert all("You play Arkon" in p for p in life)
        rows = r.store.conn.execute("SELECT kind, why FROM utterance WHERE dropped IS NULL "
                                    "AND kind='continue'").fetchall()
        assert rows  # answered by the others
    run(main())


def test_guild_topics_draw_the_subject_first_and_skip_his_and_officers_business(store):
    personas = {g: make_persona(g, f"T{g}") for g in (101, 102, 103)}
    topics = [guild_topics.GuildTopic("life", f"life:{i}", f"beat {i}", 101) for i in range(50)]
    topics.append(guild_topics.GuildTopic("raid", "raid_after:1", "how the last raid went"))
    rng = random.Random(4)
    weights = {101: 1.0, 102: 1.0, 103: 1.0}
    drawn = Counter(guild_topics.choose(topics, weights, personas, rng)[1].subject
                    for _ in range(3000))
    w = guild_topics.SUBJECT_WEIGHT
    assert abs(drawn["raid"] / 3000 - w["raid"] / (w["raid"] + w["life"] + w["game"])) < 0.04
    for _ in range(200):  # a life beat is its own member's to bring up
        speaker, topic = guild_topics.choose(topics, weights, personas, rng)
        assert topic.subject != "life" or speaker == 101
    assert guild_topics.choose(topics, {102: 1.0}, personas, random.Random(1),
                               used={t.key for t in topics})[1].subject == "game"
    seeds = [Seed("loot:1", "loot", ("loot-and-dkp",), "Testmage won [Helm] for 40 DKP", (1,)),
             Seed("loot:2", "loot", ("loot-and-dkp",), "T101 won [Item 77] for 40 DKP", (101,)),
             Seed("loot:3", "loot", ("loot-and-dkp",), "T102 won [Blade] for 40 DKP", (102,)),
             Seed("rival:1", "rival", ("general",), "R was active in trade")]
    import leeroy_mind.talk.guild_topics as gt
    original = gt.public_seeds
    try:
        gt.public_seeds = lambda conn, **kw: seeds
        found = gt.gather(store.conn, now=0, local=datetime(2026, 11, 12, 19), cast=personas,
                          human=HUMAN)
        named = gt.gather(store.conn, now=0, local=datetime(2026, 11, 12, 19), cast=personas,
                          human=HUMAN, item_name={77: "Shiny Ring"}.get)
    finally:
        gt.public_seeds = original
    assert [t.key for t in found] == ["loot:3", "rival:1"]  # his loot and an unnamed item out
    assert [t.key for t in named] == ["loot:2", "loot:3", "rival:1"]
    assert "T101 won [Shiny Ring] for 40 DKP" in named[0].topic


def test_public_seeds_never_carry_officers_business(store):
    from leeroy_mind.server.seeds import GAME_KINDS, public_seeds
    from conftest import seed_test_cast
    cast = seed_test_cast(store)
    now = store.clock.now()
    store.conn.execute("INSERT INTO realm_calendar(kind,creator,at,title,updated,state) "
                       "VALUES ('meeting',102,?,'Officer meeting',?,'posted')", (now + 3600, now))
    found = public_seeds(store.conn, now=now, local=datetime.fromtimestamp(now, timezone.utc),
                         cast=cast, human=HUMAN)
    assert not any(s.officers_only for s in found)
    assert {s.kind for s in found} <= set(GAME_KINDS)


# -- his login ----------------------------------------------------------------------------------

def test_nobody_greets_about_a_third_of_his_logins_and_never_more_than_two():
    rng = random.Random(11)
    counts = Counter(greetings.greeter_count(rng) for _ in range(5000))
    assert set(counts) == {0, 1, 2}
    assert abs(counts[0] / 5000 - 0.30) < 0.03


def greeted(r):
    return r.store.conn.execute("SELECT guid, text, why FROM utterance WHERE kind='greet' "
                                "AND dropped IS NULL ORDER BY id").fetchall()


def test_a_login_nobody_noticed_still_starts_the_quiet(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path)
        r.engine.greeter_draw = lambda rng: 0
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(60)
        assert greeted(r) == []
        assert r.engine._login_quiet(clock.now()) and LOGIN_QUIET_S > 60
    run(main())


def test_a_later_greeter_may_type_a_plain_hello(store, clock, tmp_path):
    async def main():
        r = make_rig(store, clock, tmp_path, script=numbered("hey there {n}"))
        r.engine.greeter_draw = lambda rng: 2
        r.engine.plain_hello_p = 1.0
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(60)
        rows = greeted(r)
        assert len(rows) == 2
        assert rows[0]["why"].startswith("greet:") and not rows[0]["why"].startswith("greet_plain")
        assert rows[1]["why"].startswith("greet_plain:")  # the test cast's men and plain women
        plain = {t.format(name="zan") for t in greetings.TEMPLATES}
        assert re.sub(r"[^\w/ ]", "", rows[1]["text"].lower()).strip() in plain
    run(main())


def test_a_resting_greeting_phrase_is_asked_again_then_dropped(store, clock, tmp_path):
    """"there he is" said at yesterday's login rests: a greeting that says it again is asked
    once more and, still saying it, never goes out; the next login a day later has moved on."""
    async def main():
        r = make_rig(store, clock, tmp_path, script=lambda m, msgs: "there he is, zan!")
        r.engine.greeter_draw = lambda rng: 1
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(60)
        first, = greeted(r)
        assert first["text"].startswith("there he is")
        await r.event(Logout(ts=0, guid=HUMAN, human=True))
        await r.engine.end_session()
        await r.advance(3600)
        await r.session()
        await r.event(Login(ts=0, guid=HUMAN, human=True))
        await r.advance(60)
        assert len(greeted(r)) == 1  # the second login's greeting was dropped
        asked = [p for p in r.prompts() if "just logged on" in p]
        assert 'Don\'t say "there he is"' in asked[-1]
        drops = r.store.conn.execute("SELECT dropped FROM utterance WHERE kind='greet' "
                                     "AND dropped IS NOT NULL").fetchall()
        assert [d[0] for d in drops] == ["tells"]
    run(main())


def test_greeting_phrases_rest_by_kind_and_age():
    him = ("Zan", "Testmage")
    day = 86400.0
    now = 10 * day
    said = [(now - 2 * day, "There he is!"), (now - 3600, "oh hey Zan good to see you"),
            (now - 5 * day, "the man of the hour")]
    c = greetings.Cooled(said, now, him, login_at=now)
    assert c.stock == ["there he is"]  # "man of the hour" was 5 days ago: rested out
    assert c.openings == ["oh hey {him}"]
    assert c.repeats("there he is!! finally") and c.repeats("Oh hey zan, welcome")
    assert c.repeats("oh hey testmage")  # his names count as one
    assert not c.repeats("evening zan") and not c.repeats("the man of the hour")
    assert c.avoid_line("Zan", "him") == ('Don\'t say "there he is" or "oh hey Zan": the guild '
                                          'has greeted him that way lately.')
    # this login's own greetings: only a stock phrase rests, never a plain opening
    same = greetings.Cooled([(now + 5, "o/ zan")], now + 10, him, login_at=now)
    assert not same.repeats("o/ zan") and not same.openings
    plain = greetings.Cooled([(now - 60, "hey zan")], now, him, login_at=now)
    assert all(plain.template(random.Random(i), "Zan") != "hey zan" for i in range(50))
    assert greetings.Cooled([], now, him).avoid_line("Zan", "him") == ""


# -- the flirt rule ------------------------------------------------------------------------------

def test_the_flirt_rule_only_on_turns_that_are_his():
    woman = make_persona(201, "Belra", player_gender="female", stance="lust")
    man = make_persona(101, "Arkon")
    her_away = comms_rules(ZAN, woman, at_him=False)
    assert FLIRT_RULE_AWAY["female"] in her_away and "Zan" not in her_away.split("\n")[-1]
    assert "You flirt only with" not in her_away
    assert FLIRT_RULE["female"].format(human="Zan") in comms_rules(ZAN, woman, at_him=True)
    his = comms_rules(ZAN, man, at_him=False)
    assert "only have eyes for" not in his and FLIRT_RULE_AWAY["male"] in his
    assert "only have eyes for Zan" in comms_rules(ZAN, man, at_him=False, with_him=True)
    assert "only have eyes for Zan" in comms_rules(ZAN, man, at_him=True)
    # no turn to judge by (a probe, a preview): as before
    assert comms_rules(ZAN, woman) == comms_rules(ZAN, woman, at_him=True)
    assert comms_rules(ZAN, man) == comms_rules(ZAN, man, at_him=True)


def test_a_turn_among_the_members_never_reads_the_flirt_permission(store, clock, tmp_path):
    save_member(store, 102, player_gender="female", stance="lust", style="bold")
    r = make_rig(store, clock, tmp_path, reply="lol same")
    r.speak_turn(Turn(kind=TurnKind.OPEN, speaker=102, channel="guild", max_lines=1))
    among = r.chat_calls()[-1]["messages"][0]["content"]
    assert "You flirt only with" not in among and FLIRT_RULE_AWAY["female"] in among
    r.run(90)  # past her own minute
    r.speak_turn(Turn(kind=TurnKind.FLIRT, speaker=102, channel="guild", addressee=HUMAN,
                      max_lines=1))
    flirt = r.chat_calls()[-1]["messages"][0]["content"]
    assert "You flirt only with Zan" in flirt
    r.close()
