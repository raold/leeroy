"""WoW grounding ([talk] wow_grounding, talk/grounding.py): which turns get it, the raid and guild
card from stored rows only, the glossary, and her example lines from before the model switch,
screened and private lines never among them. Invented members and rows only."""
import json
from datetime import datetime, timezone

import pytest

from leeroy_mind.protocol import Member
from leeroy_mind.talk import grounding as g
from leeroy_mind.text import estimate_tokens

NOW = datetime(2026, 11, 20, 21, tzinfo=timezone.utc).timestamp()
BEFORE = datetime(2026, 11, 19, 19, 22, 45, tzinfo=timezone.utc).timestamp()
HUMAN = 1
ME = 101
NAMES = {1: "Zan", 101: "Arkon", 102: "Belra", 103: "Coros"}


def speaker(**kw):
    base = dict(cls=2, spec_tab=0, raid_role="healer", officer=False, human=HUMAN, names=NAMES,
                protect=tuple(n.casefold() for n in NAMES.values()))
    base.update(kw)
    return g.Speaker(ME, **base)


def run(store, sql, args=()):
    with store.transaction():
        return store.conn.execute(sql, args).lastrowid


def line(store, text, *, guid=ME, channel="guild", ts=BEFORE - 86400, to=0, origin="mind"):
    return run(store, "INSERT INTO chat_line(ts,channel,from_guid,from_name,to_guid,text,origin) "
                      "VALUES (?,?,?,?,?,?,?)", (ts, channel, guid, NAMES.get(guid, "X"), to, text,
                                                 origin))


def raid(store, *, started, ended=None, key="toc", size=25, heroic=1, kills=3, wipes=1):
    return run(store, "INSERT INTO guild_raid(started,ended,raid_key,size,heroic,offline,lead_guid,"
                      "tank_guid,loot_guid,kills,wipes) VALUES (?,?,?,?,?,0,102,103,102,?,?)",
               (started, ended, key, size, heroic, kills, wipes))


def boss(store, ts, boss_id, state, map_id=649):
    run(store, "INSERT INTO game_event(ts,type,guid,payload) VALUES (?,?,?,?)",
        (ts, "boss", 0, json.dumps({"map": map_id, "instance_id": 7, "boss_id": boss_id,
                                    "boss_name": "", "state": state, "members": [ME]})))


def wipe(store, end_ts, *, boss_id=4, deaths=((66013, 3), (0, 1)), failed="toc.anubarak.pursuing_spike"):
    run(store, "INSERT INTO tg_pull(id,context,map,instance_id,boss_id,difficulty,size,outcome,"
               "start_ms,end_ms,members,assisted,boss_entry,upd_mean_ms,kind,complete,scoreable,"
               "received) VALUES ('p1',?,649,7,?,3,25,'wiped',?,?,25,0,0,0,'his',1,1,?)",
        (f"649:{boss_id}:3", boss_id, int((end_ts - 300) * 1000), int(end_ts * 1000), end_ts))
    n = 0
    for spell, count in deaths:
        for _ in range(count):
            n += 1
            run(store, "INSERT INTO tg_death(pull,guid,at_ms,spell,source,x,y,hits) "
                       "VALUES ('p1',?,0,?,0,0,0,'[]')", (200 + n, spell))
    run(store, "INSERT INTO tg_death(pull,guid,at_ms,spell,source,x,y,hits) "
               "VALUES ('p1',?,0,99999,0,0,0,'[]')", (HUMAN,))  # his own death never counts
    run(store, "INSERT INTO tg_failure(pull,kind,key,guid,count,deaths,damage_share) "
               "VALUES ('p1','avoid',?,102,2,0,0)", (failed,))
    run(store, "INSERT INTO meta(key,value) VALUES ('tg:spell_names',?)",
        (json.dumps({"66013": "Penetrating Cold", "99999": "His Own Mistake"}),))


# -- which turns ---------------------------------------------------------------------------------

@pytest.mark.parametrize("channel", ["guild", "officer", "party", "raid", "raid_warning",
                                     "discord:strategy", "discord:raid-planning",
                                     "discord:loot-and-dkp", "discord:paladin", "discord:officer-chat",
                                     "voice:raid", "voice:raid2", "voice:healers", "voice:priests"])
def test_raid_and_guild_turns_are_grounded(channel):
    assert g.wow_turn(channel)


@pytest.mark.parametrize("channel", ["whisper", "dm:101", "gdm:3", "say", "yell", "discord:general",
                                     "discord:irl", "discord:food", "discord:applications",
                                     "voice:guild_hall", "voice:irl", "voice:private1", "", None])
