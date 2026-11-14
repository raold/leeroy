"""Culture Part C: the guild's server bots post its real events, code-built from rows, exactly
once, never into the quiet window. Every name and row here is invented."""
import json
from dataclasses import replace
from datetime import datetime, time, timezone
from types import SimpleNamespace

import pytest

from conftest import TEST_CAST, run
from nightly.conftest import IDENT
from leeroy_mind.culture import bots as culture_bots
from leeroy_mind.culture.attach import attach_part_c
from leeroy_mind.culture.bots import BOTS, RECEIPT, SINCE_KEY, ServerBots
from leeroy_mind.culture.config import CultureConfig, load_culture_config
from leeroy_mind.personas.io import load_persona
from leeroy_mind.realm.calendar import result_text
from leeroy_mind.server.config import ServerConfig

UTC = timezone.utc
LOOT = "guild:loot:7:abc{n}"
ROLES = {101: "tank", 102: "healer", 103: "ranged", 104: "melee", 105: "healer"}


def make_app(h, tmp_path, *, emoji=False, window=(time(3), time(8)), birthdays=None):
    personas = {}
    for g in TEST_CAST:
        p = load_persona(h.store, g)
        p = replace(p, play=replace(p.play, raid_role=ROLES[g]))
        if birthdays and g in birthdays:
            p = replace(p, real_life=replace(p.real_life, birthday=birthdays[g]))
        personas[g] = p
    reacted = []
    app = SimpleNamespace(
        store=h.store, clock=h.clock, human_guid=1, stopping=False, data_dir=tmp_path,
        engine=SimpleNamespace(human=IDENT), personas=personas,
        world=SimpleNamespace(discord_members=lambda: {g: None for g in (*TEST_CAST, 1)}),
        culture_config=CultureConfig(enabled=True, bots=True, emoji=emoji),
        server=SimpleNamespace(reactions=SimpleNamespace(
            after_post=lambda mid, **kw: reacted.append((mid, kw)))),
        reacted=reacted)
    return app, ServerBots(app, ServerConfig(enabled=True, quiet_window=window))


def since(h, ago=86400.0):
    h.store.set_meta(SINCE_KEY, str(h.clock.now() - ago))


def posts(h, bot=None):
    rows = h.store.conn.execute("SELECT id,channel,author,origin,text,bot FROM discord_message "
                                "WHERE origin='bot' ORDER BY id").fetchall()
    return [dict(r) for r in rows if bot is None or r["bot"] == bot]


def calendar(h, *, kind="raid", at, title="Raid night", event=501):
    return h.store.conn.execute(
        "INSERT INTO realm_calendar(kind,creator,at,title,ref,updated,event_id,state) "
        "VALUES (?,101,?,?,?,?,?,'posted')", (kind, at, title, f"{kind}:{at}", at - 4 * 86400, event)).lastrowid


def loot(h, *, n=0, ts=None, winner=101, paid=47.0, item=123, name="Invented Blade"):
    key = LOOT.format(n=n)
    award = dict(guid=winner, paid=paid, bid=paid, priority=0, runner_up=None, runner_up_bid=None, close=False)
    h.store.set_meta(key, json.dumps(dict(state="charged", item_id=item, name=name, award=award,
                                          human_claimed=False, bidders=[])))
    return h.store.conn.execute("INSERT INTO dkp_ledger(ts,guid,delta,reason,raid_id,item_id,note) "
                                "VALUES (?,?,?,'item',7,?,?)", (h.clock.now() - 600 if ts is None else ts,
                                                                winner, -paid, item, key)).lastrowid


def raid(h, *, kills=3, wipes=2, ended=None):
    end = h.clock.now() - 3600 if ended is None else ended
    return h.store.conn.execute(
        "INSERT INTO guild_raid(started,ended,raid_key,size,heroic,offline,lead_guid,tank_guid,loot_guid,"
        "kills,wipes,outcome) VALUES (?,?,'naxx',25,0,0,101,101,102,?,?,'cleared')",
        (end - 7200, end, kills, wipes)).lastrowid


def raw(h):
    """A second connection without foreign keys (a new SQLite connection's default): realm rows
    whose native kill or source rows these tests do not build."""
    import sqlite3
    h.store.conn.commit()
    path = h.store.conn.execute("PRAGMA database_list").fetchone()[2]
    return sqlite3.connect(path, autocommit=True)


