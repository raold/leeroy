"""Romantic threads, phase 1 (drama/pulls.py): the recorder's event kinds, presence's saturation,
the household cost, stance damping and likeness, decay, the would-be openings with their labels
and caps, privacy scoping, the switch, and the proof that a recorded night writes nothing but
its own three tables. Invented members only (TEST_CAST 101-105; 101, 103 and 105 are men, 102
and 104 women; the owner is 1)."""
import asyncio
import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from leeroy_mind.drama import config as drama_config
from leeroy_mind.drama import pulls
from leeroy_mind.nightly.settings import night_bounds

run = asyncio.run
UTC = timezone.utc
NIGHT = "2026-10-29"
WINDOW = night_bounds(NIGHT, UTC)
T = datetime(2026, 10, 29, 21, 0, tzinfo=UTC).timestamp()
MEN, WOMEN = (101, 103, 105), (102, 104)
OWN = ("pull_event", "pull_shadow", "pull_night")
REF = re.compile(r"^(raid|event|encounter|feud|chat|discord|voice|move|claim|emotion|utterance):\d+$"
                 r"|^loot:guild:loot:\d+:[a-f0-9]+$")
REAL_LIKENESS = pulls.likeness


@pytest.fixture(autouse=True)
def alike(monkeypatch):
    """Every pair is exactly alike unless a test asks for the real likeness."""
    monkeypatch.setattr(pulls, "likeness", lambda cast, a, b: 1.0)


def cast(h, *, taken=(), stance=None, us=()):
    stance = stance or {}
    for g in MEN + WOMEN:
        over = {"player_gender": "female" if g in WOMEN else "male",
                "region": "US" if g in us else "UK"}
        if g in WOMEN:
            over["stance"] = stance.get(g, "none")
        if g in taken:
            over["household"] = "married, two kids"
        h.persona(g, **over)


def record(h, night=NIGHT, window=None):
    who = pulls.cast_from_store(h.store, h.human)
    window = night_bounds(night, UTC) if window is None else window
    with h.store.transaction():
        return pulls.record_night(h.store, night, window, who, now=window[1])


def events(h, kind=None):
    sql = "SELECT a, b, kind, weight, factor, amount, scope, ref FROM pull_event"
    rows = h.store.conn.execute(sql + (" WHERE kind = ?" if kind else "") + " ORDER BY a, b, kind, ref",
                                (kind,) if kind else ()).fetchall()
    return [dict(r) for r in rows]


def pairs(h, kind):
    return sorted((e["a"], e["b"]) for e in events(h, kind))


def shadow(h, a, b):
    row = h.store.conn.execute("SELECT * FROM pull_shadow WHERE a = ? AND b = ?", (a, b)).fetchone()
    return None if row is None else dict(row)


def sql(h, text, params=()):
    with h.store.transaction():
        return h.store.conn.execute(text, params).lastrowid


def raid(h, ident, members, *, offline=0, kills=1, t=T, long=True):
    sql(h, "INSERT INTO guild_raid(raid_id, started, ended, raid_key, size, heroic, offline, "
           "lead_guid, tank_guid, loot_guid, kills, wipes) VALUES (?,?,?,?,10,0,?,1,101,101,?,0)",
        (ident, t, t + (3600 if long else 60), "naxx", offline, kills))
    for g in members:
        sql(h, "INSERT INTO guild_attendance(raid_id, guid, status) VALUES (?,?,'raided')", (ident, g))


def game(h, t, kind, guid):
    return sql(h, "INSERT INTO game_event(ts, type, guid, payload, session_id) VALUES (?,?,?,?,NULL)",
               (t, kind, guid, json.dumps({"ts": int(t * 1000), "guid": guid, "map": 533})))


def seed_shadow(h, a, b, pull, *, first="2026-10-20", night="2026-10-28", state="none",
                thread_night=None, public=None, social=None):
    sql(h, "INSERT INTO pull_shadow(a, b, pull, public_pull, social_pull, events, first_night, "
           "last_event_night, updated_night, thread_state, thread_night) VALUES (?,?,?,?,?,3,?,?,?,?,?)",
        (a, b, pull, pull if public is None else public, pull if social is None else social,
         first, night, night, state, thread_night))


def hold(pull):
    """A stored pull that is still `pull` after one night without an event."""
    return pull / pulls.DECAY


# -- presence: raids, bosses, parties --------------------------------------------------------------

def test_a_raid_with_him_pairs_each_man_and_woman_both_ways(h):
    cast(h)
    raid(h, 1, (101, 102, 103, 1))
    summary = record(h)
    assert pairs(h, "raided") == [(101, 102), (102, 101), (102, 103), (103, 102)]
    assert all(e["scope"] == "public" and e["ref"] == "raid:1" and e["amount"] == 1.0
               for e in events(h))
    # the two men are no pair in this release, and the owner is not of the cast
    assert summary["skipped"] == {"not_cast": 6, "same_sex": 2}
    assert summary["kinds"] == {"raided": 4} and summary["pairs"] == 4


def test_a_raid_without_him_is_grouped_play_and_an_empty_one_is_nothing(h):
    cast(h)
    raid(h, 1, (101, 102), offline=1)
    raid(h, 2, (103, 104), offline=1, kills=0, long=False)  # never got going
    record(h)
    assert pairs(h, "grouped_away") == [(101, 102), (102, 101)]
    assert events(h, "grouped_away")[0]["weight"] == 1.5 and not events(h, "raided")


def test_who_stood_at_a_kill_and_a_first_kill_counts_double(h):
    cast(h)
    h.boss(T, 533, 15956, "engaged", (101, 102, 104))
    game(h, T + 30, "death", 104)
    kill = h.boss(T + 90, 533, 15956, "killed", (101, 102, 104))
    h.boss(T + 200, 533, 15956, "engaged", (101, 102, 104))
    game(h, T + 210, "death", 104)
    game(h, T + 220, "resurrect", 104)  # a battle rez: she stood at the end
    again = h.boss(T + 300, 533, 15956, "killed", (101, 102, 104))
    summary = record(h)
    assert pairs(h, "first_kill") == [(101, 102), (102, 101)]
    assert [(e["weight"], e["ref"]) for e in events(h, "first_kill")] == [(2.0, f"event:{kill}")] * 2
    # she was dead at the first kill and stood at the second; for the pair the first kill had
    # already given, the night's presence is full
    assert pairs(h, "kill_alive") == [(101, 104), (104, 101)]
    assert {e["ref"] for e in events(h, "kill_alive")} == {f"event:{again}"}
    assert summary["skipped"]["presence_cap"] == 2


def test_a_wipe_counts_only_for_those_still_standing_and_a_kill_without_its_engage_for_nobody(h):
    cast(h)
    h.boss(T, 533, 1, "engaged", (101, 102, 104))
    game(h, T + 10, "death", 102)
    h.boss(T + 60, 533, 1, "wiped", (101, 102, 104))
    h.boss(T + 100, 533, 2, "killed", (101, 102))  # its engage was never stored
    record(h)
    assert pairs(h, "wipe_stood") == [(101, 104), (104, 101)]
    assert events(h, "wipe_stood")[0]["weight"] == 0.5
    assert not events(h, "first_kill") and not events(h, "kill_alive")


