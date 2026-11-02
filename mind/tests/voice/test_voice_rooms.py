from conftest import make_persona, run
from leeroy_mind.app import MindApp
from leeroy_mind.config import load_config
from leeroy_mind.protocol import Login
from leeroy_mind.testing.fake_llm import FakeLLM
from leeroy_mind.voice import rooms, wire
from leeroy_mind.voice.comms import roll_on_comms
from leeroy_mind.voice.rooms import ROOMS, RoomState
from voice.conftest import CAST, HUMAN, OFFICERS

OFF = {102, 104}


def state(officers=OFF, human=HUMAN) -> RoomState:
    return RoomState(officers=lambda: set(officers), human=lambda: human)


def test_the_owners_tree():
    assert [(r.id, r.name, r.parent) for r in ROOMS] == [
        ("guild_hall", "Guild Hall", ""), ("raid", "Raid", ""), ("raid2", "Raid 2", "raid"),
        ("officers", "Officers", ""), ("tanks", "Tanks", ""), ("healers", "Healers", ""),
        ("casters", "Casters", ""), ("melee", "Melee", ""),
        ("class_halls", "Class Halls", ""),
        ("warriors", "Warriors", "class_halls"), ("paladins", "Paladins", "class_halls"),
        ("hunters", "Hunters", "class_halls"), ("rogues", "Rogues", "class_halls"),
        ("priests", "Priests", "class_halls"), ("death_knights", "Death Knights", "class_halls"),
        ("shamans", "Shamans", "class_halls"), ("mages", "Mages", "class_halls"),
        ("warlocks", "Warlocks", "class_halls"), ("druids", "Druids", "class_halls"),
        ("loot_dkp", "Loot & DKP", ""), ("lore", "Lore & Strategy", ""),
        ("pvp", "PvP & Arena", ""), ("achievements", "Achievements & Mounts", ""),
        ("kitchen", "The Kitchen", ""), ("irl", "IRL", ""), ("smoke_room", "The Smoke Room", ""),
        ("afk", "AFK", ""), ("private1", "Private 1", ""), ("private2", "Private 2", ""),
        ("private3", "Private 3", ""), ("interview", "Interview Room", ""),
        ("penalty_box", "Penalty Box", "")]
    assert {r.id: r.capacity for r in ROOMS if r.capacity} == {
        "private1": 3, "private2": 3, "private3": 3}
    assert rooms.PRIVATE == ("private1", "private2", "private3")
    assert rooms.RAID_ROOMS == ("raid", "raid2")
    assert rooms.CLASS_ROOM == {1: "warriors", 2: "paladins", 3: "hunters", 4: "rogues",
                                5: "priests", 6: "death_knights", 7: "shamans", 8: "mages",
                                9: "warlocks", 11: "druids"}
    assert len([r for r in ROOMS if r.parent == "class_halls"]) == 10


def test_officers_room():
    s = state()
    assert s.can_enter(102, "officers") is None
    assert s.can_enter(101, "officers") == "locked"
    assert s.can_enter(HUMAN, "officers") is None
    assert s.can_enter(101, "nowhere") == "unknown"


def test_private_lock():
    s = state()
    s.lock("private1", {1, 102})
    assert s.can_enter(103, "private1") == "locked"
    assert s.can_enter(102, "private1") is None
    s.unlock("private1")
    assert s.can_enter(103, "private1") is None
    for g in (101, 102, 103):
        s.move(g, "private1")
    assert s.can_enter(104, "private1") == "full"
    assert s.can_enter(101, "private1") is None  # already there
    assert s.free_private() == "private2"


def test_move_and_moves():
    s = state()
    assert s.move(102, "guild_hall") is None
    assert s.move(101, "guild_hall") is None
    assert s.move(HUMAN, "guild_hall") is None
    assert s.move(101, "raid", by=104) == "guild_hall"
    assert s.human_room() == "guild_hall" and s.occupants("raid") == {101}
    assert s.moves() == [wire.Moved(101, "raid", 104), wire.Moved(102, "guild_hall", 0),
                         wire.Moved(HUMAN, "guild_hall", 0)]
    s.leave(101)
    assert 101 not in s.where