def announcement(h, *, boss="the Lich King", size=25, heroic=True, first="ever", state="sent"):
    payload = json.dumps({"boss_name": boss, "size": size, "heroic": heroic, "first": first}, separators=(",", ":"))
    now = h.clock.now()
    conn = raw(h)
    try:
        conn.execute(
            "INSERT INTO realm_announcement(command_id,kill_id,guild,human,map,boss_id,difficulty,cadence,"
            "period_key,first,payload_json,payload_sha256,process_id,generation,connection_epoch,prepared_at,"
            "expires_at,dispatched_at,settled_at,state,safe_reason) VALUES (?,?,21,1,631,12,3,"
            "'product-tue04-america-new_york-v1','2026-10-27',?,?,?,'p1',0,'1',?,?,?,?,?,'none')",
            (f"announce_{first}", f"{'a' * 32}:{1 if first == 'ever' else 2}", first, payload, "0" * 64,
             now - 700, now + 9000, now - 650, now - 600, state))
    finally:
        conn.close()


def realm_first(h, *, guid=103, name="Realm First! Level 80"):
    conn = raw(h)
    try:
        fid = conn.execute("INSERT INTO realm_fact(kind,text,source,ref,public,ts) VALUES "
                           "('realm_first',?,'event','',1,?)",
                           (f"Character {guid} earned {name}.", h.clock.now() - 300)).lastrowid
        proof = json.dumps({"output_fields": {"achievement_id": 457, "guid": guid, "name": name}},
                           separators=(",", ":"))
        conn.execute(
            "INSERT INTO realm_fact_origin(fact,producer_key,source,policy,assurance,proof_json,qualification_json,"
            "text_sha256,revision,state) VALUES (?,?,1,'realm_first_achievement_v1','observed_event',?,'{}',?,1,'active')",
            (fid, "event:" + "b" * 64 + ":realm_first_achievement_v1", proof, "c" * 64))
    finally:
        conn.close()
    return fid


def set_local(h, dt):
    h.clock.set(dt.timestamp())


SETUPS = {
    "signups": (lambda h: calendar(h, at=datetime(2026, 10, 31, 20, 0, tzinfo=UTC).timestamp()),
                "raid-schedule", "Raid night — Saturday 20:00. React 🛡️ ⛑️ ⚔️"),
    "loot": (loot, "loot-and-dkp", "[Invented Blade] -> Arkon, 47 dkp"),
    "logs": (raid, "logs", f"Logs are up: Naxxramas 25 — {result_text('naxx', 3, 2, False)}, 2h 00m"),
    "birthday": (lambda h: None, "general", "Happy birthday Belra! 🎂"),
    "firsts": (announcement, "announcements", "GUILD FIRST: the Lich King (25 heroic) is down!"),
    "reminders": (lambda h: calendar(h, at=h.clock.now() + 1800, event=777), "raid-schedule",
                  "Raid night in 30 minutes"),
}


@pytest.mark.parametrize("bot", BOTS)
def test_bot_posts_from_rows(h, tmp_path, bot):
    since(h)
    setup, channel, text = SETUPS[bot]
    setup(h)
    app, bots = make_app(h, tmp_path, birthdays={102: f"{h.clock.local():%m-%d}"} if bot == "birthday" else None)
    run(bots.tick(h.clock.now()))
    mine = posts(h, bot)
    assert [(m["channel"], m["text"], m["author"], m["origin"]) for m in mine] == [(channel, text, 0, "bot")]
    assert h.store.get_meta(RECEIPT.format(bot=bot, ref=_ref(h, bot))) == str(mine[0]["id"])
    # nothing made up: no chat line, no member line, no model
    assert h.store.conn.execute("SELECT count(*) FROM chat_line WHERE text=?", (text,)).fetchone()[0] == 0
    # members react to every bot post but a signup post, whose reactions are the real signups
    assert [mid for mid, _ in app.reacted] == ([] if bot == "signups" else [mine[0]["id"]])


