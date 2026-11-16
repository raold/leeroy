import dataclasses
import json
import logging

import pytest
from conftest import HUMAN, OFFICERS, TEST_CAST, mem, run, seed_test_cast
from leeroy_mind.life.state import Impulse
from leeroy_mind.life.statebook import STATE_KEY, StateBook
from leeroy_mind.personas.io import PersonaCache
from leeroy_mind.protocol import Boss, Chat, LootWon
from leeroy_mind.talk.world import WorldView

CAST = tuple(TEST_CAST)


def world_of(store):
    if not store.persona_versions():
        seed_test_cast(store)
    world = WorldView(is_officer=lambda g: g in OFFICERS)
    world.human_guid = HUMAN
    world.members = {g: dataclasses.replace(mem(g), name=TEST_CAST[g][0]) for g in CAST}
    world.online = set(CAST)
    return world


def rig(store, clock):
    world = world_of(store)
    book = StateBook(store, clock, PersonaCache(store), world, lambda: HUMAN)
    book.tick(clock.now())  # the first tick only sets the high-water marks
    return book, world


def later(clock, book, s=31.0):
    run(clock.advance(s, step=s))
    book.tick(clock.now())


def line(store, clock, channel, frm, text, *, to=0, origin="mind", audience=None):
    name = TEST_CAST[frm][0] if frm in TEST_CAST else "Owner"
    chat = Chat(ts=int(clock.now() * 1000), channel=channel, from_guid=frm, from_name=name,
                to_guid=to, text=text, map=0, zone=10)
    return store.insert_chat_line(chat, session_id=None, origin=origin, audience=audience)


def post(store, clock, channel, author, text, *, origin="mind", reply_to=None):
    with store.transaction():
        return store.conn.execute(
            "INSERT INTO discord_message(channel,author,origin,text,reply_to,ts) "
            "VALUES (?,?,?,?,?,?)", (channel, author, origin, text, reply_to, clock.now())).lastrowid


def stored(store, g):
    raw = store.get_meta(STATE_KEY.format(guid=g))
    return None if raw is None else json.loads(raw)


def test_his_warm_whisper_lifts_her_and_persists(store, clock, caplog):
    caplog.set_level(logging.INFO, logger="leeroy_mind")
    book, world = rig(store, clock)
    line(store, clock, "whisper", HUMAN, "thanks, you're the best", to=102, origin="human",
         audience=(102,))
    later(clock, book)
    assert book.get(102).toward_him > 0.1
    data = stored(store, 102)
    assert set(data) == {"v", "e", "s", "o", "h", "ts"}
    assert all(type(v) in (int, float) for v in data.values())
    again = StateBook(store, clock, PersonaCache(store), world, lambda: HUMAN)
    assert again.get(102) == book.get(102)
    assert all(stored(store, g) is None for g in (101, 103, 104, 105))
    assert "life_state_tick: impulses=1 members=1" in caplog.text
    assert "best" not in caplog.text


def test_history_before_the_first_tick_is_never_replayed(store, clock):
    world_of(store)
    line(store, clock, "whisper", HUMAN, "you suck", to=102, origin="human", audience=(102,))
    book, _ = rig(store, clock)
    later(clock, book)
    assert stored(store, 102) is None


def test_his_lines_reach_only_whom_he_named_where_they_read(store, clock):
    book, _ = rig(store, clock)
    line(store, clock, "guild", HUMAN, "nice one belra", origin="human", audience=CAST)
    line(store, clock, "officer", HUMAN, "coros is useless", origin="human", audience=(102, 104))
    line(store, clock, "guild", HUMAN, "pulling in five", origin="human", audience=CAST)
    hers = post(store, clock, "general", 101, "a post the owner answers")
    post(store, clock, "general", HUMAN, "@Ebbo you're amazing", origin="human", reply_to=hers)
    post(store, clock, "officer-chat", HUMAN, "@Coros piss off", origin="human")
    post(store, clock, "dm:104", HUMAN, "are you kidding me", origin="human")
    later(clock, book)
    assert book.get(102).toward_him > 0.1   # named in guild, warm
    assert book.get(105).toward_him > 0.1   # @-named on Discord
    assert book.get(101).toward_him > 0.1   # he replied to her post
    assert book.get(104).toward_him < -0.2  # his harsh DM
    assert book.get(103).toward_him == 0.0  # named only where she does not read