def test_channels_message():
    s = state()
    msg = s.channels_message()
    assert [r.id for r in msg.channels] == [r.id for r in ROOMS]
    off = next(r for r in msg.channels if r.id == "officers")
    assert off.allowed == tuple(sorted(OFF | {HUMAN})) and off.locked is True
    hall = next(r for r in msg.channels if r.id == "guild_hall")
    assert hall.locked is False and hall.allowed == ()
    assert next(r for r in msg.channels if r.id == "private2").capacity == 3
    wire.encode(msg, 1)


def _roll(night, personas, spec=lambda g: True, officer=lambda g: False):
    return roll_on_comms(night, personas, raider_share=0.9, social_share=0.6,
                         is_officer=officer, has_voice=spec)


def test_roll_is_seeded():
    raider = make_persona(1, "R", guild_role="raider")
    social = make_persona(1, "S", guild_role="social")
    officer = make_persona(1, "O", guild_role="officer")
    people = {**{g: raider for g in range(1000, 2000)},
              **{g: social for g in range(2000, 3000)}, 5: officer}
    a = _roll("2026-10-30", people)
    assert a == _roll("2026-10-30", people)
    assert a != _roll("2026-10-31", people)
    assert 5 in a
    assert abs(len([g for g in a if 1000 <= g < 2000]) / 1000 - 0.9) <= 0.05
    assert abs(len([g for g in a if 2000 <= g < 3000]) / 1000 - 0.6) <= 0.05
    assert _roll("2026-10-30", people, spec=lambda g: g != 5 and g < 2000) \
        .isdisjoint({5, *range(2000, 3000)})
    assert 7 in _roll("n", {7: raider, 8: raider}, officer=lambda g: g == 7)


# -- the session (login, restart, logout)

def test_login_seats_everyone(vapp):
    run(vapp.login())
    seated = len(vapp.ext.rooms.where)
    assert vapp.types() == ["session", "channels"] + ["voice"] * 5 + ["moved"] * seated + ["raid"]
    first = vapp.sent[0]
    assert first.online is True and first.human_guid == HUMAN
    moved = [m for m in vapp.sent if isinstance(m, wire.Moved)]
    assert moved[-1] == wire.Moved(HUMAN, "guild_hall", 0)
    assert {m.channel for m in moved} == {"guild_hall"}
    assert set(OFFICERS) <= {m.guid for m in moved}  # officers are always on comms
    assert {m.guid for m in moved} <= set(CAST) | {HUMAN}
    assert vapp.ext.rooms.human_room() == "guild_hall"
    open_rows = vapp.rows("SELECT guid FROM voice_presence WHERE until IS NULL")
    assert {r["guid"] for r in open_rows} == {m.guid for m in moved}
    assert vapp.rows("SELECT DISTINCT session_id FROM voice_presence")[0][0] \
        == vapp.app.session_id


def test_restart_restores_seats(vapp):
    run(vapp.login())
    sid = vapp.app.session_id
    ext = vapp.ext
    now = vapp.clock.now()
    ext.store.enter(102, "raid", 104, now, sid)  # moved before the "restart"
    seats = ext.store.open_presence(sid)
    assert seats[102] == ("raid", 104)
    ext.rooms.clear()  # a new mind process over the same db: nothing seated in memory
    ext.on_comms, ext._seated_sid = set(), None
    vapp.sent.clear()
    before = vapp.rows("SELECT count(*) FROM voice_presence")[0][0]
    run(vapp.hello())  # hello with him online: the same session resumes
    assert vapp.app.session_id == sid
    assert ext.rooms.where == {g: r for g, (r, _) in seats.items()}
    assert vapp.types()[:2] == ["session", "channels"] and vapp.types()[-1] == "raid"
    assert wire.Moved(102, "raid", 104) in vapp.sent
    assert vapp.rows("SELECT count(*) FROM voice_presence")[0][0] == before  # no fresh seating


def test_logout_clears(vapp):
    run(vapp.login())
    vapp.sent.clear()
    run(vapp.logout())
    assert vapp.rows("SELECT count(*) FROM voice_presence WHERE until IS NULL")[0][0] == 0
    assert vapp.rows("SELECT count(*) FROM voice_presence")[0][0] > 0
    assert vapp.ext.rooms.where == {}
    assert vapp.types() == ["session"] and vapp.sent[0].online is False


