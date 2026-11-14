"""The console's Raid and Guild tabs and its side of the mind's control endpoint: the read-only
questions to mind.db (raidview), the routes and their gates, the endpoint located and checked
before its key is sent (mindlink), the raid actions forwarded, and the clean stop with the
forced one as its fallback. Scratch data, fake links and fake processes only."""
import asyncio
import contextlib
import hashlib
import json
import os
import sqlite3
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind import lifecycle
from leeroy_mind.console import feed, raidview
from leeroy_mind.console.control import Controls, MindHandle
from leeroy_mind.console.mindlink import LinkError, MindLink
from leeroy_mind.console.netinfo import ESTABLISHED, LISTEN, Conn
from leeroy_mind.endpoint import KEY_FILE, ControlCfg, ControlEndpoint, write_key_file
from leeroy_mind.guild.dkp import LedgerEntry
from leeroy_mind.guild.loot import _put
from leeroy_mind.guild.store import add_ledger, end_raid, new_raid, record_attendance
from leeroy_mind.protocol import Member
from console.rig import events, http, make_app, running
from console.test_lifecycle_shared import Clock, Processes, mind as mind_rows  # noqa: F401

NOW = 1_793_304_000.0  # 2026-10-29 20:00 UTC
MIND_KEY = "M" * 43