def test_private_and_other_turns_never_are(channel):
    assert not g.wow_turn(channel)


# -- the card ------------------------------------------------------------------------------------

def test_a_running_raid_card(store):
    rid = raid(store, started=NOW - 3600)
    run(store, "INSERT INTO guild_attendance(raid_id,guid,status) VALUES (?,?,'raided')", (rid, ME))
    boss(store, NOW - 1200, 3, "killed")
    boss(store, NOW - 200, 4, "engaged")
    wipe(store, NOW - 600)
    run(store, "INSERT INTO meta(key,value) VALUES (?,?)", (f"guild:loot:{rid}:aa", json.dumps(
        {"name": "Signet of Testing", "award": {"guid": ME, "paid": 40}})))
    for guid, delta in ((ME, 50), (102, 80), (103, 10)):
        run(store, "INSERT INTO dkp_ledger(ts,guid,delta,reason) VALUES (?,?,?,'kill')",
            (NOW - 100, guid, delta))
    lines = g.card_lines(store.conn, speaker(), NOW, budget=1000)
    assert lines[0] == "You: Holy Paladin, healer."
    assert lines[1] == "Raid now: Trial of the Crusader 25 heroic, 3 kills, 1 wipe so far; you're in it."
    assert lines[2] == "Fighting now: Anub'arak."
    explains = [x for x in lines if x.startswith("Anub'arak, ")]
    # the playbook's lines for a healer, as the raid lead's brief picks them (a kite first)
    assert [x.split(":")[0] for x in explains] == ["Anub'arak, Pursued by Anub'arak",
                                                   "Anub'arak, Penetrating Cold"]
    assert ("Last wipe (Anub'arak, 10 minutes ago): most deaths to Penetrating Cold (3), "
            "melee or untagged (1); missed Pursued by Anub'arak.") in lines
    assert "Your loot: you won Signet of Testing for 40 DKP." in lines
    assert "Your DKP: 50, 2nd of 3." in lines
    assert not any("His Own Mistake" in x for x in lines)


def test_the_card_fits_its_budget_whole_lines_by_priority(store):
    raid(store, started=NOW - 3600)
    boss(store, NOW - 200, 4, "engaged")
    wipe(store, NOW - 600)
    full = g.card_lines(store.conn, speaker(), NOW, budget=1000)
    fitted = g.card_lines(store.conn, speaker(), NOW)
    assert estimate_tokens(g._section(g.CARD_HEAD, fitted)) <= g.CARD_TOKENS
    assert set(fitted) <= set(full) and fitted[:3] == full[:3]  # never a cut line
    tight = g.card_lines(store.conn, speaker(), NOW, budget=25)
    assert tight == ["You: Holy Paladin, healer."]


def test_an_ended_raid_within_a_day_and_her_bench(store):
    rid = raid(store, started=NOW - 6 * 3600, ended=NOW - 3 * 3600, key="naxx", heroic=0, kills=13,
               wipes=0)
    run(store, "INSERT INTO guild_attendance(raid_id,guid,status) VALUES (?,?,'benched')", (rid, ME))
    boss(store, NOW - 4 * 3600, 14, "killed", map_id=533)
    lines = g.card_lines(store.conn, speaker(cls=8, spec_tab=2, raid_role=None), NOW, budget=1000)
    assert lines[0] == "You: Frost Mage, ranged damage."
    assert lines[1] == ("Last raid (3 hours ago): Naxxramas 25, 13 kills, 0 wipes; "
                        "you were on the bench.")
    assert lines[2] == "Last boss: Kel'Thuzad, killed."


def test_no_recent_raid_gives_the_next_raid_night_and_the_last_kills(store):
    raid(store, started=NOW - 3 * 86400, ended=NOW - 3 * 86400 + 3600, key="icc", size=10,
         heroic=0, kills=4, wipes=2)
    run(store, "INSERT INTO realm_calendar(kind,creator,at,title,updated,event_id,state) "
               "VALUES ('raid',102,?,'Raid night',?,77,'posted')", (NOW + 5 * 3600, NOW))
    run(store, "INSERT INTO realm_rsvp(event,guid,status,ts) VALUES (77,?,'signed_up',?)", (ME, NOW))
    lines = g.card_lines(store.conn, speaker(), NOW, budget=1000)
    assert lines == ["You: Holy Paladin, healer.", "Next raid night: in 5 hours; you: signed up.",
                     "Last kills: Icecrown Citadel 10, 4 bosses, 2 days ago."]


