"""The answers the console tests script for the mind's control endpoint, held against what the
real endpoint answers for the same op: a real ControlEndpoint on 127.0.0.1:0 with a small fake
app, asked through endpoint.request. A scripted answer may leave out what an older mind does
not send, but never carries a key the real one has not, nor a value of another kind."""
import asyncio
import contextlib
import os
import sqlite3
from types import SimpleNamespace

from conftest import run
from leeroy_mind import endpoint
from leeroy_mind.console import libraryview
from leeroy_mind.endpoint import ControlCfg, ControlEndpoint
from leeroy_mind.guild import bankread
from leeroy_mind.protocol import Hello
from leeroy_mind.testing import control
from console.test_console_bank import SNAPSHOT
from console.test_console_library import hit
from console.test_console_raid import STATUS
from console.test_console_server import WORLD
from console.test_console_typing import ENTRY, WHISPER, status
from test_health import Endpoint as HealthEndpoint

KINDS = {bool: "bool", int: "number", float: "number", str: "text", list: "list", dict: "object"}


def within(fake, real, where: str, *, deep=()) -> None:
    """Every key of `fake` is one `real` has, with a value of the same kind (null on either side
    is unknown, as a real answer may say); the keys in `deep` are held the same way."""
    extra = set(fake) - set(real)
    assert not extra, f"{where}: {sorted(extra)} are not in the real answer"
    for k, v in fake.items():
        if v is None or real[k] is None:
            continue
        assert KINDS[type(v)] == KINDS[type(real[k])], f"{where}.{k}: {v!r} vs {real[k]!r}"
        if k in deep:
            within(v, real[k], f"{where}.{k}")


class Guild:
    def __init__(self, armory=None):
        self.armory = armory

    def console_status(self):
        return {"state": "waiting_call", "actions": {"call": None}}

    async def console_action(self, args):
        return f"did {args['action']}"


class App:
    def __init__(self, data_dir):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA user_version = 17")
        self.data_dir, self.stopping, self.stops = data_dir, False, 0
        self.guild = Guild()
        self.hello = Hello(ts=1793304000000, server="Acme", version="3.3.5", guild_id=21,
                           human_guid=9)
        self.bridge = SimpleNamespace(connected=True, greeted=True)
        self.world = SimpleNamespace(online={1, 2, 3}, human_online=False,
                                     cast_guids=lambda: {1, 2, 3, 4, 5})
        self.store = SimpleNamespace(conn=conn, get_meta=lambda k: "21" if k == "guild_id" else None)
        self.engine = SimpleNamespace(typing_now=lambda: [])

    def request_stop(self):
        self.stops += 1
        self.stopping = True


def ask(tmp_path, op, args=None, *, setup=lambda app: None):
    """The real endpoint's answer to one request, over its socket."""
    async def go():
        app = App(tmp_path)
        setup(app)
        ep = ControlEndpoint(app, ControlCfg(), key=control.KEY)
        task = asyncio.create_task(ep.serve())
        await asyncio.wait_for(ep.listening.wait(), 5)
        try:
            return await asyncio.to_thread(endpoint.request, ep.port, control.KEY, op, args,
                                           timeout=5)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return run(go())


def test_the_status_fakes_carry_only_what_the_real_status_carries(tmp_path):
    real = ask(tmp_path, "status")
    assert real["ok"] is True and real["pid"] == os.getpid() and real["world"]["connected"]
    fakes = {"raid": STATUS, "typing": status([ENTRY, WHISPER]),
             "server": {"ok": True, "stopping": False, "raid": None, "world": WORLD},
             "health": HealthEndpoint().reply}
    for name, fake in fakes.items():
        within(fake, real, f"{name} status", deep=("world",))
    assert set(WORLD) == set(real["world"])  # the Health tab shows the world as it comes


def test_the_bank_fake_is_a_snapshot_the_real_bank_op_gives(tmp_path):
    rows = ([["1234567"]], [["0", "Potions"], ["1", ""]],
            [["0", "0", "2589", "20"], ["0", "14", "4306", "5"], ["1", "97", "777777", "1"]],
            [["1793304000", "4", "1101", "50000"], ["1793303000", "5", "1102", "10000"]])
    armory = SimpleNamespace(available=True, bank=lambda guild_id: bankread.parse_bank(*rows))
    real = ask(tmp_path, "bank", setup=lambda app: setattr(app.guild, "armory", armory))
    assert isinstance(real.pop("read_at"), float)
    assert real == {k: v for k, v in SNAPSHOT.items() if k != "read_at"}


def test_the_library_fakes_hit_is_the_endpoints_hit_shape():
    chunk = SimpleNamespace(id=7, kind="command", title=".tele", section="", text="teleports",
                            source="serverref", cite=lambda: "installed server reference",
                            score=0.5)
    real = libraryview.hit(chunk)
    assert tuple(real) == libraryview.HIT_KEYS
    within(hit(), real, "library hit")
    assert set(hit()) == set(real)  # the console keeps a hit only with exactly these keys


def test_the_action_fakes_answer_as_the_real_raid_and_stop_ops(tmp_path):
    raid = ask(tmp_path, "raid", {"action": "summon"})
    assert raid == {"ok": True, "message": "did summon"}
    within({"ok": True, "message": "summoning 2 late members now"}, raid, "raid action")
    stop = ask(tmp_path, "stop")
    assert stop == {"ok": True, "stopping": True}  # what the clean stop's fake answers
