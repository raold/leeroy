"""The bridge's wire contract for test doubles: what a fake that stands in for the bridge, or for a
port that sends through it, must refuse because production refuses it.

- WireContract runs BridgeClient.send's own checks (bridge.claim_preserved_id for a kept identity,
  then protocol.encode_command) and mod-leeroy's decoder on the line that would be written.
- decode_command is mod-leeroy's DecodeCommand (modules/mod-leeroy/src/Lib/LeeroyProtocol.cpp)
  in Python: the envelope (json object, v, id, a known type) and the core commands' fields.
  modules/mod-leeroy/tests/fixtures/bridge_protocol.jsonl holds the cases both run.
- native_command_types reads the command types the native decoder knows from the module's source,
  so a fake cannot accept a type the server has never registered.

A guild DKP give went unrefused through the guild tests' fake ports for weeks while the real bridge
refused every one of them (#406): a fake that accepts anything proves nothing."""

import dataclasses
import functools
import json
import os
import re
from pathlib import Path
from unittest.mock import DEFAULT, AsyncMock

from .. import protocol
from ..bridge import NotConnected, claim_preserved_id, valid_preserved_id
from ..protocol import Ack, encode_command

NATIVE_SRC = Path(__file__).resolve().parents[3] / "modules" / "mod-leeroy" / "src"
U32_MAX = 2 ** 32 - 1
BAD_JSON = "bad json"
MISSING_ID = "missing id"
UNSUPPORTED_VERSION = "unsupported version"
UNKNOWN_TYPE = "unknown type: "
BAD_FIELD = "bad field: "
# LeeroyProtocol.cpp ParseChannel and IsSayChannel: raid_warning and emote are never said.
CHANNELS = frozenset({"guild", "officer", "party", "raid", "raid_warning", "say", "yell", "whisper", "emote"})
SAY_CHANNELS = CHANNELS - {"raid_warning", "emote"}


def _source() -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                     for path in sorted(NATIVE_SRC.rglob("*")) if path.suffix in (".cpp", ".h"))


def capture(line: bytes, error: str) -> None:
    """With LEEROY_WIRE_CAPTURE set, append {"line", "error", "test"} to that file: a command line
    a double saw, the error decode_command gave it ("" when accepted) and the test that sent it.
    mod-leeroy's Bridge_CapturedLinesParse replays the file: native must decode each line as the
    fake did, and accept every extension line the fake passed on."""
    path = os.environ.get("LEEROY_WIRE_CAPTURE")
    if not path:
        return
    row = {"line": line.rstrip(b"\n").decode("utf-8", "replace"), "error": error,
           "test": os.environ.get("PYTEST_CURRENT_TEST", "?")}
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps(row) + "\n")


@functools.cache
def native_core_types() -> frozenset[str]:
    """LeeroyProtocol.cpp kCommandNames; the mind's own core names when the source is not here."""
    if not NATIVE_SRC.is_dir():
        return frozenset(protocol._CORE_COMMANDS)
    return frozenset(re.findall(r'\{CommandType::\w+,\s*"(\w+)"\}', _source()))


@functools.cache
def native_command_types() -> frozenset[str] | None:
    """Every command type mod-leeroy's decoder knows, read from its source: the core names, each
    RegisterExtensionCommand list (k...CommandTypes), each handler RegisterCommand or
    LeeroyRegisterCommand registers by a literal or a named constant, and each day check
    (RegisterDayOwnerCheck). None when the module's source is not next to this package."""
    if not NATIVE_SRC.is_dir():
        return None
    text = _source()
    constants = dict(re.findall(r'\b(k\w+)\s*=\s*"(\w+)"', text))

    def resolve(arg: str) -> str | None:
        literal = re.fullmatch(r'"(\w+)"', arg)
        constant = re.fullmatch(r'std::string\((k\w+)\)', arg)
        if literal:
            return literal[1]
        return constants.get(constant[1]) if constant else None

    names = set(native_core_types())
    for body in re.findall(r'\bk\w*CommandTypes\s*=\s*\{([^}]*)\}', text):
        names.update(re.findall(r'"(\w+)"', body))
    for arg in re.findall(r'\b(?:Leeroy)?RegisterCommand\(\s*([^,()]+(?:\([^()]*\))?)\s*,', text):
        if (name := resolve(arg.strip())) is not None:
            names.add(name)
    for arg in re.findall(r'\bRegisterDayOwnerCheck\(\s*([^,()]+(?:\([^()]*\))?)\s*,', text):
        if (name := resolve(arg.strip())) is not None:
            names.add("day_check_" + name)
    return frozenset(names)