def test_ignored_counts_once_per_line(store, clock):
    book, _ = rig(store, clock)
    line(store, clock, "whisper", 102, "a whisper left unread", to=HUMAN, audience=(102,))
    line(store, clock, "whisper", 103, "a whisper answered elsewhere", to=HUMAN, audience=(103,))
    post(store, clock, "dm:105", 105, "a dm answered by whisper")
    later(clock, book)
    line(store, clock, "guild", HUMAN, "who is up for heroics", origin="human", audience=CAST)
    post(store, clock, "dm:103", HUMAN, "one sec, reading", origin="human")
    line(store, clock, "whisper", HUMAN, "back in five", to=105, origin="human", audience=(105,))
    for _ in range(40):  # 1240 s: her lines' 1200 s have passed
        later(clock, book)
    assert book.get(102).toward_him < -0.07
    assert book.get(103).toward_him > 0.03 and book.get(105).toward_him > 0.03  # his plain lines
    first = stored(store, 102)
    later(clock, book)
    assert stored(store, 102) == first  # counted once: no new write
    line(store, clock, "whisper", 104, "a whisper while he is away", to=HUMAN, audience=(104,))
    for _ in range(40):  # he writes nowhere at all: nobody is ignored
        later(clock, book)
    assert abs(book.get(104).toward_him) < 0.01


def test_contagion_never_from_private_rows(store, clock):
    book, _ = rig(store, clock)
    for g in (101, 102):
        for _ in range(4):
            book.impulse(g, Impulse.LOOT_WON)
    line(store, clock, "whisper", 101, "a private aside", to=103, audience=(101, 103))
    line(store, clock, "officer", 102, "an officers' aside", audience=(102, 104))
    post(store, clock, "dm:101", 101, "a direct message")
    post(store, clock, "gdm:1", 101, "a group message")
    post(store, clock, "officer-chat", 102, "an officers' post")
    later(clock, book)
    assert all(stored(store, g) is None for g in (103, 104, 105))
    before = {g: book.get(g).valence for g in (103, 104, 105)}
    speaker = book.get(101).valence
    line(store, clock, "guild", 101, "a cheerful guild line", audience=CAST)
    line(store, clock, "guild", 101, "a second cheerful guild line", audience=CAST)
    later(clock, book)
    for g in (103, 104, 105):  # one pull a tick, however many lines
        assert book.get(g).valence == pytest.approx(before[g] + 0.03 * (speaker - before[g]),
                                                     abs=2e-3)
    calm = book.get(103).valence
    post(store, clock, "general", 102, "a cheerful general post")
    later(clock, book)
    assert book.get(103).valence > calm + 0.01


def test_boss_and_loot_events(store, clock):
    book, _ = rig(store, clock)
    base = {g: book.get(g) for g in CAST}
    ms = int(clock.now() * 1000)
    boss = lambda state, members, ts=ms: Boss(ts=ts, map=533, instance_id=7, boss_id=1,
                                              boss_name="Invented Boss", state=state,
                                              members=members)
    run(book.on_event(boss("killed", (101, 102, HUMAN))))
    run(book.on_event(boss("wiped", (103,))))
    run(book.on_event(boss("engaged", (104,))))
    run(book.on_event(LootWon(ts=ms, guid=104, item_id=40000, name="Invented Blade",
                              method="dkp")))
    book.loot_lost(105)
    run(book.on_event(boss("killed", (105,), ts=ms - 3_600_000)))  # replayed an hour late
    v = lambda g: round(book.get(g).valence - base[g].valence, 3)
    assert (v(101), v(102), v(103), v(104), v(105)) == (0.15, 0.15, -0.06, 0.2, -0.12)
    assert round(book.get(101).energy - base[101].energy, 3) == 0.1
    assert store.get_meta(STATE_KEY.format(guid=HUMAN)) is None