def _ref(h, bot):
    if bot == "birthday":
        return f"102:{h.clock.local().year}"
    if bot == "firsts":
        return "announce_ever"
    row = h.store.conn.execute({"signups": "SELECT max(id) FROM realm_calendar WHERE event_id=501",
                                "reminders": "SELECT max(id) FROM realm_calendar WHERE event_id=777",
                                "loot": "SELECT max(id) FROM dkp_ledger",
                                "logs": "SELECT max(raid_id) FROM guild_raid"}[bot]).fetchone()
    return str(row[0])


def test_a_realm_first_achievement_and_a_meeting_reminder_reach_their_channels(h, tmp_path):
    since(h)
    realm_first(h)
    calendar(h, kind="meeting", at=h.clock.now() + 3540, title="Officer meeting", event=900)
    announcement(h, first="week")  # a weekly re-kill is no first
    app, bots = make_app(h, tmp_path)
    run(bots.tick(h.clock.now()))
    assert [(m["bot"], m["channel"], m["text"]) for m in posts(h)] == [  # oldest first
        ("firsts", "announcements", "REALM FIRST: Coros earned Realm First! Level 80!"),
        ("reminders", "officer-chat", "Officer meeting in 1 hour")]


def test_events_from_before_the_bots_were_switched_on_are_never_posted(h, tmp_path):
    loot(h, ts=h.clock.now() - 600)
    app, bots = make_app(h, tmp_path)
    run(bots.tick(h.clock.now()))  # the first tick sets the start
    assert posts(h) == [] and float(h.store.get_meta(SINCE_KEY)) == h.clock.now()
    loot(h, n=1, ts=h.clock.now() + 10, name="Invented Helm", item=1005)
    h.clock.advance(60)
    run(bots.tick(h.clock.now()))
    assert [m["text"] for m in posts(h)] == ["[Invented Helm] -> Arkon, 47 dkp"]


def test_bot_posts_once_across_restart(h, tmp_path, monkeypatch):
    since(h)
    ledger = loot(h)
    app, bots = make_app(h, tmp_path)
    real = h.store.set_meta

    def crash(key, value):  # the process dies between the post and its receipt
        if key.startswith("culture:bot:"):
            raise RuntimeError("crash")
        return real(key, value)
    monkeypatch.setattr(h.store, "set_meta", crash)
    with pytest.raises(RuntimeError):
        run(bots.tick(h.clock.now()))
    assert posts(h) == []  # the post rolled back with its receipt
    monkeypatch.setattr(h.store, "set_meta", real)
    for _ in range(3):  # restarts: a new bots object each time
        _, again = make_app(h, tmp_path)
        run(again.tick(h.clock.now()))
    assert len(posts(h, "loot")) == 1
    assert h.store.get_meta(RECEIPT.format(bot="loot", ref=ledger)) == str(posts(h)[0]["id"])


def test_bot_waits_for_the_window_end(h, tmp_path):
    night = datetime(2026, 10, 30, 2, 0, tzinfo=UTC)  # a loot award at 02:00, inside 01:00-08:00
    set_local(h, night)
    since(h)
    for n in range(12):
        loot(h, n=n, ts=night.timestamp() + n * 60, item=2000 + n, name=f"Invented Ring {n}")
    calendar(h, kind="raid", at=night.timestamp() + 3000, event=601)  # its reminder passes in the window
    app, bots = make_app(h, tmp_path, window=(time(1), time(8)))
    h.clock.set(night.timestamp() + 12 * 60)
    run(bots.tick(h.clock.now()))
    assert posts(h) == []
    set_local(h, datetime(2026, 10, 30, 8, 0, tzinfo=UTC))  # the window's end
    run(bots.tick(h.clock.now()))
    first = posts(h, "loot")
    assert len(first) == culture_bots.PER_TICK
    assert [m["text"] for m in first] == [f"[Invented Ring {n}] -> Arkon, 47 dkp" for n in range(10)]
    h.clock.advance(60)
    run(bots.tick(h.clock.now()))
    assert [m["text"] for m in posts(h, "loot")][10:] == [f"[Invented Ring {n}] -> Arkon, 47 dkp" for n in (10, 11)]
    assert posts(h, "reminders") == []  # its raid night began inside the window: never sent late