def test_a_raid_left_open_for_days_is_not_running_now(store):
    raid(store, started=NOW - 2 * 86400, key="naxx", heroic=0, kills=2, wipes=0)
    lines = g.card_lines(store.conn, speaker(), NOW, budget=1000)
    assert not any(x.startswith(("Raid now", "Last raid")) for x in lines)
    assert "Last kills: Naxxramas 25, 2 bosses, 2 days ago." in lines


def test_nothing_stored_says_only_who_she_is(store):
    assert g.card_lines(store.conn, speaker(), NOW) == ["You: Holy Paladin, healer."]
    assert g.card_lines(store.conn, speaker(cls=0), NOW) == []
    assert g.card_lines(store.conn, speaker(spec_tab=-1, raid_role="tank"), NOW) == [
        "You: Paladin, tank."]


def test_her_lost_bid_names_the_winner(store):
    rid = raid(store, started=NOW - 3600)
    run(store, "INSERT INTO meta(key,value) VALUES (?,?)", (f"guild:loot:{rid}:bb", json.dumps(
        {"name": "Cloak of Testing", "award": {"guid": 102, "paid": 30, "runner_up": ME},
         "bidders": [{"guid": ME, "wanted": True}]})))
    assert "Your loot: Cloak of Testing went to Belra; you wanted it." in g.card_lines(
        store.conn, speaker(), NOW, budget=1000)


def test_build_reads_her_class_from_the_roster_when_the_loop_had_none(store):
    store.upsert_cast([Member(guid=ME, name="Arkon", race=1, class_=5, gender=0, level=80, rank=3,
                              online=False, map=0, zone=0, spec_tab=2)], NOW)
    ground, _ = g.build(store.conn, speaker(cls=0), "guild", NOW, before=BEFORE)
    assert ground.card[0] == "You: Shadow Priest, ranged damage."


# -- the glossary --------------------------------------------------------------------------------

def slang(store, term, uses, origin="chat:0"):
    return run(store, "INSERT INTO lexicon(kind,term,origin_ref,originator,born,uses,last_used) "
                      "VALUES ('slang',?,?,102,?,?,?)", (term, origin, NOW, uses, NOW))


def test_the_glossary_is_fixed_words_and_the_guilds_screened_slang(store):
    assert g.glossary(store.conn) == g.GLOSSARY
    private = line(store, "brb wiping on purpose", guid=102, channel="whisper", to=HUMAN)
    slang(store, "gnomed", 9)
    slang(store, "pew", 8)  # an everyday word is never slang
    slang(store, "sapphiron", 7)  # a name of the game
    slang(store, "rizz", 6)  # a later era's
    slang(store, "glorp", 5, origin=f"chat:{private}")  # born in a whisper
    slang(store, "zonked", 4)
    text = g.glossary(store.conn)
    assert text == g.GLOSSARY + " Guild slang: gnomed, zonked."
    assert estimate_tokens(text) <= g.GLOSSARY_TOKENS


# -- her example lines ---------------------------------------------------------------------------

def candidates(store, **kw):
    return g.candidates(store.conn, speaker(**{k: v for k, v in kw.items() if k != "officer_turn"}),
                        officer_turn=kw.get("officer_turn", False), before=BEFORE, now=NOW)


def test_examples_are_her_public_lines_from_before_the_switch(store):
    keep = line(store, "pulling the next pack, stay behind me", channel="raid")
    line(store, "ok who forgot to repair again lol", channel="discord:strategy")
    line(store, "this is after the switch so it never shows", ts=BEFORE + 60)
    line(store, "whispered to him, never an example", channel="whisper", to=HUMAN)
    line(store, "said in her dm with him, never an example", channel="dm:101", to=HUMAN)
    line(store, "officer business stays officer business", channel="officer")
    line(store, "a general channel line about lunch today", channel="discord:general")
    line(store, "somebody else wrote this nice line here", guid=102)
    line(store, "the owner wrote this line in guild chat", guid=HUMAN, origin="human")
    got = {c.text for c in candidates(store)}
    assert got == {"pulling the next pack, stay behind me", "ok who forgot to repair again lol"}
    assert keep in {c.id for c in candidates(store)}


def test_officer_lines_only_on_an_officers_turn_of_an_officer(store):
    line(store, "officer business stays officer business", channel="officer")
    assert not candidates(store, officer=True, officer_turn=False)
    assert not candidates(store, officer=False, officer_turn=True)
    assert [c.text for c in candidates(store, officer=True, officer_turn=True)] == [
        "officer business stays officer business"]


def test_lines_of_the_last_hours_are_the_conversation_not_examples(store):
    line(store, "this was said an hour ago in guild", ts=NOW - 3600)
    later = g.candidates(store.conn, speaker(), officer_turn=False, before=NOW, now=NOW)
    assert not later