def test_his_five_man_party_is_a_small_shared_activity_and_a_raid_group_or_a_battleground_is_not(h):
    cast(h)
    party = h.group(T, (1, 101, 102, 104))
    h.group(T + 60, (1, 101, 102, 103, 104, 105), raid=True)
    h.bg(T + 120, (103, 104))
    record(h)
    assert pairs(h, "partied") == [(101, 102), (101, 104), (102, 101), (104, 101)]
    assert {(e["weight"], e["scope"], e["ref"]) for e in events(h)} == {(4.0, "public", f"event:{party}")}
    assert shadow(h, 101, 102)["social_pull"] == 4.0          # social, not presence


def test_presence_saturates_and_is_capped_a_night(h):
    cast(h)
    raid(h, 1, (101, 102))
    h.boss(T, 533, 1, "engaged", (101, 102))
    h.boss(T + 60, 533, 1, "killed", (101, 102))
    h.boss(T + 100, 533, 2, "engaged", (101, 102))
    h.boss(T + 160, 533, 2, "killed", (101, 102))
    h.boss(T + 200, 533, 3, "engaged", (101, 102))
    h.boss(T + 260, 533, 3, "wiped", (101, 102))
    summary = record(h)
    got = [(e["kind"], e["weight"], e["amount"]) for e in events(h) if e["a"] == 101]
    # the raid counts whole; every later presence event at a quarter; never above 2 a night
    assert sorted(got) == [("first_kill", 0.5, 0.5), ("first_kill", 0.5, 0.5), ("raided", 1.0, 1.0)]
    assert shadow(h, 101, 102)["pull"] == 2.0 and shadow(h, 101, 102)["social_pull"] == 0.0
    assert summary["skipped"]["presence_cap"] == 2          # the wipe, both ways
    assert pulls.PRESENCE_CAP == 2.0 and pulls.PRESENCE_LATER == 0.25
    assert pulls.PRESENCE == {"raided", "grouped_away", "kill_alive", "first_kill", "wipe_stood"}


# -- scenes, rumours, feuds, feelings ----------------------------------------------------------------

def scene(h, a, b, deltas, *, volition=None, ref="", night=NIGHT, reason="encounter"):
    ident = sql(h, "INSERT INTO encounter(night, idx, a, b, setting, status, scene, volition, ref) "
                   "VALUES (?, (SELECT count(*) FROM encounter), ?, ?, 'hall', 'done', 'x', ?, ?)",
                (night, a, b, volition, ref))
    for x, y, delta in deltas:
        sql(h, "INSERT INTO rel_delta(a, b, dim, delta, reason, evidence, ts, applied) "
               "VALUES (?,?,'affinity',?,?,?,?,1)",
            (x, y, delta, reason, json.dumps([f"encounter:{ident}", "memory:1"]), int(WINDOW[1] + 60)))
    return ident


def test_a_warm_accepted_scene_draws_both_and_a_cold_or_unapplied_one_neither(h):
    cast(h)
    warm = scene(h, 101, 102, [(101, 102, 2), (102, 101, -1)])
    scene(h, 103, 104, [(103, 104, -2)])
    scene(h, 105, 104, [(105, 104, 3)], reason="encounter (not applied: the night's cap is used up)")
    scene(h, 105, 102, [(105, 102, 3)], night="2026-10-28")  # another night's scene
    record(h)
    assert pairs(h, "warm_scene") == [(101, 102), (102, 101)]
    assert {(e["scope"], e["ref"], e["weight"]) for e in events(h)} == {("pair", f"encounter:{warm}", 10.0)}


def test_setting_a_rumour_straight_to_her_face_takes_her_side_and_behind_her_back_nothing(h):
    """A correction told to someone else never reaches the one it is about: nothing stores that
    she learned of it, so only a correction told to her moves her pull."""
    cast(h)
    rumour = sql(h, "INSERT INTO rumor(root, about, canonical, created, last_told) "
                    "VALUES ('r1', 104, 'x', ?, ?)", (T, T))
    ident = scene(h, 101, 104, [], volition="correct", ref=f"rule:rumor:{rumour}")  # to her
    scene(h, 105, 103, [], volition="correct", ref=f"rule:rumor:{rumour}")  # to another member
    scene(h, 104, 103, [], volition="correct", ref=f"rule:rumor:{rumour}")  # she spoke for herself
    record(h)
    assert events(h, "took_side") == [{"a": 104, "b": 101, "kind": "took_side", "weight": 12.0,
                                       "factor": 1.0, "amount": 12.0, "scope": "pair",
                                       "ref": f"encounter:{ident}"}]
    assert h.count("pull_event") == 1


def test_siding_with_a_member_in_a_feud_is_public(h):
    cast(h)
    feud = sql(h, "INSERT INTO feud(a, b, started, cause, sides) VALUES (101, 103, ?, 'x', ?)",
               (T, json.dumps({"101": [102, 105], "103": [104]})))
    record(h)
    assert pairs(h, "took_side") == [(101, 102), (103, 104)]
    assert {(e["scope"], e["ref"]) for e in events(h, "took_side")} == {("public", f"feud:{feud}")}


def feel(h, guid, target, kind, memories, *, intensity=2, source="reflect", night=NIGHT):
    return sql(h, "INSERT INTO emotion(guid, night, type, target, intensity, why, source, evidence, ts) "
                  "VALUES (?,?,?,?,?,'x',?,?,?)",
               (guid, night, kind, target, intensity, source,
                json.dumps([f"memory:{m}" for m in memories]), T))


def test_tonights_admiration_for_a_member_counts_only_on_open_memories(h):
    cast(h)
    seen = h.mem(102, "a memory of the raid")
    hidden = h.mem(104, "a memory of a whisper", scope="private", scope_with=1)
    ident = feel(h, 102, 101, "admiration", [seen], intensity=3)
    feel(h, 104, 103, "gratitude", [seen, hidden])          # one memory is private
    feel(h, 104, 105, "resentment", [seen])                 # no warm feeling
    feel(h, 102, 1, "admiration", [seen])                   # toward the owner
    feel(h, 102, 103, "admiration", [seen], source="encounter")   # the scene's own, counted as a scene
    feel(h, 102, 105, "admiration", [seen], night="2026-10-28")
    record(h)
    assert events(h) == [{"a": 102, "b": 101, "kind": "admired", "weight": 6.25, "factor": 1.0,
                          "amount": 6.25, "scope": "pair", "ref": f"emotion:{ident}"}]


# -- chat ------------------------------------------------------------------------------------------

def test_a_reply_naming_and_agreeing_with_a_member_is_her_being_heard(h):
    cast(h)
    h.say(T, 102, "we should swap the tanks on this one")
    line = h.say(T + 30, 101, "exactly, Belra is right")
    h.say(T + 60, 103, "good point")                      # names nobody
    h.say(T + 90, 105, "Belra is wrong, I don't agree")   # no agreement
    h.say(T + 5000, 105, "exactly, Belra")                # nothing of hers recent enough
    record(h)
    assert events(h, "heard") == [{"a": 102, "b": 101, "kind": "heard", "weight": 7.5, "factor": 1.0,
                                   "amount": 7.5, "scope": "public", "ref": f"chat:{line}"}]