@pytest.fixture
def data(store):
    return Path(store.conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def _persona(store, guid, *, guild_role="raider", title="", raid_role="melee"):
    store.conn.execute(
        "INSERT INTO persona_core(guid, json, card, card_tokens, version, source, updated) "
        "VALUES (?, ?, '', 0, 1, 'seed', 0)",
        (guid, json.dumps({"play": {"guild_role": guild_role, "officer_title": title,
                                    "raid_role": raid_role, "main_spec": "x", "skill": "solid"},
                           "secret": "never read by the console"})))
    store.conn.commit()


def _member(guid, name, cls, spec_tab, *, online=True, rank=3, level=80):
    return Member(guid=guid, name=name, race=2, class_=cls, gender=0, level=level, rank=rank,
                  online=online, map=631, zone=4812, spec_tab=spec_tab)


@pytest.fixture
def guild(store):
    """A synthetic guild: a lead tank, a loot officer healer, two damage dealers, the owner (1),
    one ended raid with a kill, an MVP and an awarded item, one open raid with a pending drop,
    a season award and a chronicle entry."""
    store.set_meta("human_guid", "1")
    store.upsert_cast([_member(1, "Owner", 8, 0, rank=0), _member(201, "Tanka", 1, 2),
                       _member(202, "Healy", 5, 1), _member(203, "Stabby", 4, 1),
                       _member(204, "Zappy", 8, 2, online=False)], NOW - 3600)
    _persona(store, 201, guild_role="officer", title="Raid Lead", raid_role="tank")
    _persona(store, 202, guild_role="officer", title="Loot, DKP", raid_role="healer")
    _persona(store, 203)
    _persona(store, 204, raid_role="ranged")
    conn = store.conn
    old = new_raid(conn, started=NOW - 86400, raid_key="naxx", size=10, heroic=False,
                   offline=True, lead_guid=201, tank_guid=201, loot_guid=202)
    record_attendance(conn, old, [201, 202, 203], [204])
    add_ledger(conn, [LedgerEntry(NOW - 86400, g, 5, "attendance", old) for g in (201, 202, 203)]
               + [LedgerEntry(NOW - 86400, 204, 2, "standby", old)]
               + [LedgerEntry(NOW - 86000, g, 10, "kill", old) for g in (201, 202, 203)]
               + [LedgerEntry(NOW - 85900, 203, -7, "item", old, 40001, f"guild:loot:{old}:aa")]
               + [LedgerEntry(NOW - 3000, g, -1.5, "decay") for g in (201, 202)])
    with conn:
        _put(conn, f"guild:loot:{old}:aa", {"state": "charged", "name": "Synthetic Blade",
                                             "item_id": 40001, "price": 7, "ordinal": 1,
                                             "award": {"guid": 203, "paid": 7.0, "close": False},
                                             "bidders": [{"guid": 203, "gain": 4}]})
        _put(conn, f"guild:raid:{old}:mvp", {"done": True, "names": {"203": "Stabby"},
                                             "result": {"winner": 203, "votes": 2}})
    end_raid(conn, old, ended=NOW - 85000, kills=1, wipes=2, outcome="complete")
    now_id = new_raid(conn, started=NOW - 600, raid_key="icc", size=25, heroic=True,
                      offline=False, lead_guid=201, tank_guid=201, loot_guid=202)
    record_attendance(conn, now_id, [1, 201, 202, 203], [204])
    add_ledger(conn, [LedgerEntry(NOW - 600, g, 5, "attendance", now_id) for g in (1, 201, 202, 203)])
    with conn:
        _put(conn, f"guild:raid:{now_id}:plan", {
            "raid_key": "icc", "size": 25, "heroic": True, "offline": False, "lead_guid": 201,
            "tank_guid": 201, "loot_guid": 202, "members": [201, 202, 203], "bench": [204],
            "human_in_raid": True})
        _put(conn, f"guild:raid:{now_id}:instance", 77)
        _put(conn, f"guild:loot:{now_id}:bb", {"state": "pending", "name": "Synthetic Helm",
                                                "item_id": 50002, "price": 30, "ordinal": 1,
                                                "human_claimed": True, "received": NOW - 60})
        conn.execute("INSERT INTO award(season, title, guid, value) VALUES "
                     "('2026-S5', 'Loot Goblin', 203, 3)")
        conn.execute("INSERT INTO chronicle(week, era, author, text, evidence, created) VALUES "
                     "('2026-W43', 'The Frost Era', 201, 'A synthetic week of raiding.', '[]', ?)",
                     (NOW - 7200,))
    return store, old, now_id


def _digest(path: Path) -> str:
    return "".join(hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(path.parent.glob(path.name + "*")))


# -- the questions -------------------------------------------------------------------------------

def test_the_raid_view_shows_the_open_raid_its_loot_dkp_and_the_recent_ones(guild, data):
    store, old, now_id = guild
    view = feed.read(data / "mind.db", raidview.raid_view, now=NOW)
    assert view["human"] == 1
    open_ = view["open"]
    assert (open_["raid_id"], open_["name"], open_["size"], open_["heroic"]) == (
        now_id, "Icecrown Citadel", 25, True)
    assert (open_["lead"], open_["tank"], open_["loot"], open_["bosses"]) == (201, 201, 202, 12)
    assert open_["members"] == [201, 202, 203] and open_["bench"] == [204]
    assert open_["human_in_raid"] and open_["instance"]
    assert open_["loot_items"] == [{"ordinal": 1, "name": "Synthetic Helm", "item_id": 50002,
                                    "price": 30, "state": "pending", "winner": None, "paid": None,
                                    "close": False, "runner_up": None, "claimed": True,
                                    "received": NOW - 60}]
    assert open_["dkp"]["totals"] == {"attendance": {"n": 4, "total": 20.0}}
    assert open_["dkp"]["events"] == [{"ts": NOW - 600, "reason": "attendance", "n": 4,
                                       "total": 20.0, "each": 5.0, "guid": None, "item_id": None,
                                       "item": None}]
    [recent] = view["recent"]
    assert (recent["raid_id"], recent["offline"], recent["kills"], recent["wipes"]) == (old, True, 1, 2)
    assert recent["mvp"] == {"guid": 203, "votes": 2} and recent["items"] == 1
    assert recent["raided"] == 3 and recent["outcome"] == "complete"
    people = view["people"]
    assert people["201"]["role"] == "tank" and people["202"]["role"] == "healer"
    assert people["204"]["online"] is False and people["201"]["spec"] == "protection"
    assert people["202"]["guild_role"] == "officer" and people["202"]["officer_title"] == "Loot, DKP"
    assert "secret" not in json.dumps(view)  # only the play section of a persona is read
    assert [o["guid"] for o in view["officers"]] == [202, 201]  # by name
    assert view["balances"]["203"] == 13.0 and view["quiet_night"] is False
    assert {c["key"] for c in view["catalog"]} >= {"icc", "toc", "naxx"}


def test_the_guild_view_shows_roster_ladder_loot_awards_and_chronicle(guild, data):
    store, old, now_id = guild
    view = feed.read(data / "mind.db", raidview.guild_view)
    assert [p["guid"] for p in view["roster"]] == [1, 202, 203, 201, 204]  # rank, then name
    ladder = {e["guid"]: e for e in view["ladder"]}
    assert [e["guid"] for e in view["ladder"]][:3] == [201, 202, 203]
    assert ladder[203] == {"guid": 203, "balance": 13.0, "earned": 20.0, "spent": 7.0,
                           "decay": 0.0, "adjust": 0.0, "items": 1, "last": NOW - 600,
                           "raids": 2, "benched": 0}
    assert ladder[204]["benched"] == 2 and ladder[201]["decay"] == -1.5
    assert view["stats"] == {"raids": 1, "offline": 1, "kills": 1, "wipes": 2,
                             "first": NOW - 86400, "awarded": 67.0, "spent": 7.0,
                             "decayed": 3.0, "items": 1}
    assert view["loot"] == [{"ts": NOW - 85900, "guid": 203, "paid": 7.0, "item_id": 40001,
                             "raid_id": old, "raid": "naxx", "size": 10, "heroic": False,
                             "item": "Synthetic Blade"}]
    assert view["awards"] == [{"season": "2026-S5", "title": "Loot Goblin", "guid": 203, "value": 3.0}]
    assert view["chronicle"] == [{"week": "2026-W43", "era": "The Frost Era", "author": 201,
                                  "text": "A synthetic week of raiding.", "created": NOW - 7200}]


def test_the_questions_never_write(guild, data):
    store, _, _ = guild
    store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = _digest(data / "mind.db")
    feed.read(data / "mind.db", raidview.raid_view, now=NOW)
    feed.read(data / "mind.db", raidview.guild_view)
    assert _digest(data / "mind.db") == before


def test_a_run_that_never_raided_shows_as_not_raided_and_is_not_counted(guild, data):
    store, old, _ = guild
    conn = store.conn
    skipped = new_raid(conn, started=NOW - 7000, raid_key="icc", size=25, heroic=False,
                       offline=True, lead_guid=201, tank_guid=201, loot_guid=202)
    with conn:
        _put(conn, f"guild:raid:{skipped}:not-raided", True)
    end_raid(conn, skipped, ended=NOW - 6999, kills=0, wipes=0, outcome="setup:starting:no_fresh_encounters")
    view = feed.read(data / "mind.db", raidview.raid_view, now=NOW)
    recent = {r["raid_id"]: r for r in view["recent"]}
    assert recent[skipped]["not_raided"] is True and recent[skipped]["raided"] == 0
    assert recent[old]["not_raided"] is False
    stats = feed.read(data / "mind.db", raidview.guild_view)["stats"]
    assert (stats["raids"], stats["offline"]) == (1, 1)


def test_a_quiet_night_and_an_empty_or_older_database(store, data, tmp_path):
    with store.conn:
        _put(store.conn, "guild:raid-night:quiet-night", "2026-10-29")
    view = feed.read(data / "mind.db", raidview.raid_view, now=NOW, tz=timezone.utc)
    assert view["quiet_night"] is True and view["open"] is None and view["recent"] == []
    bare = tmp_path / "bare"
    bare.mkdir()
    sqlite3.connect(bare / "mind.db").close()  # a database without any of the tables
    assert feed.read(bare / "mind.db", raidview.raid_view, now=NOW)["people"] == {}
    empty = feed.read(bare / "mind.db", raidview.guild_view)
    assert (empty["roster"], empty["ladder"], empty["loot"], empty["awards"]) == ([], [], [], [])
    assert empty["stats"]["raids"] == 0


# -- the mind's endpoint, located and checked ------------------------------------------------------

def _listening(pid, port, *, local="127.0.0.1", state=LISTEN):
    return [Conn(local, port, "0.0.0.0", 0, state, pid)]


def _key_file(data, pid=4242, port=40000):
    write_key_file(data / KEY_FILE, {"v": 1, "pid": pid, "port": port, "key": MIND_KEY})


def test_the_key_goes_only_to_the_running_minds_own_listener(tmp_path):
    sent = []

    def request(port, key, op, args=None, *, timeout):
        sent.append((port, key, op, args))
        return {"ok": True}
    table = [None]
    link = MindLink(tmp_path, tcp=lambda: table[0], request=request)
    cases = [((), "the mind is not running"),
             ((4242,), "has no control endpoint")]
    for pids, words in cases:
        with pytest.raises(LinkError, match=words):
            link.call(pids, "status")
    _key_file(tmp_path)
    with pytest.raises(LinkError, match="not the running mind's"):
        link.call((99, 98), "status")
    with pytest.raises(LinkError, match="cannot be verified here"):
        link.call((4242,), "status")
    for conns in (_listening(4242, 40001), _listening(4243, 40000),
                  _listening(4242, 40000, local="0.0.0.0"),
                  _listening(4242, 40000, state=ESTABLISHED), []):
        table[0] = conns
        with pytest.raises(LinkError, match="not listening where control.json says"):
            link.call((4242, 4241), "status")
    assert sent == []  # the key went nowhere
    table[0] = _listening(4242, 40000) + _listening(7, 7)
    assert link.call((4242, 4241), "status") == {"ok": True}
    assert sent == [(40000, MIND_KEY, "status", None)]


def test_an_endpoint_that_does_not_answer_is_a_link_error(tmp_path):
    _key_file(tmp_path)
    for error in (ConnectionRefusedError("x"), TimeoutError("x"), ValueError("not json")):
        def request(*a, error=error, **kw):
            raise error
        link = MindLink(tmp_path, tcp=lambda: _listening(4242, 40000), request=request)
        with pytest.raises(LinkError, match=f"did not answer \\({type(error).__name__}\\)"):
            link.call((4242,), "stop")


def test_the_link_talks_to_a_real_endpoint(tmp_path):
    class App:
        data_dir, stopping, guild = tmp_path, False, None

    async def go():
        ep = ControlEndpoint(App(), ControlCfg(), key=MIND_KEY)
        task = asyncio.create_task(ep.serve())
        await asyncio.wait_for(ep.listening.wait(), 5)
        try:
            link = MindLink(tmp_path, tcp=lambda: _listening(os.getpid(), ep.port))
            reply = await asyncio.to_thread(link.call, (os.getpid(),), "status")
            assert reply["ok"] is True and reply["raid"] is None
            reply = await asyncio.to_thread(link.call, (os.getpid(),), "raid", {"action": "end"})
            assert reply == {"ok": False, "error": "guild life is not running in this mind"}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    asyncio.run(go())


# -- the routes ----------------------------------------------------------------------------------

class FakeLink:
    def __init__(self, reply=None, *, error=None):
        self.calls, self.reply, self.error = [], reply, error

    def locate(self, pids):
        if self.error:
            raise LinkError(self.error)
        return {"pid": 101, "port": 1, "key": "x"}

    def call(self, pids, op, args=None, *, timeout=3.0):
        self.calls.append((tuple(pids), op, args))
        if self.error:
            raise LinkError(self.error)
        return self.reply(op, args) if callable(self.reply) else self.reply


STATUS = {"ok": True, "v": 1, "pid": 101, "stopping": False,
          "raid": {"state": "running", "actions": {"stop": None}, "run": {"raid": "icc"}}}


@contextlib.asynccontextmanager
async def served(app):
    await app.start()
    try:
        yield app
    finally:
        await app.close()


def test_the_tabs_need_the_key_and_show_mind_db(guild, data):
    async def go():
        async with served(make_app(data, mind=running(data), link=FakeLink(STATUS))) as app:
            for path in ("/api/raid", "/api/guild"):
                assert (await http(app, "GET", path, key=False)).status == 403
                assert (await http(app, "GET", path, origin="http://127.0.0.1:1")).status == 403
            r = await http(app, "GET", "/api/raid")
            assert r.status == 200
            body = r.json()
            assert body["view"]["open"]["name"] == "Icecrown Citadel"
            assert body["mind"] == {"ok": True, "stopping": False, "raid": STATUS["raid"], "world": None}
            assert app.link.calls == [((101, 100), "status", None)]
            g = await http(app, "GET", "/api/guild")
            assert g.status == 200 and g.json()["stats"]["raids"] == 1
    asyncio.run(go())


def test_the_raid_tab_says_why_it_has_no_live_state(guild, data):
    async def go():
        async with served(make_app(data, link=FakeLink(STATUS))) as app:  # the mind is stopped
            body = (await http(app, "GET", "/api/raid")).json()
            assert body["mind"] == {"ok": False, "error": "the mind is stopped"}
            assert app.link.calls == []
        link = FakeLink(error="the mind has no control endpoint (it is off in mind.toml, ...)")
        async with served(make_app(data, mind=running(data), link=link)) as app:
            body = (await http(app, "GET", "/api/raid")).json()
            assert body["mind"]["ok"] is False and "no control endpoint" in body["mind"]["error"]
            assert body["view"]["open"] is not None  # mind.db is still shown
            health = (await http(app, "GET", "/api/health")).json()
            assert health["endpoint"] == {"ok": False, "detail": link.error}
        async with served(make_app(data, mind=running(data), link=FakeLink(
                {"ok": False, "error": "forbidden"}))) as app:
            assert (await http(app, "GET", "/api/raid")).json()["mind"] == {
                "ok": False, "error": "forbidden"}
    asyncio.run(go())


def test_a_raid_action_needs_the_key_the_origin_and_json(guild, data):
    link = FakeLink({"ok": True, "message": "summoning 2 late members now"})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            body = {"action": "summon"}
            assert (await http(app, "POST", "/api/raid", body=body)).status == 403
            assert (await http(app, "POST", "/api/raid", body=body, key=False,
                               origin=True)).status == 403
            assert (await http(app, "POST", "/api/raid", body=b"action=summon", origin=True,
                               headers={"Content-Type": "text/plain"})).status == 415
            assert link.calls == []
            r = await http(app, "POST", "/api/raid", body=body, origin=True)
            assert r.status == 200 and r.json() == {"ok": True,
                                                    "message": "summoning 2 late members now"}
            assert link.calls == [((101, 100), "raid", {"action": "summon"})]
    asyncio.run(go())


@pytest.mark.parametrize("body", [
    {"action": "disband"}, {"action": "end", "confirm": True}, {"action": "form", "raid": "icc"},
    {"action": "form", "raid": "naxx", "size": 25, "heroic": True, "lead": None}, ["end"]])
def test_a_raid_action_not_in_the_endpoints_exact_form_never_leaves_the_console(guild, data, body):
    link = FakeLink({"ok": True, "message": "x"})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            r = await http(app, "POST", "/api/raid", body=body, origin=True)
            assert r.status == 400  # an object of another shape is answered in JSON, too
            assert isinstance(body, list) or r.json()["ok"] is False
    asyncio.run(go())
    assert link.calls == []


def test_raid_actions_are_refused_with_control_off_while_busy_or_without_the_mind(guild, data):
    async def go():
        off = Controls(enabled=False, reason="process control is off (--no-control)",
                       mind=running(data))
        link = FakeLink({"ok": True, "message": "x"})
        async with served(make_app(data, mind=running(data), controls=off, link=link)) as app:
            r = await http(app, "POST", "/api/raid", body={"action": "end"}, origin=True)
            assert r.status == 403 and "--no-control" in r.json()["error"]
            # status is still read with control off: it changes nothing
            assert (await http(app, "GET", "/api/raid")).json()["mind"]["ok"] is True
        assert [c[1] for c in link.calls] == ["status"]
        async with served(make_app(data, mind=running(data), link=link)) as app:
            app.controls.busy = ("mind", "restart")
            r = await http(app, "POST", "/api/raid", body={"action": "end"}, origin=True)
            assert r.status == 409 and r.json()["error"] == "a restart of the mind is still running"
        async with served(make_app(data, link=link)) as app:
            r = await http(app, "POST", "/api/raid", body={"action": "end"}, origin=True)
            assert r.status == 409 and r.json()["error"] == (
                "the mind is stopped: raid actions need it running")
        async with served(make_app(data, mind=running(data))) as app:  # no link at all
            r = await http(app, "POST", "/api/raid", body={"action": "end"}, origin=True)
            assert r.status == 409 and r.json()["error"] == "no control endpoint here"
        assert [c[1] for c in link.calls] == ["status"]
    asyncio.run(go())


def test_the_minds_refusal_or_silence_comes_back_and_a_success_is_announced(guild, data):
    async def go():
        refusing = FakeLink({"ok": False, "error": "you're not in game: guild life runs raid "
                                                   "nights only while you are"})
        async with served(make_app(data, mind=running(data), link=refusing)) as app:
            r = await http(app, "POST", "/api/raid", body={"action": "call"}, origin=True)
            assert r.status == 409 and r.json()["error"].startswith("you're not in game")
        silent = FakeLink(error="the control endpoint did not answer (TimeoutError)")
        async with served(make_app(data, mind=running(data), link=silent)) as app:
            r = await http(app, "POST", "/api/raid", body={"action": "call"}, origin=True)
            assert r.status == 409 and "did not answer" in r.json()["error"]
        form = {"action": "form", "raid": "icc", "size": None, "heroic": True, "lead": "me"}
        ok = FakeLink({"ok": True, "message": "forming Icecrown Citadel heroic: ..."})
        async with served(make_app(data, mind=running(data), link=ok)) as app:
            listen = asyncio.create_task(events(app, count=2))
            await asyncio.sleep(0.2)
            r = await http(app, "POST", "/api/raid", body=form, origin=True)
            assert r.status == 200
            kinds = await listen
            assert kinds[1] == ("raid", {"action": "form",
                                         "message": "forming Icecrown Citadel heroic: ..."})
            assert ok.calls[-1] == ((101, 100), "raid", form)
    asyncio.run(go())


# -- the clean stop ------------------------------------------------------------------------------

class StopLink:
    """The mind's endpoint: a stop takes the family away after `ends_after` lock probes."""
    def __init__(self, processes, *, reply=None, error=None, ends=True):
        self.processes, self.reply, self.error, self.ends = processes, reply, error, ends
        self.calls = []

    def call(self, pids, op, args=None, *, timeout=3.0):
        self.calls.append((tuple(pids), op))
        if self.error:
            raise LinkError(self.error)
        if self.ends:
            self.processes.rows = []  # its own shutdown ran, and the process ended
        return self.reply or {"ok": True, "stopping": True}


def _handle(m, processes, link, **kw):
    clock = Clock()
    return MindHandle(m.root, m.data, python=m.python, base_python=str(m.base),
                      processes=processes, locked=lambda _: bool(processes.rows),
                      spawn=kw.pop("spawn", lambda *a: pytest.fail("unexpected spawn")),
                      sleep=clock.sleep, monotonic=clock.monotonic, timeout=1, link=link, **kw)


def test_a_stop_is_the_minds_own_shutdown_when_its_endpoint_answers(mind_rows):
    processes = Processes([mind_rows.launcher, mind_rows.child])
    link = StopLink(processes)
    out = _handle(mind_rows, processes, link).act("stop")
    assert (out.code, out.state) == (0, "stopped")
    assert out.message == "mind stopped cleanly (its own shutdown, as Ctrl+C runs it)"
    assert link.calls == [((101, 100), "stop")]
    assert processes.killed == [] and processes.opened == []  # nothing was terminated


@pytest.mark.parametrize("how, words", [
    ("no_link", "no control endpoint here"),
    ("link_error", "the mind has no control endpoint"),
    ("refused", "its control endpoint refused the stop"),
    ("never_ends", "it did not finish its clean stop within 60 s"),
])
def test_without_a_usable_endpoint_the_stop_is_forced_and_says_why(mind_rows, how, words):
    processes = Processes([mind_rows.launcher, mind_rows.child])
    link = {"no_link": None,
            "link_error": StopLink(processes, error="the mind has no control endpoint (...)"),
            "refused": StopLink(processes, reply={"ok": False, "error": "forbidden"}, ends=False),
            "never_ends": StopLink(processes, ends=False)}[how]
    out = _handle(mind_rows, processes, link).act("stop")
    assert (out.code, out.state) == (0, "stopped")
    assert out.message.startswith("mind stopped by force (") and words in out.message
    assert processes.killed == [(101, 1120), (100, 1110)]


def test_a_clean_restart_starts_it_again_and_an_ambiguous_family_is_refused(mind_rows):
    processes = Processes([mind_rows.launcher, mind_rows.child])
    launches = []

    def spawn(root, data, python):
        processes.rows = [mind_rows.launcher, mind_rows.child]
        launches.append(python)
        return SimpleNamespace(pid=100, poll=lambda: None)
    link = StopLink(processes)
    out = _handle(mind_rows, processes, link, spawn=spawn).act("restart")
    assert out.state == "running" and launches == [mind_rows.python] and processes.killed == []
    other = lifecycle.Process(201, 20, 2, mind_rows.launcher.exe, mind_rows.launcher.argv)
    processes.rows = [mind_rows.launcher, mind_rows.child, other]
    with pytest.raises(lifecycle.OwnershipError, match="multiple"):
        _handle(mind_rows, processes, StopLink(processes)).act("stop")
    processes.rows = []
    assert _handle(mind_rows, Processes(), StopLink(Processes())).act("stop").message == (
        "mind already stopped")


def test_a_busy_endpoint_is_asked_again_before_the_stop_is_forced(mind_rows):
    processes = Processes([mind_rows.launcher, mind_rows.child])
    busy = LinkError("the control endpoint did not answer (ConnectionResetError)", unanswered=True)

    class Busy(StopLink):
        def __init__(self, failures):
            super().__init__(processes)
            self.failures = failures

        def call(self, pids, op, args=None, *, timeout=3.0):
            if self.failures:
                self.failures -= 1
                self.calls.append((tuple(pids), op))
                raise busy
            return super().call(pids, op, args, timeout=timeout)
    link = Busy(2)  # its slots were taken twice, then it answered
    out = _handle(mind_rows, processes, link).act("stop")
    assert out.message.startswith("mind stopped cleanly") and len(link.calls) == 3
    assert processes.killed == []
    processes.rows = [mind_rows.launcher, mind_rows.child]
    link = Busy(99)  # never answers
    out = _handle(mind_rows, processes, link).act("stop")
    assert out.message.startswith("mind stopped by force (the control endpoint did not answer")
    assert len(link.calls) == 5 and processes.killed == [(101, 1120), (100, 1110)]


def test_the_raid_lead_picker_goes_to_the_minds_raidlead_op(guild, data):
    lead = {"lead": 101, "source": "default", "his": False, "assistant": True, "raid": True,
            "blocked": None, "candidates": [{"guid": 101, "name": "Arkon", "officer": True, "online": True}]}
    link = FakeLink(lambda op, args: {**STATUS, "raid_lead": lead} if op == "status"
                    else {"ok": True, "message": "Arkon leads your group's raid now."})

    async def go():
        async with served(make_app(data, mind=running(data), link=link)) as app:
            assert (await http(app, "GET", "/api/raid")).json()["mind"]["raid_lead"] == lead
            body = {"action": "set", "guid": 101}
            assert (await http(app, "POST", "/api/raidlead", body=body)).status == 403  # no Origin
            r = await http(app, "POST", "/api/raidlead", body={"action": "set", "guid": "101"}, origin=True)
            assert r.status == 400
            r = await http(app, "POST", "/api/raidlead", body=body, origin=True)
            assert r.status == 200 and r.json()["message"] == "Arkon leads your group's raid now."
            assert link.calls[-1] == ((101, 100), "raidlead", body)
        off = Controls(enabled=False, reason="process control is off (--no-control)", mind=running(data))
        async with served(make_app(data, mind=running(data), controls=off, link=link)) as app:
            r = await http(app, "POST", "/api/raidlead", body={"action": "clear"}, origin=True)
            assert r.status == 403
    asyncio.run(go())
    assert [c[1] for c in link.calls].count("raidlead") == 1


def test_a_loot_council_award_keeps_its_item_name(guild, data):
    """A council charges nothing; its row's note names the receipt behind `council:` (decisions)."""
    store, _, now_id = guild
    conn = store.conn
    add_ledger(conn, [LedgerEntry(NOW - 30, 202, 0.0, "item", now_id, 50003, f"council:guild:loot:{now_id}:cc")])
    with conn:
        _put(conn, f"guild:loot:{now_id}:cc", {"state": "charged", "name": "Synthetic Ring", "item_id": 50003,
                                                "mode": "loot_council", "award": {"guid": 202, "paid": 0.0}})
    view = feed.read(data / "mind.db", raidview.raid_view, now=NOW)
    [item] = [e for e in view["open"]["dkp"]["events"] if e["reason"] == "item"]
    assert (item["guid"], item["total"], item["item"]) == (202, 0.0, "Synthetic Ring")
    loot = feed.read(data / "mind.db", raidview.guild_view)["loot"][0]
    assert (loot["guid"], loot["paid"], loot["item"]) == (202, 0.0, "Synthetic Ring")
