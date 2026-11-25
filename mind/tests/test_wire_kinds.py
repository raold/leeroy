"""Every command kind the mind can put on the bridge, end to end: built from a line mod-leeroy's own
parsers accept (a row of the shared fixtures under modules/mod-leeroy/tests/fixtures, each read by a
native test), sent through the real BridgeClient to FakeBridge, which decodes it as native does, and
acked. The kinds come from the protocol registry, so a new one fails here until a native-validated
line of it exists and the mind encodes that line exactly."""

import ast
import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from conftest import run
from leeroy_mind import owner_say, protocol
from leeroy_mind.bridge import Backoff, BridgeClient, valid_preserved_id
from leeroy_mind.guild import protocol as guild
from leeroy_mind.lead import protocol as lead
from leeroy_mind.raidlead import protocol as raidlead
from leeroy_mind.raidplay import respond as raidplay
from leeroy_mind.realm import protocol as realm
from leeroy_mind.testing import wire
from leeroy_mind.testing.fake_bridge import FakeBridge
from leeroy_mind.training import protocol as training
from leeroy_mind.world import protocol as world

PACKAGE = Path(protocol.__file__).resolve().parent
NATIVE_TESTS = Path(__file__).resolve().parents[2] / "modules" / "mod-leeroy" / "tests"
# Each fixture file and the native test that runs every command row of it through the handler's parser.
FIXTURE_TESTS = {
    "bridge_protocol.jsonl": "TestBridgeProtocol.cpp",
    "p1d_protocol.jsonl": "TestGuildCommands.cpp",
    "realm_protocol.jsonl": "TestRealmCommands.cpp",
    "training_protocol.jsonl": "TestTrainingProtocol.cpp",
    "owner_say_protocol.jsonl": "TestOwnerSay.cpp",
    "day_protocol.jsonl": None,
}
# Kinds whose handlers parse inline in game code (Realm/DayPlans.cpp), with no parser a native test can
# call: their rows prove only the envelope native decodes.
ENVELOPE_ONLY = frozenset({"day_begin", "day_status", "day_revoke"})
# Every module that registers a command type, and how. A new registration site fails
# test_every_registration_site_is_enumerated until it is listed here.
REGISTRARS = {
    "leeroy_mind.guild.protocol": guild.register,
    "leeroy_mind.realm.protocol": realm.register,
    "leeroy_mind.training.protocol": training.register,
    "leeroy_mind.lead.protocol": lead.register,
    "leeroy_mind.raidlead.protocol": raidlead.register,
    "leeroy_mind.world.protocol": world.register,
    "leeroy_mind.owner_say": owner_say.register,
    "leeroy_mind.raidplay.respond": raidplay.register,
}
# Sent with BridgeClient's own m<n> id; every other kind keeps the identity its sender built
# (guild, realm and training ports, godmode, invites, lead, raid lead and playstyle all send
# preserve_id=True).
AUTOMATIC_ID = frozenset(protocol._CORE_COMMANDS) | {"owner_say", "raid_mechanics", "get_world_roster"}
DERIVED = {f"day_check_{name}": name for name in protocol.DAY_CHECK_TYPES} | {
    derived: name for name, names in protocol.DRIVER_TYPES.items() for derived in names}


def mind_commands() -> dict[str, type]:
    for register in REGISTRARS.values():
        register()
    return {name: cls for name, cls in protocol._commands.items() if cls.__module__.startswith("leeroy_mind.")}


COMMANDS = mind_commands()
KINDS = sorted(protocol.wire_command_types(COMMANDS))


def rows() -> list[tuple[str, dict]]:
    out = []
    for name in FIXTURE_TESTS:
        for raw in (NATIVE_TESTS / "fixtures" / name).read_text(encoding="utf-8").splitlines():
            row = json.loads(raw) if raw.strip() else None
            if row and row.get("kind", "command") == "command" and row.get("valid") and isinstance(row.get("msg"), dict):
                out.append((name, row))
    return out


ROWS = rows()


def fields(msg: dict) -> dict:
    return {key: value for key, value in msg.items() if key not in ("v", "type", "ts")}


def sample(kind: str):
    """(the command, the native row it reproduces): the first valid row of this kind the mind
    encodes exactly, full rows and *_ok rows first."""
    cls = COMMANDS[DERIVED.get(kind, kind)]
    candidates = [row for _, row in ROWS if row["msg"].get("type") == kind]
    candidates.sort(key=lambda row: (not row.get("full"), not row["name"].endswith("_ok")))
    for row in candidates:
        try:
            cmd = protocol._build(cls, fields(row["msg"]))
            line = json.loads(protocol.encode_command(cmd, row["msg"].get("ts", 0)))
        except (protocol.ProtocolError, ValueError, TypeError):
            continue
        if line == row["msg"]:
            return cmd, row
    pytest.fail(f"no native-validated line of {kind!r} that the mind encodes exactly: add one to a fixture "
                f"under {NATIVE_TESTS / 'fixtures'} that a native test parses (FIXTURE_TESTS)")


def test_every_registration_site_is_enumerated():
    sites = set()
    for path in PACKAGE.rglob("*.py"):
        relative = path.relative_to(PACKAGE.parent).with_suffix("")
        module = ".".join(relative.parts)
        if module in ("leeroy_mind.protocol",) or module.startswith("leeroy_mind.testing"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name == "register_command":
                    sites.add(module)
    assert sites == set(REGISTRARS)


def test_each_fixture_is_read_by_its_native_test():
    for name, test in FIXTURE_TESTS.items():
        assert (NATIVE_TESTS / "fixtures" / name).is_file()
        if test is not None:
            assert name in (NATIVE_TESTS / test).read_text(encoding="utf-8"), (name, test)
    day = {row["msg"]["type"] for name, row in ROWS if name == "day_protocol.jsonl"}
    assert day == ENVELOPE_ONLY


def test_native_knows_every_kind_the_mind_sends():
    assert len(KINDS) > 80
    assert set(KINDS) <= wire.native_command_types()


@contextlib.asynccontextmanager
async def session():
    bridge = FakeBridge()
    port = await bridge.start()

    async def on_event(ev):
        pass

    client = BridgeClient("127.0.0.1", port, on_event, backoff=Backoff(0.01, 0.05, 0))
    task = asyncio.create_task(client.run())
    try:
        await client.wait_greeted(2.0)
        yield bridge, client
    finally:
        await client.close()
        await asyncio.wait_for(task, 2.0)
        await bridge.stop()


@pytest.mark.parametrize("kind", KINDS)
def test_the_kind_goes_through_the_client_and_is_acked(kind):
    cmd, row = sample(kind)
    keep = kind not in AUTOMATIC_ID

    async def go():
        async with session() as (bridge, client):
            ack = await client.send(cmd, preserve_id=keep)
            assert ack.ok and not bridge.refused
            sent = bridge.commands[-1]
            assert sent["type"] == kind and fields(sent) | {"id": row["msg"]["id"]} == fields(row["msg"])
            assert sent["id"] == ack.id
            if keep:
                assert sent["id"] == cmd.id and valid_preserved_id(sent["id"])
            else:
                assert sent["id"].startswith("m")
    run(go())