def test_answering_a_taunt_aimed_at_another_member_defends_her(h):
    cast(h)
    h.say(T, 103, "Dunna is such a noob")
    first = h.say(T + 20, 101, "lay off Dunna")
    h.say(T + 600, 1, "Belra is useless tonight")        # the owner's taunt counts as one too
    second = h.say(T + 620, 105, "back off Testmage, she did fine")
    h.say(T + 1200, 103, "Dunna is trash")
    h.say(T + 1210, 105, "anyone want to do a heroic")   # no defence
    record(h)
    assert [(e["a"], e["b"], e["ref"], e["weight"]) for e in events(h, "defended")] == [
        (102, 105, f"chat:{second}", 12.0), (104, 101, f"chat:{first}", 12.0)]


@pytest.mark.parametrize("channel", ["whisper", "dm:102", "officer", "voice:officers", "gdm:7",
                                     "discord:officer-chat", "discord:no-such-channel",
                                     "voice:private1"])
def test_only_public_channels_are_read(h, channel):
    """Whispers and DMs are with the owner; the officers' channels, a group DM and a Discord
    channel the layout lacks are restricted audiences, and a private voice room is private:
    nothing said there pulls anyone."""
    cast(h)
    h.say(T, 102, "we should swap the tanks", channel=channel, to=1 if channel == "whisper" else 0)
    h.say(T + 5, 101, "exactly, Belra is right", channel=channel, to=1 if channel == "whisper" else 0)
    h.say(T + 60, 103, "Dunna is such a noob", channel=channel)
    h.say(T + 70, 105, "lay off Dunna", channel=channel)
    record(h)
    assert events(h) == [] and h.count("pull_shadow") == 0


def heard_by(h, line, *guids):
    sql(h, "UPDATE chat_line SET audience = ? WHERE id = ?", (json.dumps(list(guids)), line))


def test_naming_a_member_on_the_games_channels(h):
    cast(h)
    named = h.say(T, 101, "Belra and Dunna should take the left side")
    h.say(T + 10, 103, "Arkon, wait for the tank")                    # names a man
    h.say(T + 20, 105, "Dunna", channel="discord:general")            # Discord is _discord's
    away = h.say(T + 30, 105, "Belra, the portal", channel="party")
    heard_by(h, away, 105, 104)                                       # she was not in that party
    record(h)
    assert [(e["a"], e["b"], e["weight"], e["scope"], e["ref"]) for e in events(h, "mentioned")] == [
        (102, 101, 4.0, "public", f"chat:{named}"), (104, 101, 4.0, "public", f"chat:{named}")]


def test_a_line_whose_audience_leaves_her_out_moves_nothing_of_hers(h):
    cast(h)
    h.say(T, 102, "we should swap the tanks", channel="party")
    agree = h.say(T + 30, 101, "exactly, Belra is right", channel="party")
    heard_by(h, agree, 101, 103)                 # she had left the party
    taunt = h.say(T + 600, 103, "Dunna is such a noob", channel="raid")
    defend = h.say(T + 620, 101, "lay off Dunna", channel="raid")
    for line in (taunt, defend):
        heard_by(h, line, 101, 103, 105)         # she was not in the raid
    move(h, 105, 102, "compliment", channel="party", t=T + 900)
    said = h.say(T + 905, 105, "nice pull", channel="party")
    heard_by(h, said, 105, 101)                  # she was not there to hear it
    move(h, 103, 104, "joke", channel="say", t=T + 1000)   # no line of his: nobody heard it
    good = h.say(T + 1200, 103, "Dunna carried that", channel="say")
    heard_by(h, good, 103, 105)
    claim(h, 103, "Dunna", good)
    record(h)
    assert events(h) == []


def test_both_speaking_in_one_banter_riff(h):
    cast(h)

    def riff(t, guid, channel="guild", why="banter:topic", dropped=None):
        ident = h.utter(t, guid, "x")
        sql(h, "UPDATE utterance SET channel = ?, why = ?, dropped = ? WHERE id = ?",
            (channel, why, dropped, ident))
        return ident

    first = riff(T, 101)
    riff(T + 60, 102)
    riff(T + 120, 104, dropped="guard")           # never sent
    riff(T + 2000, 104)                           # a later riff of her own
    riff(T + 2060, 103, why="opener")             # no banter
    riff(T + 3000, 105, channel="officer")        # never read
    riff(T + 3010, 102, channel="officer")
    record(h)
    assert [(e["a"], e["b"], e["weight"], e["scope"], e["ref"]) for e in events(h, "riffed")] == [
        (101, 102, 6.0, "public", f"utterance:{first}"), (102, 101, 6.0, "public", f"utterance:{first}")]


# -- Discord ------------------------------------------------------------------------------------------

def post(h, ident, channel, author, t, *, reply_to=None, text="x", origin="mind"):
    sql(h, "INSERT INTO discord_message(id, channel, author, origin, text, reply_to, ts) "
           "VALUES (?,?,?,?,?,?,?)", (ident, channel, author, origin, text, reply_to, t))


def react(h, message, member, t, emoji="\U0001f602"):
    sql(h, "INSERT INTO discord_reaction(message, emoji, member, ts) VALUES (?,?,?,?)",
        (message, emoji, member, t))


def test_a_discord_reply_counts_both_ways_and_a_late_one_not(h):
    cast(h)
    post(h, 1, "general", 102, T)
    post(h, 2, "general", 101, T + 400, reply_to=1)          # within ten minutes
    post(h, 3, "general", 104, T + 2000)
    post(h, 4, "general", 103, T + 2000 + 700, reply_to=3)   # too late
    post(h, 5, "general", 1, T + 5000, origin="human")
    post(h, 6, "general", 105, T + 5010, reply_to=5)         # a reply to the owner
    record(h)
    assert [(e["a"], e["b"], e["weight"], e["scope"], e["ref"]) for e in events(h, "answered")] == [
        (102, 101, 5.0, "public", "discord:2")]
    assert [(e["a"], e["b"], e["weight"]) for e in events(h, "replied")] == [(101, 102, 4.0)]
    assert not events(h, "talked")       # 400 s apart is no back and forth; a reply is no second event


def test_talking_back_and_forth_and_naming_a_member(h):
    cast(h)
    post(h, 1, "general", 102, T)
    post(h, 2, "general", 101, T + 60)                               # right after her
    post(h, 3, "general", 104, T + 90, text="ask Ebbo, he knows")    # names another member
    post(h, 4, "food", 103, T + 95)                                  # another channel
    record(h)
    assert pairs(h, "talked") == [(101, 102), (101, 104), (102, 101), (104, 101)]
    assert events(h, "talked")[0]["weight"] == 2.5
    assert [(e["a"], e["b"], e["weight"], e["ref"]) for e in events(h, "mentioned")] == [
        (105, 104, 4.0, "discord:3")]


def test_a_reaction_counts_and_more_when_she_spoke_in_the_conversation(h):
    cast(h)
    post(h, 1, "pets", 101, T)
    post(h, 2, "pets", 102, T + 1000)
    react(h, 1, 102, T + 1090)             # she spoke and laughed
    react(h, 1, 104, T + 1095)             # she only reacted
    react(h, 2, 101, T + 1099, "\U0001f44e")  # a thumbs down is nothing
    record(h)
    assert [(e["a"], e["b"], e["weight"], e["scope"], e["ref"]) for e in events(h, "banter")] == [
        (102, 101, 6.0, "public", "discord:1")]
    assert [(e["a"], e["b"], e["weight"]) for e in events(h, "reacted")] == [(104, 101, 2.5)]