@pytest.mark.parametrize("text", [
    "belra is pulling again lol",           # a member's name
    "zan you there",                        # his
    "got [Signet of Testing] finally",       # a link
    "check http://example.com later",        # a url
    "as an ai i cannot say that",           # model talk
    "that pull was so sus honestly",        # a later era's slang
    "ok",                                   # too short
    "ok " * 30,                             # too long
    "you absolute menace, pull the boss",   # a stock (slop) phrase
])
def test_screened_out(store, text):
    line(store, text)
    assert not candidates(store)


def test_ranked_by_thumbs_then_rewards_and_bad_ones_left_out(store):
    plain = line(store, "fine line about the trash pack here")
    rewarded = line(store, "rewarded line about cooldowns on this boss")
    thumbed = line(store, "thumbs up line about the tank swap")
    bad = line(store, "a bad line about the healers tonight")
    template = line(store, "a template line from the canned set")
    run(store, "INSERT INTO learn_weights(created,weights) VALUES (?,'{}')", (NOW,))
    for lid, value in ((rewarded, 0.5), (bad, -0.4)):
        run(store, "INSERT INTO talk_reward(line_id,guid,value,parts,weights_version,scored_at) "
                   "VALUES (?,?,?,'{}',1,?)", (lid, ME, value, NOW))
    run(store, "INSERT INTO talk_signal(line_id,guid,kind,value,ts,source) "
               "VALUES (?,?,'explicit',1,?,'explicit')", (thumbed, ME, NOW))
    run(store, "INSERT INTO talk_recipe(line_id,template_id,template_version,dials_hash,model_id,"
               "guard_verdict) VALUES (?,'x',?,?,'none','not_run')", (template, "0" * 16, "0" * 64))
    got = [c.id for c in candidates(store)]
    assert got == [thumbed, rewarded, plain]


def test_a_line_a_private_memory_cites_is_never_an_example(store):
    cited = line(store, "a line a private memory rests on")
    free = line(store, "a line nobody remembers privately")
    from conftest import make_mem
    with store.transaction():
        mid = store.insert_memory(make_mem(owner=ME, kind="conversation", text="a private thing",
                                           evidence=(f"chat:{cited}",)))
        store.conn.execute("UPDATE memory SET scope='private', scope_with=?, derived_at=NULL "
                           "WHERE id=?", (HUMAN, mid))
    assert [c.id for c in candidates(store)] == [free]


def test_shown_examples_fit_their_budget_and_never_two_alike(store):
    pool = [g.Candidate(i, BEFORE - i, "guild", t, 1.0 - i / 10) for i, t in enumerate([
        "pulling the next pack now stay close",
        "pulling the next pack now stay close ok",
        "who has the soulstone on the healer",
        "drink up before the boss please",
        "a fourth line that would never fit in the budget anyway"])]
    shown = g.exemplars(pool, "guild", 0)
    assert shown == ["pulling the next pack now stay close", "who has the soulstone on the healer",
                     "drink up before the boss please"]
    assert estimate_tokens(g._section(g.EXEMPLAR_HEAD, shown)) <= g.EXEMPLAR_TOKENS


def test_the_turns_medium_first_and_a_daily_rotation(store):
    pool = [g.Candidate(1, 1, "discord:strategy", "discord line about the boss plan", 3),
            *(g.Candidate(i, i, "guild", f"game line number {i} about raiding", 2 - i / 10)
              for i in range(2, 6))]
    assert g.exemplars(pool, "guild", 0)[0] == "game line number 2 about raiding"
    assert g.exemplars(pool, "discord:strategy", 0)[0] == "discord line about the boss plan"
    assert g.exemplars(pool, "guild", 86400) != g.exemplars(pool, "guild", 0)


def test_sections_in_priority_order():
    ground = g.Grounding(("You: Holy Paladin, healer.",), ("a line",), g.GLOSSARY)
    assert ground.sections() == [g.CARD_HEAD + "\n- You: Holy Paladin, healer.",
                                 g.EXEMPLAR_HEAD + "\n- a line", g.GLOSSARY]
    assert g.Grounding().sections() == []


def test_an_older_database_reads_as_nothing(tmp_path):
    import sqlite3
    conn = sqlite3.connect(tmp_path / "old.db")
    ground, pool = g.build(conn, speaker(), "guild", NOW, before=BEFORE)
    assert ground.card == ("You: Holy Paladin, healer.",) and not pool and not ground.exemplars
    assert ground.glossary == g.GLOSSARY
