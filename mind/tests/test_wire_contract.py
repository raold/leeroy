"""The fakes' wire contract against mod-leeroy's own: bridge_protocol.jsonl is run through the
native decoder by modules/mod-leeroy/tests/TestBridgeProtocol.cpp and through the fake bridge's
decoder here, and the fakes refuse what BridgeClient.send refuses."""

import asyncio
import contextlib
import dataclasses
import json
from pathlib import Path

import pytest

from conftest import run
from leeroy_mind import protocol
from leeroy_mind.bridge import Backoff, BridgeClient, claim_preserved_id
from leeroy_mind.protocol import Say
from leeroy_mind.testing import wire
from leeroy_mind.testing.fake_bridge import FakeBridge

FIXTURES = Path(__file__).resolve().parents[2] / "modules" / "mod-leeroy" / "tests" / "fixtures"
ROWS = [json.loads(line) for line in (FIXTURES / "bridge_protocol.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()]


def line(row) -> bytes:
    return (row["line"] if "line" in row else json.dumps(row["msg"])).encode()


@pytest.mark.parametrize("row", ROWS, ids=[row["name"] for row in ROWS])
def test_the_fake_decoder_answers_each_line_as_native_does(row):
    decoded = wire.decode_command(line(row))
    assert (decoded.msg is not None, decoded.error) == (row["valid"], row["error"])
    if "ackable" in row:
        assert decoded.ackable is row["ackable"]
    msg = row.get("msg")
    expected_id = msg["id"] if isinstance(msg, dict) and type(msg.get("id")) is str else ""
    assert decoded.id == expected_id


def test_the_native_source_is_read():
    types = wire.native_command_types()
    assert types is not None and wire.native_core_types() == frozenset(protocol._CORE_COMMANDS)
    assert {"give_loot", "realm_say", "dummy_trial", "lead_do", "day_check_string_do",
            "member_dungeon_inspect", "calendar_post", "owner_say", "string_do"} <= types
    assert "dance" not in types


def test_a_preserved_identity_follows_the_clients_rule():
    issued = set()
    assert claim_preserved_id("guild-loot-3-abc", issued) == "guild-loot-3-abc"
    for bad in ("guild-loot:3:abc", "", "x" * 129, "ümlaut", "a b", None):
        with pytest.raises(ValueError, match="invalid preserved command identity"):
            claim_preserved_id(bad, issued)
    with pytest.raises(ValueError, match="already issued"):
        claim_preserved_id("guild-loot-3-abc", issued)
    with pytest.raises(ValueError, match="already issued"):
        claim_preserved_id("busy", issued, in_use={"busy"})


def test_the_contract_refuses_what_the_client_and_the_server_refuse():
    contract = wire.WireContract()
    say = Say(id="", guid=101, channel="guild", text="hi")
    assert contract.send(say, preserve_id=False) == ("m1", "")
    assert contract.send(dataclasses.replace(say, id="kept-1"), preserve_id=True) == ("kept-1", "")
    with pytest.raises(ValueError, match="already issued"):
        contract.send(dataclasses.replace(say, id="kept-1"), preserve_id=True)
    with pytest.raises(ValueError, match="invalid preserved"):
        contract.send(dataclasses.replace(say, id="a:b"), preserve_id=True)
    with pytest.raises(ValueError, match="channel not allowed"):
        contract.send(dataclasses.replace(say, channel="raid_warning"), preserve_id=False)
    # A field the encoder passes but native refuses comes back as native's ack.
    ack = contract.ack(dataclasses.replace(say, guid=0), preserve_id=False)
    assert (ack.ok, ack.error) == (False, "bad field: guid")
    assert contract.refused[-1][1] == "bad field: guid"
    # A kept identity is never reissued as an automatic one, nor an automatic one kept afterwards.
    contract = wire.WireContract()
    contract.send(dataclasses.replace(say, id="m1"), preserve_id=True)
    assert contract.send(say, preserve_id=False)[0] == "m2"
    with pytest.raises(ValueError, match="already issued"):
        contract.send(dataclasses.replace(say, id="m2"), preserve_id=True)
    assert contract.send(dataclasses.replace(say, id="m3"), preserve_id=True)[0] == "m3"


@dataclasses.dataclass(frozen=True, slots=True)
class NativeNeverHeardOf:
    id: str


def test_a_type_native_never_registered_is_refused_by_the_contract_and_the_fake():
    protocol.register_command("wire_contract_unknown", NativeNeverHeardOf)
    contract = wire.WireContract()
    ack = contract.ack(NativeNeverHeardOf(id="x-1"), preserve_id=True)
    assert (ack.id, ack.ok, ack.error) == ("x-1", False, "unknown type: wire_contract_unknown")

    async def go():
        async with session() as (bridge, client):
            ack = await client.send(NativeNeverHeardOf(id=""))
            assert (ack.ok, ack.error) == (False, "unknown type: wire_contract_unknown")
            assert bridge.refused == [(bridge.commands[-1], "unknown type: wire_contract_unknown")]
    run(go())


def test_wrap_checks_a_tests_own_fake_send():
    sent = []

    async def send(cmd, *, preserve_id=False):
        sent.append(cmd)
        return protocol.Ack(0, cmd.id, True, "")

    async def go():
        checked = wire.WireContract().wrap(send)
        say = Say(id="kept", guid=101, channel="guild", text="hi")
        assert (await checked(say, preserve_id=True)).ok
        with pytest.raises(ValueError, match="already issued"):
            await checked(say, preserve_id=True)
        refused = await checked(dataclasses.replace(say, guid=0))
        assert (refused.ok, refused.error) == (False, "bad field: guid")
        port = wire.WireContract().wrap(send, preserve_id=True)
        with pytest.raises(ValueError, match="invalid preserved"):
            await port(dataclasses.replace(say, id="a:b"))
        assert sent == [say]
    run(go())


@contextlib.asynccontextmanager
async def session(**bridge_kw):
    bridge = FakeBridge(**bridge_kw)
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


def test_the_fake_bridge_answers_a_bad_core_field_with_natives_ack():
    async def go():
        async with session() as (bridge, client):
            ack = await client.send(Say(id="", guid=0, channel="guild", text="hi"))
            assert (ack.ok, ack.error) == (False, "bad field: guid")
            ack = await client.send(Say(id="", guid=101, channel="whisper", text="hi"))
            assert (ack.ok, ack.error) == (False, "bad field: to_guid")
            assert [error for _, error in bridge.refused] == ["bad field: guid", "bad field: to_guid"]
            assert (await client.send(Say(id="", guid=101, channel="guild", text="hi"))).ok
    run(go())


def test_the_fake_bridge_answers_a_bad_envelope_with_natives_error():
    async def go():
        async with session() as (bridge, client):
            writer = client._writer
            for raw, error in ((b'{"v":2,"type":"ping","id":"x1","ts":0}', "unsupported version"),
                               (b'{"v":1,"type":"ping","ts":0}', "missing id")):
                writer.write(raw + b"\n")
                await writer.drain()
                for _ in range(200):
                    if len(bridge.refused) and bridge.refused[-1][1] == error:
                        break
                    await asyncio.sleep(0.005)
                assert bridge.refused[-1][1] == error
            assert bridge.written[-2:] == ["error", "error"]
    run(go())