@dataclasses.dataclass(frozen=True, slots=True)
class Decoded:
    """One line as DecodeCommand sees it: the accepted message (None when refused), the line's
    string id (kept even when refused), the error and whether native answers it with an ack."""
    msg: dict | None
    id: str
    error: str
    ackable: bool


def _unsigned(value) -> bool:
    return type(value) is int and value >= 0


def _no_constant(name):
    raise ValueError(name)


class _Fields:
    """LeeroyProtocol.cpp FieldReader: the first bad field is the error."""

    def __init__(self, msg: dict):
        self.msg, self.error = msg, ""

    def _fail(self, key):
        self.error = BAD_FIELD + key

    def guid(self, key, required):
        if self.error:
            return 0
        if key not in self.msg:
            if required:
                self._fail(key)
            return 0
        value = self.msg[key]
        if not _unsigned(value) or value > U32_MAX:
            self._fail(key)
            return 0
        if required and value == 0:
            self._fail(key)
        return value

    def text(self, key, required, non_empty):
        if self.error:
            return ""
        if key not in self.msg:
            if required:
                self._fail(key)
            return ""
        value = self.msg[key]
        if type(value) is not str:
            self._fail(key)
            return ""
        if non_empty and not value:
            self._fail(key)
        return value

    def channel(self, key):
        if self.error:
            return ""
        value = self.msg.get(key)
        if type(value) is not str or value not in SAY_CHANNELS:
            self._fail(key)
            return ""
        return value

    def rank(self, key):
        if not self.error and not (_unsigned(self.msg.get(key)) and self.msg[key] <= 9):
            self._fail(key)

    def boolean(self, key):
        if not self.error and type(self.msg.get(key)) is not bool:
            self._fail(key)


def _core_fields(type_name: str, msg: dict) -> str:
    f = _Fields(msg)
    if type_name == "say":
        f.guid("guid", True)
        channel = f.channel("channel")
        f.text("text", True, False)
        f.guid("to_guid", channel == "whisper")
        if "quiet_in_combat" in msg:
            f.boolean("quiet_in_combat")
    elif type_name == "human_say":
        whisper = f.channel("channel") == "whisper"
        f.text("text", True, False)
        f.text("to_name", whisper, whisper)
    elif type_name == "emote":
        f.guid("guid", True)
        f.text("emote", True, True)
        f.guid("target_guid", False)
    elif type_name == "set_rank":
        f.guid("guid", True)
        f.rank("rank")
    elif type_name == "set_motd":
        f.text("text", True, False)
    elif type_name == "add_friend":
        f.guid("guid", True)
        f.text("note", False, False)
    elif type_name == "afk":
        f.guid("guid", True)
        f.boolean("on")
        f.text("message", False, False)
    return f.error