@pytest.mark.parametrize("channel", ["officer-chat", "dm:102", "gdm:3", "no-such-channel"])
def test_discord_outside_the_public_channels_is_never_read(h, channel):
    cast(h)
    post(h, 1, channel, 102, T)
    post(h, 2, channel, 101, T + 30, reply_to=1, text="Dunna said so")
    react(h, 1, 101, T + 40)
    record(h)
    assert events(h) == []


# -- what the talk pipeline and the claims table already scored ---------------------------------------

def move(h, guid, counterpart, main, *, channel="guild", t=T):
    return sql(h, "INSERT INTO talk_move(ts, guid, counterpart, channel, main) VALUES (?,?,?,?,?)",
               (t, guid, counterpart, channel, main))


def test_a_joke_at_a_member_is_shared_and_a_compliment_is_hers(h):
    cast(h)
    joke = move(h, 101, 102, "joke")
    nice = move(h, 103, 104, "compliment", channel="discord:general")
    move(h, 105, 104, "tease", channel="whisper")      # never read
    move(h, 105, 102, "raid")                          # no social move
    move(h, 101, 1, "joke")                            # at the owner
    record(h)
    assert [(e["a"], e["b"], e["weight"], e["ref"]) for e in events(h, "joked")] == [
        (101, 102, 4.0, f"move:{joke}"), (102, 101, 4.0, f"move:{joke}")]
    assert [(e["a"], e["b"], e["weight"], e["ref"]) for e in events(h, "complimented")] == [
        (104, 103, 7.5, f"move:{nice}")]


def claim(h, guid, subject, line, *, polarity=1, kind="opinion"):
    return sql(h, "INSERT INTO member_claim(guid, kind, subject, text, polarity, source, source_line, "
                  "first_seen, last_seen, embedding) VALUES (?,?,?,'x',?,'chat',?,?,?,zeroblob(2048))",
               (guid, kind, subject, polarity, line, T, T))


def test_speaking_well_of_a_member_by_name_in_public(h):
    cast(h)
    said = h.say(T, 101, "Belra carried that fight")
    private = h.say(T + 5, 103, "Dunna is great", channel="officer")
    good = claim(h, 101, "belra", said)
    claim(h, 105, "Belra", said, polarity=-1)            # a poor opinion
    claim(h, 103, "Dunna", private)                      # said among the officers
    claim(h, 105, "the new patch", said)                 # about no member
    claim(h, 102, "Belra", said)                         # about herself
    record(h)
    assert [(e["a"], e["b"], e["kind"], e["weight"], e["ref"]) for e in events(h)] == [
        (101, 102, "praised", 5.0, f"claim:{good}"), (102, 101, "mentioned", 4.0, f"chat:{said}"),
        (102, 101, "spoke_well", 9.0, f"claim:{good}")]


# -- voice ---------------------------------------------------------------------------------------------

def seat(h, guid, room, start, minutes):
    sql(h, "INSERT INTO voice_presence(guid, room, since, until) VALUES (?,?,?,?)",
        (guid, room, start, start + minutes * 60))


LATE = datetime(2026, 10, 29, 23, 0, tzinfo=UTC).timestamp()  # 23:00 in Britain, 19:00 in New York
NOON = datetime(2026, 10, 29, 15, 0, tzinfo=UTC).timestamp()


def test_a_small_voice_room_together_and_late_on_the_holders_own_clock(h):
    cast(h, us=(103,))
    seat(h, 101, "smoke_room", LATE, 45)
    seat(h, 102, "smoke_room", LATE, 45)
    seat(h, 103, "irl", LATE, 120)       # 19:00 for him: a room together, not a late one
    seat(h, 104, "irl", LATE, 120)       # 23:00 for her
    seat(h, 105, "lore", LATE, 15)
    seat(h, 102, "lore", LATE, 15)       # too short together
    record(h)
    got = {(e["a"], e["b"]): (e["kind"], e["weight"], e["scope"]) for e in events(h)}
    assert got == {(101, 102): ("voice_late", 7.5, "public"), (102, 101): ("voice_late", 7.5, "public"),
                   (104, 103): ("voice_late", 7.5, "public"), (103, 104): ("voice_room", 5.0, "public")}


def test_a_crowded_room_and_the_officers_and_private_rooms_count_for_nobody(h, monkeypatch):
    cast(h)
    seat(h, 101, "smoke_room", NOON, 120)
    seat(h, 102, "smoke_room", NOON, 120)
    for g in (101, 102, 103):
        seat(h, g, "guild_hall", LATE, 60)
    seat(h, 105, "officers", LATE, 60)
    seat(h, 104, "officers", LATE, 60)
    seat(h, 103, "private1", LATE + 4000, 40)
    seat(h, 104, "private1", LATE + 4000, 40)
    monkeypatch.setattr(pulls, "VOICE_ROOM_MAX", 2)
    summary = record(h)
    assert {(e["a"], e["b"]): e["kind"] for e in events(h)} == {
        (101, 102): "voice_room", (102, 101): "voice_room"}
    assert summary["skipped"]["voice_crowd"] == 1 and pulls.VOICE_ROOM_MAX == 2


def test_the_room_limit_is_four_members():
    assert pulls.VOICE_ROOM_MAX == 4 and pulls.DISCORD_REPLY_S == 600.0


# -- DKP -------------------------------------------------------------------------------------------------

def award(h, winner, runner, *, t=T, close=True, key="guild:loot:3:abc123"):
    h.store.set_meta(key, json.dumps({"state": "charged", "item_id": 40000, "award": {
        "guid": winner, "paid": 50, "runner_up": runner, "runner_up_bid": 48, "close": close}}))
    sql(h, "INSERT INTO dkp_ledger(ts, guid, delta, reason, raid_id, item_id, note) "
           "VALUES (?,?,-50,'item',3,40000,?)", (t, winner, key))


def test_a_close_award_is_an_outcome_and_respect_after_it_is_a_pull_once(h):
    cast(h)
    award(h, 101, 102)
    first = record(h)
    assert pairs(h, "dkp_close") == [(101, 102), (102, 101)]
    assert all(e["amount"] == 0 and e["weight"] == 0 for e in events(h, "dkp_close"))
    assert shadow(h, 102, 101) is None and first["pairs"] == 0   # an outcome moves nothing
    sql(h, "INSERT INTO rel_delta(a, b, dim, delta, reason, evidence, ts, applied) "
           "VALUES (102, 101, 'respect', 1, 'copresence', '[]', ?, 1)", (int(T + 86400),))
    record(h, "2026-10-30")
    record(h, "2026-10-31")
    assert [(e["a"], e["b"], e["amount"], e["ref"]) for e in events(h, "dkp_respect")] == [
        (102, 101, 7.5, "loot:guild:loot:3:abc123")]
    assert shadow(h, 102, 101)["events"] == 1


def test_an_award_that_was_not_close_is_nothing(h):
    cast(h)
    award(h, 101, 102, close=False)
    record(h)
    assert events(h) == []


# -- the household cost, the stance damping, likeness, the caps ---------------------------------------------