def test_switch_off_writes_nothing(store, clock, tmp_path):
    from leeroy_mind.config import load_config
    from leeroy_mind.life.service import LifeService
    world = world_of(store)
    cfg = load_config(tmp_path / "absent.toml")
    life =LifeService(store=store, clock=clock, personas=PersonaCache(store), world=world, cfg=cfg)
    assert life.state is None
    life.tick(clock.now())
    line(store, clock, "whisper", HUMAN, "thanks, you're the best", to=102, origin="human",
         audience=(102,))
    run(clock.advance(31, step=31))
    life.tick(clock.now())
    run(life.on_event(Boss(ts=int(clock.now() * 1000), map=533, instance_id=7, boss_id=1,
                           boss_name="Invented Boss", state="killed", members=(101,))))
    life.loot_lost(103)
    keys = lambda: store.conn.execute(
        "SELECT count(*) FROM meta WHERE key LIKE 'life:state:%'").fetchone()[0]
    assert keys() == 0
    on = dataclasses.replace(cfg, life=dataclasses.replace(cfg.life, state=True))
    life.configure(on)
    assert isinstance(life.state, StateBook)
    life.loot_lost(103)
    assert keys() == 1
    life.configure(cfg)
    assert life.state is None


def test_life_state_cli_reads_only(tmp_path, store, clock, capsys):
    from leeroy_mind.life.cli import life_main
    book, _ = rig(store, clock)
    book.impulse(102, Impulse.LOOT_WON)
    before = store.conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
    assert life_main(["state", "--data-dir", str(tmp_path), "--guid", "102"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("102 now valence=") and " toward_him=" in out[0]
    assert out[0].endswith("s ago") and out[1].startswith("102 baseline valence=")
    assert life_main(["state", "--data-dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [ln.split()[0] for ln in lines[::2]] == [str(g) for g in CAST]
    assert all("stored=none" in ln for ln in lines[::2] if not ln.startswith("102 "))
    assert "Belra" not in "\n".join(lines)
    assert store.conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall() == before
    assert life_main(["state", "--data-dir", str(tmp_path), "--guid", "999"]) == 1


def test_the_app_feeds_her_state(tmp_path, clock):
    from test_app_lifecycle import seed_db
    from leeroy_mind.app import MindApp
    from leeroy_mind.config import load_config
    from leeroy_mind.guild.ports import LivePorts
    from leeroy_mind.testing.fake_llm import FakeLLM
    seed_db(tmp_path / "mind.db", clock)
    (tmp_path / "mind.toml").write_bytes(b"[life]\nstate = true\n")

    async def main():
        cfg = load_config(tmp_path / "mind.toml")
        app = MindApp(cfg, tmp_path, llm=FakeLLM(models={cfg.models.live}), clock=clock)
        try:
            life = app.engine.life
            assert isinstance(life.state, StateBook)
            assert life.on_event in app._handlers["boss"] and life.on_event in app._handlers["loot_won"]
            await app._dispatch_handlers(
                Boss(ts=int(clock.now() * 1000), map=533, instance_id=7, boss_id=1,
                     boss_name="Invented Boss", state="killed", members=(101,)), "boss",
                app._handlers)
            await LivePorts(app).loot_lost(102, "Invented Blade")
            assert app.store.get_meta(STATE_KEY.format(guid=101)) is not None
            assert app.store.get_meta(STATE_KEY.format(guid=102)) is not None
        finally:
            await app.stop()
    run(main())