def test_signup_reactions_match_calendar_signups(h, tmp_path):
    at = h.clock.now() + 86400
    calendar(h, at=at)
    rsvp = [(101, "signed_up"), (102, "signed_up"), (103, "tentative"), (104, "declined"), (1, "signed_up")]
    h.store.conn.executemany("INSERT INTO realm_rsvp(event,guid,status,ts) VALUES (501,?,?,?)",
                             [(g, s, h.clock.now()) for g, s in rsvp])
    app, bots = make_app(h, tmp_path)
    run(bots.tick(h.clock.now()))
    (post,) = posts(h, "signups")

    def reactions():
        return sorted(tuple(r) for r in h.store.conn.execute(
            "SELECT member,emoji FROM discord_reaction WHERE message=?", (post["id"],)))
    assert reactions() == [(101, "🛡️"), (102, "⛑️"), (103, "🤷")]  # nothing for a decline or for him
    h.store.conn.execute("INSERT INTO realm_rsvp(event,guid,status,ts) VALUES (501,105,'signed_up',?)",
                         (h.clock.now(),))
    for _ in range(2):  # a later signup reacts once; nothing is added twice
        h.clock.advance(60)
        run(bots.tick(h.clock.now()))
    assert reactions() == [(101, "🛡️"), (102, "⛑️"), (103, "🤷"), (105, "⛑️")]


def test_signups_use_the_guilds_own_role_emoji_when_they_are_on(h, tmp_path):
    calendar(h, at=h.clock.now() + 86400)
    h.store.conn.executemany("INSERT INTO realm_rsvp(event,guid,status,ts) VALUES (501,?,'signed_up',0)",
                             [(103,), (104,)])
    app, bots = make_app(h, tmp_path, emoji=True)
    run(bots.tick(h.clock.now()))
    (post,) = posts(h, "signups")
    assert post["text"].endswith("React :tank: :healer: :dps:")
    assert sorted(tuple(r) for r in h.store.conn.execute(
        "SELECT member,emoji FROM discord_reaction WHERE message=?", (post["id"],))) == [
        (103, ":dps:"), (104, ":dps:")]


def test_a_real_name_never_goes_out(h, tmp_path):
    since(h)
    loot(h, name="Quillon's Blade")  # the fixture real name inside an item name
    app, bots = make_app(h, tmp_path)
    run(bots.tick(h.clock.now()))
    assert posts(h) == []


def test_culture_off_posts_nothing(tmp_path):
    path = tmp_path / "mind.toml"
    path.write_text("[server]\nenabled=true\n[culture]\nenabled=true\n")
    cfg = load_culture_config(path)
    assert (cfg.bots, cfg.emoji, cfg.memes) == (False, False, False)  # each off by default
    app = SimpleNamespace(culture_config=cfg, register_background=lambda f: pytest.fail("started"),
                          register_shutdown_drain=lambda f: pytest.fail("drained"))
    assert attach_part_c(app, ServerConfig(enabled=True)) is None
    path.write_text("[server]\nenabled=true\n[culture]\nbots=true\n")  # culture itself off
    app.culture_config = load_culture_config(path)
    assert attach_part_c(app, ServerConfig(enabled=True)) is None
    path.write_text("[server]\nenabled=true\n[culture]\nenabled=true\nbots=1\n")  # a wrong type: all off
    assert not load_culture_config(path).enabled


def test_the_running_mind_attaches_the_bots_only_when_asked(tmp_path):
    from selfies.fakes import mind_app
    (tmp_path / "off").mkdir()
    off = mind_app(tmp_path / "off", toml="[server]\nenabled=true\n[culture]\nenabled=true\n")
    try:
        assert off.server.extras is None
    finally:
        run(off.stop())
    (tmp_path / "on").mkdir()
    on = mind_app(tmp_path / "on", toml="[server]\nenabled=true\n[culture]\nenabled=true\nbots=true\n")
    try:
        assert isinstance(on.server.extras.bots, ServerBots) and on.server.extras.memes is False
    finally:
        run(on.stop())


def test_the_page_names_every_bot_as_the_mind_does():
    import re
    from pathlib import Path
    page = (Path(culture_bots.__file__).parents[1] / "console/static/js/comms/discord.mjs").read_text(encoding="utf-8")
    block = re.search(r"export var BOT_NAMES = \{([^}]*)\};", page).group(1)
    assert dict(re.findall(r'(\w+): "([^"]+)"', block)) == culture_bots.NAMES
    assert tuple(culture_bots.NAMES) == BOTS and set(culture_bots.CHANNEL) == set(BOTS)