def test_a_household_is_a_cost_and_her_stance_toward_him_damps_her_pull_a_little(h):
    cast(h, taken=(101, 104), stance={102: "lust", 104: "drawn"})
    raid(h, 1, (101, 102, 103, 104))
    record(h)
    got = {(e["a"], e["b"]): (e["factor"], e["amount"]) for e in events(h, "raided")}
    assert got[(103, 102)] == (1.0, 1.0)                       # a free man: no cost
    assert got[(101, 102)] == (0.6, 0.6)                       # a married man
    assert got[(102, 101)] == (0.6, 0.6)                       # she lusts after the owner
    assert got[(104, 103)] == pytest.approx((0.48, 0.48))      # married and drawn to him: 0.6 x 0.8
    who = pulls.cast_from_store(h.store, h.human)
    assert who.taken == {101, 104} and who.stance == {102: "lust", 104: "drawn"}
    assert pulls.factor(who, 105) == 1.0
    assert pulls.STANCE_FACTOR == {"fond": 0.9, "drawn": 0.8, "lust": 0.6} and pulls.TAKEN_FACTOR == 0.6


def test_the_two_costs_together_never_go_under_the_floor(h):
    cast(h, taken=(102,), stance={102: "lust", 104: "fond"})
    h.persona(101, player_gender="male", stance="lust")       # a man is never damped by a stance
    who = pulls.cast_from_store(h.store, h.human)
    assert pulls.factor(who, 102) == 0.4 == pulls.FACTOR_FLOOR   # 0.6 x 0.6 would be 0.36
    assert pulls.factor(who, 104) == 0.9 and pulls.factor(who, 101) == 1.0


def test_likeness_is_bounded_symmetric_seeded_and_follows_the_cards(h):
    cast(h, us=(105,))
    h.persona(101, player_gender="male", region="UK", night_owl=True, humour="dry",
              outside_raids=["fishing", "arena"], likes=["Old maps", "tea"])
    h.persona(102, player_gender="female", region="UK", night_owl=True, humour="dry",
              outside_raids=["arena", "fishing"], likes=["tea", "old  maps"])
    h.persona(104, player_gender="female", region="UK", night_owl=False, humour="puns",
              outside_raids=["crafting"], likes=["spreadsheets"])
    h.persona(105, player_gender="male", region="US", night_owl=True, humour="dark",
              outside_raids=["gathering"], likes=["trucks"])
    who = pulls.cast_from_store(h.store, h.human)
    near, far = REAL_LIKENESS(who, 101, 102), REAL_LIKENESS(who, 105, 104)
    assert near == REAL_LIKENESS(who, 102, 101) == REAL_LIKENESS(pulls.cast_from_store(h.store, h.human), 101, 102)
    assert 1.2 <= near <= pulls.LIKENESS_MAX == 1.25      # everything shared
    assert pulls.LIKENESS_MIN == 0.8 <= far <= 0.9        # nothing shared, an ocean and a clock apart
    for a in MEN:
        for b in WOMEN:
            assert 0.8 <= REAL_LIKENESS(who, a, b) <= 1.25
    assert REAL_LIKENESS(who, 101, 999) == 1.0            # no card: no opinion


def test_likeness_multiplies_what_a_pair_takes(h, monkeypatch):
    cast(h)
    monkeypatch.setattr(pulls, "likeness", lambda cast, a, b: 1.2 if {a, b} == {101, 102} else 0.8)
    raid(h, 1, (101, 102, 104))
    record(h)
    got = {(e["a"], e["b"]): e["amount"] for e in events(h)}
    assert got == {(101, 102): 1.2, (102, 101): 1.2, (101, 104): 0.8, (104, 101): 0.8}
    assert [r[0] for r in h.store.conn.execute(
        "SELECT DISTINCT likeness FROM pull_event ORDER BY 1")] == [0.8, 1.2]


def test_each_kind_and_each_night_has_a_cap(h, monkeypatch):
    cast(h)
    raid(h, 1, (101, 102))
    raid(h, 2, (101, 102))          # a second raid the same night: the kind's cap is one
    summary = record(h)
    assert pairs(h, "raided") == [(101, 102), (102, 101)] and summary["skipped"]["kind_cap"] == 2
    monkeypatch.setattr(pulls, "NIGHT_CAP", 1.5)
    scene(h, 101, 102, [(101, 102, 2)], night="2026-10-30")
    raid(h, 3, (101, 102), t=T + 86400)
    again = record(h, "2026-10-30")
    amounts = sorted(e["amount"] for e in events(h)
                     if e["a"] == 101 and (e["ref"] == "raid:3" or e["kind"] == "warm_scene"))
    assert amounts == [0.5, 1.0]    # the raid's 1, then what was left of 1.5 for the scene's 10
    assert again["skipped"] == {}


def test_members_on_a_break_and_members_without_a_persona_are_left_out(h):
    cast(h)
    sql(h, "INSERT INTO away(guid, reason, left_at) VALUES (102, 'x', ?)", (T,))
    raid(h, 1, (101, 102, 104, 999))
    record(h)
    assert pairs(h, "raided") == [(101, 104), (104, 101)]


# -- decay -------------------------------------------------------------------------------------------------

def test_a_pull_halves_over_twenty_one_nights_without_an_event(h):
    cast(h)
    raid(h, 1, (101, 102))
    scene(h, 101, 102, [(101, 102, 2)])
    record(h)
    assert shadow(h, 101, 102)["pull"] == 11.0
    record(h, "2026-10-30")
    assert shadow(h, 101, 102)["pull"] == pytest.approx(11.0 * 0.5 ** (1 / 21))
    record(h, "2026-11-19")   # 21 nights after the event
    row = shadow(h, 101, 102)
    assert row["pull"] == pytest.approx(5.5) and row["public_pull"] == pytest.approx(0.5)
    assert row["social_pull"] == pytest.approx(5.0)
    assert (row["first_night"], row["last_event_night"], row["updated_night"]) == (
        NIGHT, NIGHT, "2026-11-19")
    assert pulls.decayed(40.0, 0) == 40.0 and pulls.decayed(40.0, 42) == pytest.approx(10.0)
    assert pulls.HALF_LIFE == 21.0


def test_the_public_and_the_social_part_decay_by_their_own_nights(h):
    """A night of a private scene alone keeps the pull but lets its public part decay; a night
    of a raid alone keeps it too but lets its social part decay; a pull with no event for 21
    nights halves in every part."""
    cast(h)
    seed_shadow(h, 101, 102, 30.0, public=10.0, social=20.0)
    scene(h, 101, 102, [(101, 102, 2)])                  # between the two: social, not public
    record(h)
    row = shadow(h, 101, 102)
    assert (row["pull"], row["public_pull"], row["social_pull"]) == pytest.approx(
        (40.0, 10.0 * pulls.DECAY, 30.0))
    raid(h, 1, (101, 102), t=T + 86400)                  # public presence: neither private nor social
    record(h, "2026-10-30")
    row = shadow(h, 101, 102)
    assert (row["pull"], row["public_pull"], row["social_pull"]) == pytest.approx(
        (41.0, 10.0 * pulls.DECAY + 1.0, 30.0 * pulls.DECAY))
    seed_shadow(h, 103, 104, 30.0, public=10.0, social=20.0, night="2026-10-30")
    record(h, "2026-11-20")
    row = shadow(h, 103, 104)
    assert (row["pull"], row["public_pull"], row["social_pull"]) == pytest.approx((15.0, 5.0, 10.0))