def decode_command(line: bytes | str, *, known=None) -> Decoded:
    """mod-leeroy's DecodeCommand. `known` is the extension types the server has registered
    (default: native_command_types(), else every type the mind registered); core types always are."""
    raw = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    try:
        msg = json.loads(raw, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        return Decoded(None, "", BAD_JSON, False)
    if type(msg) is not dict:
        return Decoded(None, "", BAD_JSON, False)
    cmd_id = msg["id"] if type(msg.get("id")) is str else ""
    has_id = type(msg.get("id")) is str
    if type(msg.get("v")) is not int or msg["v"] != protocol.PROTOCOL_VERSION:
        return Decoded(None, cmd_id, UNSUPPORTED_VERSION, False)
    if not has_id:
        return Decoded(None, "", MISSING_ID, False)
    type_name = msg["type"] if type(msg.get("type")) is str else ""
    if known is None:
        known = native_command_types()
        if known is None:
            known = protocol.wire_command_types()
    core = type_name in native_core_types()
    if not core and type_name not in known:
        return Decoded(None, cmd_id, UNKNOWN_TYPE + type_name, False)
    if not core:
        return Decoded(msg, cmd_id, "", False)
    error = _core_fields(type_name, msg)
    return Decoded(None if error else msg, cmd_id, error, True)


class WireContract:
    """BridgeClient.send's checks and mod-leeroy's decoder, for one fake bridge or port: a command
    production cannot send is refused here as it is there. One instance per fake, as identities
    are never reissued on one client (claim_preserved_id)."""

    def __init__(self, *, known=None):
        self.issued: set[str] = set()
        self.lines: list[dict] = []  # every line that would have been written, decoded, in order
        self.refused: list[tuple[dict, str]] = []  # (line, native's error)
        self._known = known
        self._next_id = 1

    def send(self, cmd, *, preserve_id: bool, ts_ms: int = 0) -> tuple[str, str]:
        """(the wire id, native's refusal or ""). Raises ValueError where BridgeClient.send does,
        before anything is written: a bad or reissued kept identity, a command encode_command
        refuses."""
        if preserve_id:
            # BridgeClient.send's old_auto: an m<n> it already allocated is never kept afterwards.
            old_auto = (valid_preserved_id(cmd.id) and cmd.id.startswith("m") and cmd.id[1:].isdigit()
                        and int(cmd.id[1:]) < self._next_id)
            cmd_id = claim_preserved_id(cmd.id, self.issued, in_use={cmd.id} if old_auto else ())
        else:
            cmd_id = f"m{self._next_id}"
            while cmd_id in self.issued:
                self._next_id += 1
                cmd_id = f"m{self._next_id}"
            self._next_id += 1
        line = encode_command(dataclasses.replace(cmd, id=cmd_id), ts_ms)
        decoded = decode_command(line, known=self._known)
        capture(line, decoded.error)
        msg = json.loads(line)
        self.lines.append(msg)
        if decoded.error:
            self.refused.append((msg, decoded.error))
        return cmd_id, decoded.error

    def ack(self, cmd, *, preserve_id: bool, ts_ms: int = 0, result=(True, "")) -> Ack:
        """send(), then the ack the mind gets: native's refusal, else `result` (ok, error)."""
        cmd_id, refusal = self.send(cmd, preserve_id=preserve_id, ts_ms=ts_ms)
        ok, error = (False, refusal) if refusal else result
        return Ack(ts_ms, cmd_id, ok, error)

    def wrap(self, send, *, preserve_id: bool | None = None):
        """A test's own fake send(cmd, ...) behind this contract: the command is checked first (a
        port's send always keeps its identity: preserve_id=True; a bridge's takes the call's own
        preserve_id keyword) and a refused one is answered as native answers it, never passed on."""
        async def checked(cmd, *args, **kwargs):
            keep = kwargs.get("preserve_id", False) if preserve_id is None else preserve_id
            cmd_id, refusal = self.send(cmd, preserve_id=keep)
            if refusal:
                return Ack(0, cmd_id, False, refusal)
            try:
                return await send(cmd, *args, **kwargs)
            except NotConnected:
                self.forget(cmd_id)
                raise
        return checked

    def forget(self, cmd_id: str) -> None:
        """A send the fake failed with NotConnected: BridgeClient refuses those before it claims
        the identity, so it may go out again."""
        self.issued.discard(cmd_id)


def bridge_send(send):
    """Decorator for a test's own async send(cmd, *, preserve_id=False, ...) standing in for
    BridgeClient.send: behind a WireContract of its own (WireContract.wrap)."""
    return WireContract().wrap(send)


def port_send(send):
    """Decorator for a test's own async send(cmd) standing in for a port's (guild life's, the
    Realm's, training's), which sends every command with its identity kept."""
    return WireContract().wrap(send, preserve_id=True)


def _mock(preserve_id, kwargs) -> AsyncMock:
    contract = WireContract()

    def check(cmd, *args, **call_kwargs):
        keep = call_kwargs.get("preserve_id", False) if preserve_id is None else preserve_id
        cmd_id, refusal = contract.send(cmd, preserve_id=keep)
        return Ack(0, cmd_id, False, refusal) if refusal else DEFAULT
    return AsyncMock(side_effect=check, **kwargs)


def bridge_mock(**kwargs) -> AsyncMock:
    """An AsyncMock standing in for BridgeClient.send (its return_value is the ack) whose every
    call passes a WireContract first; its assertions work as an AsyncMock's."""
    return _mock(None, kwargs)


def port_mock(**kwargs) -> AsyncMock:
    """bridge_mock for a port's send: every identity kept."""
    return _mock(True, kwargs)


def _method(method, preserve_id):
    async def checked(self, cmd, *args, **kwargs):
        contract = vars(self).get("wire_contract")
        if contract is None:
            contract = self.wire_contract = WireContract()

        async def call(command, *a, **k):
            return await method(self, command, *a, **k)
        return await contract.wrap(call, preserve_id=preserve_id)(cmd, *args, **kwargs)
    return checked


def bridge_method(method):
    """bridge_send for a fake class's send method; one WireContract per instance (wire_contract)."""
    return _method(method, None)


def port_method(method):
    """port_send for a fake class's send method; one WireContract per instance (wire_contract)."""
    return _method(method, True)