def test_a_failing_hook_does_not_escape(vapp, caplog):
    def boom():
        raise RuntimeError("hook")
    vapp.ext.rooms.moves = boom
    with caplog.at_level("WARNING"):
        run(vapp.login())  # no exception reaches the app
    assert any("voice_handler_failed" in r.getMessage() for r in caplog.records)


def test_spec_missing_is_counted(vapp, caplog):
    (vapp.app.data_dir / "voices" / "103.json").unlink()
    with caplog.at_level("INFO"):
        run(vapp.login())
    assert 103 not in vapp.ext.rooms.where
    assert any("voice_spec_missing" in r.getMessage() and "count=1" in r.getMessage()
               for r in caplog.records)


def test_disabled_does_nothing(tmp_path, clock):
    (tmp_path / "mind.toml").write_text("", encoding="utf-8")
    app = MindApp(load_config(tmp_path / "mind.toml"), tmp_path, llm=FakeLLM(), clock=clock)
    assert app.voice.store is None and app._handlers.get("login", []) == []
    run(app.handle(Login(1, 1, True)))
    assert app.voice.rooms.where == {}


def _clean_restart(vapp):
    """What a clean mind stop and start does to the session: it ends, the process is new."""
    app, ext = vapp.app, vapp.ext
    app.store.end_session(app.session_id, app.clock.now())
    app._session_id = None
    ext.rooms.clear()
    ext.on_comms, ext._seated_sid = set(), None


def test_stop_closes_rows_at_the_stop_and_a_start_restores(vapp):
    run(vapp.login())
    ext, app = vapp.ext, vapp.app
    ext.store.enter(102, "raid", 104, app.clock.now(), app.session_id)
    ext.rooms.move(102, "raid", 104)
    run(app.clock.advance(60))
    stop_at = app.clock.now()
    ext.shutdown()
    assert vapp.rows("SELECT count(*) FROM voice_presence WHERE until IS NULL")[0][0] == 0
    assert {r[0] for r in vapp.rows("SELECT until FROM voice_presence")} == {stop_at - 60, stop_at}
    _clean_restart(vapp)
    run(app.clock.advance(1200, 600))  # the mind was down; he is back within the evening
    start_at = app.clock.now()
    vapp.sent.clear()
    run(vapp.login())
    assert ext.rooms.where[102] == "raid" and ext.rooms.human_room() == "guild_hall"
    assert wire.Moved(102, "raid", 104) in vapp.sent
    new = vapp.rows("SELECT since FROM voice_presence WHERE until IS NULL")
    assert {r[0] for r in new} == {start_at}  # no down-time in any row
    assert max(r[0] for r in vapp.rows("SELECT until FROM voice_presence "
                                       "WHERE until IS NOT NULL")) == stop_at


def test_a_stale_stop_is_not_restored(vapp):
    run(vapp.login())
    run(vapp.app.clock.advance(1))
    vapp.ext.store.enter(102, "raid", 104, vapp.app.clock.now(), vapp.app.session_id)
    vapp.ext.rooms.move(102, "raid", 104)
    vapp.ext.shutdown()
    _clean_restart(vapp)
    run(vapp.app.clock.advance(4000, 1000))
    run(vapp.login())
    assert vapp.ext.rooms.where.get(102) != "raid"


def test_cancelling_serve_closes_the_rows(vapp):
    import asyncio

    async def go():
        await vapp.login()
        task = asyncio.create_task(vapp.ext._serve())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    run(go())
    assert vapp.rows("SELECT count(*) FROM voice_presence WHERE until IS NULL")[0][0] == 0


def test_restore_drops_a_member_who_left_the_cast(vapp):
    run(vapp.login())
    app, ext = vapp.app, vapp.ext
    ext.store.enter(103, "raid", 0, app.clock.now(), app.session_id)
    stay = [m for g, m in app.store.cast().items() if g != 103]
    app.store.replace_cast(stay, app.clock.now(), keep=(HUMAN,))
    ext.rooms.clear()
    ext.on_comms, ext._seated_sid = set(), None
    vapp.sent.clear()
    run(vapp.hello())
    assert 103 not in ext.rooms.where and all(m.guid != 103 for m in vapp.sent
                                              if isinstance(m, wire.Moved))
    assert vapp.rows("SELECT count(*) FROM voice_presence "
                     "WHERE guid = 103 AND until IS NULL")[0][0] == 0