def test_a_night_with_an_event_does_not_decay_and_a_night_is_recorded_once(h):
    cast(h)
    raid(h, 1, (101, 102))
    first = record(h)
    raid(h, 2, (101, 102), t=T + 86400)
    record(h, "2026-10-30")
    assert shadow(h, 101, 102)["pull"] == 2.0 and shadow(h, 101, 102)["events"] == 2
    assert record(h) == first                                    # already recorded: left alone
    assert record(h, "2026-10-28") == {"skipped": "out_of_order"}
    assert h.count("pull_event") == 4 and h.count("pull_night") == 2


# -- would-be threads ----------------------------------------------------------------------------------------

@pytest.fixture
def sure(monkeypatch):
    monkeypatch.setattr(pulls, "OPEN_P", 1.0)
    monkeypatch.setattr(pulls, "OPENS_PER_NIGHT", 9)


def test_a_thread_would_open_at_pull_35(h, sure):
    cast(h)
    seed_shadow(h, 101, 102, hold(35.0))
    seed_shadow(h, 103, 104, hold(34.9))
    summary = record(h)
    row = shadow(h, 101, 102)
    assert (row["thread_state"], row["thread_night"], row["shape"], row["thread_why"]) == (
        "open", NIGHT, "unrequited", "")
    assert shadow(h, 103, 104)["thread_state"] == "none"       # under the threshold
    assert (summary["threshold"], summary["opened"], summary["open"], summary["waiting"]) == (1, 1, 1, {})
    assert summary["shapes"] == {"unrequited": 1}
    assert (summary["at"], summary["max"]) == ({"10": 2, "20": 2, "35": 1}, {"men": 35.0, "women": 0.0})
    assert pulls.OPEN_PULL == 35.0


@pytest.mark.parametrize("why", ["affinity", "social", "draw", "feud", "ex"])
def test_what_holds_a_candidate_back(h, monkeypatch, why):
    cast(h)
    # presence alone never opens a thread: enough of the pull must be social
    seed_shadow(h, 101, 102, 60.0, social=11.0 if why == "social" else 60.0)
    if why == "affinity":
        h.rel(101, 102, affinity=-0.5)      # he dislikes her
    monkeypatch.setattr(pulls, "OPEN_P", 0.0 if why == "draw" else 1.0)
    if why == "feud":
        sql(h, "INSERT INTO feud(a, b, started, cause) VALUES (101, 102, ?, 'x')", (T - 9 * 86400,))
    if why == "ex":
        sql(h, "INSERT INTO breakup(woman, partner, for_guid, stage, night, ts) "
               "VALUES (102, 101, 1, 'whispers', '2026-10-20', ?)", (T - 9 * 86400,))
    summary = record(h)
    row = shadow(h, 101, 102)
    assert (row["thread_state"], row["thread_why"]) == ("waiting", why)
    assert summary["waiting"] == {why: 1} and summary["open"] == 0


def test_raids_alone_never_open_a_thread(h, monkeypatch):
    monkeypatch.setattr(pulls, "OPEN_P", 1.0)
    cast(h)
    seed_shadow(h, 101, 102, 80.0, social=0.0)
    raid(h, 1, (101, 102))
    record(h)
    assert (shadow(h, 101, 102)["thread_state"], shadow(h, 101, 102)["thread_why"]) == ("waiting", "social")
    assert pulls.OPEN_SOCIAL == 12.0 and pulls.OPEN_AFFINITY == 0.0


def test_the_draw_is_seeded_by_the_night_and_the_pair(h):
    cast(h)
    seed_shadow(h, 101, 102, 60.0)
    record(h)
    first = shadow(h, 101, 102)["thread_state"]
    sql(h, "DELETE FROM pull_night")
    sql(h, "UPDATE pull_shadow SET thread_state = 'none', updated_night = '2026-10-28'")
    record(h)
    assert shadow(h, 101, 102)["thread_state"] == first


def test_one_thread_a_member_and_the_stronger_direction_holds_the_pair(h, sure):
    cast(h)
    for a, b, pull in ((101, 102, 50.0), (102, 101, 45.0), (103, 102, 48.0)):
        seed_shadow(h, a, b, pull)
    summary = record(h)
    assert shadow(h, 101, 102)["thread_state"] == "open"
    assert (shadow(h, 102, 101)["thread_state"], shadow(h, 102, 101)["thread_why"]) == ("waiting", "pair_open")
    assert (shadow(h, 103, 102)["thread_state"], shadow(h, 103, 102)["thread_why"]) == ("waiting", "cap_member")
    assert summary["waiting"] == {"cap_member": 1, "pair_open": 1} and summary["threshold"] == 3
    # the next night the open one stays open and the others still wait
    record(h, "2026-10-30")
    assert shadow(h, 101, 102)["thread_state"] == "open" and shadow(h, 101, 102)["thread_night"] == NIGHT
    assert shadow(h, 102, 101)["thread_why"] == "pair_open"


def test_the_caps_admit_three_threads_one_with_a_taken_member_and_one_a_night():
    def c(a, b, pull, shape="unrequited", taken=False):
        return {"a": a, "b": b, "pull": pull, "shape": shape, "taken": taken}
    live = [c(1, 2, 40.0), c(3, 4, 40.0, taken=True)]
    opened, why = pulls.admit([c(5, 6, 50.0, taken=True), c(7, 8, 45.0), c(9, 10, 60.0), c(1, 12, 70.0)], live)
    assert [(t["a"], t["b"]) for t in opened] == [(9, 10)]
    assert why == {(1, 12): "cap_member", (5, 6): "cap_taken", (7, 8): "cap_live"}
    assert (pulls.THREADS_MAX, pulls.TAKEN_MAX, pulls.OPENS_PER_NIGHT) == (3, 1, 1)
    # the rarest shape among the live threads opens first, whatever its pull
    opened, why = pulls.admit([c(5, 6, 90.0), c(7, 8, 36.0, "triangle")], [c(1, 2, 40.0), c(3, 4, 40.0)])
    assert [(t["a"], t["b"]) for t in opened] == [(7, 8)] and why == {(5, 6): "cap_live"}
    # at most one new thread a night
    opened, why = pulls.admit([c(5, 6, 90.0), c(7, 8, 80.0)], [])
    assert [(t["a"], t["b"]) for t in opened] == [(5, 6)] and why == {(7, 8): "cap_night"}


def test_one_opening_a_night_in_the_ledger(h, monkeypatch):
    monkeypatch.setattr(pulls, "OPEN_P", 1.0)
    cast(h)
    seed_shadow(h, 101, 102, 60.0)
    seed_shadow(h, 103, 104, 50.0)
    first = record(h)
    assert first["opened"] == 1 and first["waiting"] == {"cap_night": 1}
    assert shadow(h, 103, 104)["thread_why"] == "cap_night"
    second = record(h, "2026-10-30")
    assert second["opened"] == 1 and second["open"] == 2
    assert shadow(h, 103, 104)["thread_night"] == "2026-10-30"


def test_the_taken_cap_holds_a_second_thread_with_a_taken_member(h, sure):
    cast(h, taken=(102, 104))
    for a, b, pull in ((101, 102, 60.0), (103, 104, 50.0)):
        seed_shadow(h, a, b, pull)
    summary = record(h)
    assert shadow(h, 101, 102)["thread_state"] == "open" and shadow(h, 101, 102)["taken"] == 1
    assert shadow(h, 103, 104)["thread_why"] == "cap_taken"
    assert summary["open_taken"] == 1 and summary["waiting"] == {"cap_taken": 1}


def test_the_labels():
    def shape(**kw):
        return pulls.shape_of(**{"reverse": 0.0, "taken": False, "third": False,
                                 "nights_to_open": 5, **kw})
    assert shape() == "unrequited"
    assert shape(nights_to_open=20) == "slow_burn"
    assert shape(reverse=20.0) == "mutual_open"
    assert shape(reverse=20.0, taken=True) == "mutual_secret"
    assert shape(reverse=19.9, taken=True) == "unrequited"
    assert shape(reverse=60.0, taken=True, third=True) == "triangle"
    assert set(pulls.SHAPES) == {"unrequited", "mutual_secret", "mutual_open", "triangle", "slow_burn"}


def test_labels_come_from_the_nights_facts(h, sure):
    cast(h, taken=(102,))
    seed_shadow(h, 101, 102, 60.0)
    seed_shadow(h, 102, 101, hold(21.0))
    record(h)
    assert shadow(h, 101, 102)["shape"] == "mutual_secret"
    # a third member's pull toward her makes it a triangle the next night
    seed_shadow(h, 103, 102, 30.0, night=NIGHT)
    record(h, "2026-10-30")
    assert shadow(h, 101, 102)["shape"] == "triangle" and shadow(h, 101, 102)["thread_state"] == "open"


def test_a_slow_burn_and_her_arc_with_him_as_a_ceiling_and_a_triangle(h, sure):
    cast(h, stance={104: "lust"})
    seed_shadow(h, 101, 102, 60.0, first="2026-10-01")
    seed_shadow(h, 104, 103, 60.0)
    h.rel(104, 1, romance_stage="late_nights")
    record(h)
    assert (shadow(h, 101, 102)["shape"], shadow(h, 101, 102)["ceiling"]) == ("slow_burn", "")
    # her arc with the owner is at late_nights: he is the third, and it never passes acknowledged
    assert (shadow(h, 104, 103)["shape"], shadow(h, 104, 103)["ceiling"]) == ("triangle", "acknowledged")


def test_only_a_third_members_pull_toward_either_makes_a_triangle(h, sure):
    """His own strong pull toward another woman is no third person in this thread; another
    man's toward her is."""
    cast(h)
    seed_shadow(h, 101, 102, 60.0)
    seed_shadow(h, 101, 104, 30.0)       # outgoing, from one of the two
    seed_shadow(h, 102, 105, 30.0)       # outgoing, from the other
    record(h)
    assert shadow(h, 101, 102)["shape"] == "unrequited"
    seed_shadow(h, 105, 102, 30.0, night=NIGHT)
    record(h, "2026-10-30")
    assert shadow(h, 101, 102)["shape"] == "triangle"


def test_a_member_couple_with_someone_else_makes_a_triangle(h, sure):
    cast(h)
    seed_shadow(h, 101, 102, 60.0)
    for x, y in ((102, 105), (105, 102)):
        h.rel(x, y, romance_stage="whispers")
    record(h)
    assert shadow(h, 101, 102)["shape"] == "triangle"


def test_a_would_be_thread_fades_under_ten_and_a_waiting_one_lets_go(h, sure):
    cast(h)
    seed_shadow(h, 101, 102, 10.1, state="open", thread_night="2026-10-20")
    seed_shadow(h, 103, 104, 35.5, state="waiting", thread_night="2026-10-27")
    summary = record(h)
    assert (shadow(h, 101, 102)["thread_state"], shadow(h, 101, 102)["thread_night"]) == ("faded", NIGHT)
    assert shadow(h, 103, 104)["thread_state"] == "none" and shadow(h, 103, 104)["thread_night"] is None
    assert summary["faded"] == 1 and summary["open"] == 0


# -- privacy -----------------------------------------------------------------------------------------------------

def everything(h):
    raid(h, 1, (101, 102))
    h.boss(T, 533, 1, "engaged", (101, 102))
    h.boss(T + 60, 533, 1, "killed", (101, 102))
    h.group(T + 90, (1, 101, 102))
    scene(h, 101, 102, [(101, 102, 2)])
    first = h.say(T, 102, "SECRETWORDS we should swap the tanks")
    h.say(T + 30, 101, "SECRETWORDS exactly, Belra is right")
    post(h, 1, "general", 101, T, text="SECRETWORDS")
    post(h, 2, "general", 102, T + 60, reply_to=1, text="SECRETWORDS Arkon")
    react(h, 1, 102, T + 90)
    move(h, 101, 102, "joke")
    claim(h, 101, "Belra", first)
    feel(h, 102, 101, "admiration", [h.mem(102, "SECRETWORDS a memory of the raid")])
    seat(h, 101, "smoke_room", LATE, 45)
    seat(h, 102, "smoke_room", LATE, 45)
    award(h, 101, 102)
    for t, g in ((T + 100, 101), (T + 130, 102)):
        sql(h, "UPDATE utterance SET why = 'banter:topic' WHERE id = ?", (h.utter(t, g, "SECRETWORDS"),))
    h.say(T + 200, 103, "SECRETWORDS Dunna, the portal")


def test_rows_hold_ids_counts_and_codes_never_text(h, monkeypatch):
    monkeypatch.setattr(pulls, "NIGHT_CAP", 1000.0)
    cast(h)
    everything(h)
    record(h)
    rows = events(h)
    assert {e["kind"] for e in rows} >= {"raided", "first_kill", "partied", "warm_scene", "heard",
                                         "answered", "replied", "banter", "riffed", "mentioned",
                                         "joked", "spoke_well", "praised", "admired", "voice_late",
                                         "dkp_close"}
    assert all(REF.match(e["ref"]) for e in rows), [e["ref"] for e in rows]
    assert all(e["scope"] in pulls.SCOPES for e in rows)
    dump = json.dumps([list(r) for t in OWN for r in h.store.conn.execute(f"SELECT * FROM {t}")])
    assert "SECRETWORDS" not in dump and "Belra" not in dump and "Arkon" not in dump


def test_public_pull_counts_only_public_evidence(h):
    cast(h)
    raid(h, 1, (101, 102))                    # public
    scene(h, 101, 102, [(101, 102, 2)])       # between the two
    record(h)
    row = shadow(h, 101, 102)
    assert row["pull"] == 11.0 and row["public_pull"] == 1.0 and row["social_pull"] == 10.0
    assert record(h, "2026-10-30")["scopes"] == {}


def test_every_kind_has_a_weight_and_a_cap():
    assert set(pulls.KINDS) == pulls.PRESENCE | {
        "partied", "warm_scene", "took_side", "heard", "defended", "answered", "replied", "talked",
        "mentioned", "reacted", "banter", "riffed", "joked", "complimented", "spoke_well", "praised",
        "admired", "voice_room", "voice_late", "dkp_close", "dkp_respect"}
    social = [w for k, (w, _) in pulls.KINDS.items() if k not in pulls.PRESENCE and w > 0]
    # a social event weighs more than a whole night of presence can give... or at least as much
    assert min(social) > pulls.PRESENCE_CAP


# -- the switch -----------------------------------------------------------------------------------------------------

def test_threads_is_off_by_default_and_record_turns_it_on(tmp_path):
    path = tmp_path / "mind.toml"
    assert drama_config.load_drama_config(path).threads == "off"
    path.write_bytes(b'[drama]\nmember_romance = true\n')
    assert drama_config.load_drama_config(path) == drama_config.DramaConfig(True, "off")
    path.write_bytes(b'[drama]\nthreads = "record"\n')
    assert drama_config.load_drama_config(path) == drama_config.DramaConfig(False, "record")


@pytest.mark.parametrize("value,key", [('"on"', "threads_on"), ('"maybe"', "threads"),
                                       ("true", "threads"), ("3", "threads")])
def test_on_is_reserved_and_refused_without_touching_member_romance(tmp_path, caplog, value, key):
    path = tmp_path / f"{key}-{len(value)}.toml"
    path.write_bytes(f"[drama]\nmember_romance = true\nthreads = {value}\n".encode())
    with caplog.at_level("WARNING"):
        cfg = drama_config.load_drama_config(path)
    assert cfg == drama_config.DramaConfig(True, "off")
    assert f"drama_config_invalid key={key}" in caplog.text


def test_the_settings_carry_the_mode_and_an_unknown_one_reads_off(h, tmp_path):
    from leeroy_mind.config import load_config
    from leeroy_mind.nightly.settings import load_settings
    (tmp_path / "mind.toml").write_bytes(b'[drama]\nthreads = "record"\n')
    settings = load_settings(load_config(tmp_path / "missing.toml"), tmp_path)
    assert settings.drama_threads == "record" and h.settings.drama_threads == "off"
    ctx = SimpleNamespace(settings=replace(h.settings, drama_threads="on"))
    assert drama_config.threads_mode(ctx) == "off"


class _Runner:
    def __init__(self):
        self.added = []

    def add_stage(self, name, fn, *, after, model):
        self.added.append((name, fn, after, model))


@pytest.mark.parametrize("text,stages", [(b"", 0), (b'[drama]\nthreads = "off"\n', 0),
                                         (b'[drama]\nthreads = "on"\n', 0),
                                         (b'[drama]\nthreads = "record"\n', 1)])
def test_only_record_attaches_the_stage(tmp_path, text, stages):
    (tmp_path / "mind.toml").write_bytes(text)
    app = SimpleNamespace(data_dir=tmp_path, night_runner=_Runner())
    pulls.attach(app)
    assert len(app.night_runner.added) == stages
    if stages:
        assert app.night_runner.added == [("pulls", pulls.stage_pulls, "encounters", None)]


def busy_night(h):
    cast(h, taken=(102,), stance={104: "lust"})
    everything(h)
    raid(h, 2, (101, 102, 103, 104, 105), t=T + 60)
    h.say(T + 100, 103, "Dunna is such a noob")
    h.say(T + 120, 105, "lay off Dunna")
    seed_shadow(h, 105, 104, 60.0)
    h.rel(105, 104, affinity=30.0)
    h.rel(101, 102, affinity=12.0, attraction=7.0)
    h.clock.set(WINDOW[1] + 600)


def test_off_runs_nothing_and_writes_no_row(h):
    busy_night(h)
    sql(h, "DELETE FROM pull_shadow")
    ctx = h.ctx()
    run(pulls.stage_pulls(ctx))
    assert [h.count(t) for t in OWN] == [0, 0, 0]
    assert not ctx.done("pulls", "all") and not ctx.failures


def test_record_runs_once_a_night(h, monkeypatch):
    monkeypatch.setattr(pulls, "OPEN_P", 1.0)
    monkeypatch.setattr(pulls, "NIGHT_CAP", 1000.0)
    busy_night(h)
    h.settings = replace(h.settings, drama_threads="record")
    ctx = h.ctx()
    run(pulls.stage_pulls(ctx))
    assert ctx.done("pulls", "all") and not ctx.failures
    counts = [h.count(t) for t in OWN]
    assert counts[0] > 20 and counts[2] == 1
    assert shadow(h, 105, 104)["thread_state"] == "open"
    run(pulls.stage_pulls(h.ctx()))
    assert [h.count(t) for t in OWN] == counts


def table_hashes(conn):
    """A hash of every row of every table but the recorder's own and the night's step log."""
    out = {}
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    for name in names:
        if name in OWN or name == "night_step":
            continue
        digest = hashlib.sha256()
        for row in sorted(repr(tuple(r)) for r in conn.execute(f'SELECT * FROM "{name}"')):
            digest.update(row.encode())
        out[name] = digest.hexdigest()
    return out


def test_a_recorded_night_writes_only_its_own_tables(h, monkeypatch):
    """The owner's ruling for this phase: no relationship row, attraction, scene, prompt input or
    memory changes. Every other table hashes the same before and after a busy recorded night
    that exercises every source."""
    monkeypatch.setattr(pulls, "OPEN_P", 1.0)
    monkeypatch.setattr(pulls, "likeness", REAL_LIKENESS)
    monkeypatch.setattr(pulls, "NIGHT_CAP", 1000.0)
    busy_night(h)
    h.settings = replace(h.settings, drama_threads="record")
    before = table_hashes(h.store.conn)
    for must in ("relationship", "rel_delta", "encounter", "memory", "volition", "emotion",
                 "persona_core", "persona_state", "jealousy", "feud", "rumor", "knows", "fact",
                 "attraction_state", "chat_line", "utterance", "meta", "breakup", "talk_move",
                 "member_claim", "discord_message", "discord_reaction", "voice_presence"):
        assert must in before
    ctx = h.ctx()
    run(pulls.stage_pulls(ctx))
    assert ctx.done("pulls", "all") and h.count("pull_event") > 20 and h.count("pull_night") == 1
    after = table_hashes(h.store.conn)
    assert {t for t in before if before[t] != after[t]} == set()
    assert set(before) == set(after)
    rel = h.store.get_relationship(101, 102)
    assert (rel.affinity, rel.attraction, rel.romance_stage) == (12.0, 7.0, "none")


def test_the_recorder_is_no_part_of_the_base_pipeline():
    from leeroy_mind.nightly.pipeline import STAGES
    assert "pulls" not in [s.name for s in STAGES]


def test_the_mode_is_pinned_at_mind_start(h):
    from nightly.test_night_wiring import app_for, context
    path = h.settings.data_dir / "mind.toml"
    path.write_bytes(b'[drama]\nthreads = "record"\n')
    app = app_for(h)
    try:
        path.write_bytes(b'[drama]\nthreads = "off"\n')
        assert context(app).settings.drama_threads == "record"
        names = [s.name for s in app.night_runner.stages]
        assert names.index("pulls") == names.index("encounters") + 1
    finally:
        run(app.stop())


def test_a_mind_started_without_it_has_no_stage(h):
    from nightly.test_night_wiring import app_for, context
    path = h.settings.data_dir / "mind.toml"
    app = app_for(h)
    try:
        path.write_bytes(b'[drama]\nthreads = "record"\n')
        assert context(app).settings.drama_threads == "off"
        assert "pulls" not in [s.name for s in app.night_runner.stages]
    finally:
        run(app.stop())
