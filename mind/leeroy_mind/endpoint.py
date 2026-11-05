"""The mind's control endpoint: a loopback socket through which the guild console stops the mind
cleanly and runs the raid actions guild life already performs.

Where: 127.0.0.1 only, on [control] port in mind.toml (0, the default: a free port the system
picks), with a socket no other process can share. It is on by default; `[control] enabled =
false` turns it off, and the console then stops the mind by force, as before, and offers no
raid actions.

The key: every start makes a new one (secrets.token_urlsafe(32)) and writes it, with the port
and the mind's pid, to control.json in the data dir, readable by the mind's own user only (a
protected DACL that names only that user on Windows, mode 0600 elsewhere), replacing any file
a crash left behind; the endpoint removes it when it closes. The console reads it and sends the
key only to a listener that the TCP table shows is the running mind's own process.

A request: one connection, one line. A JSON object of at most MAX_LINE bytes ending in a
newline, sent within READ_S (2 s), with exactly the keys v (1), key, op and, for a raid action
only, args. Anything else (an unknown or missing key, a wrong type, an HTTP request) is refused and
the connection closed; nothing after the first line is read. One answer line comes back,
{"ok": true, ...} or {"ok": false, "error": ...}, and the connection closes.

    status          the raid night's state and which raid actions can run now, or why not
    stop            the mind's own clean shutdown, the one Ctrl+C runs: answered, then begun
    raid {action}   call | form {raid, size, heroic, lead} | summon | start | stop | end:
                    guild life's own transitions, on the mind's event loop, with its guards
    tune {action}   status | set {scope, target, dial, value, previewed} | undo {change} |
                    revert {to} | group {id, name, where} | retire {id} |
                    preview {scope, target, dial, value}: the owner's persona dials
                    (leeroy_mind.tuning), written by this mind only, one change at a time,
                    and in force from the next read; a preview's text goes only to the caller

Logs carry the op, the action and the outcome, never the key, a name or a preview's text."""
import asyncio
import json
import logging
import os
import secrets
import socket
import time
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from leeroy_mind.config import ConfigError, log
from leeroy_mind.logs import log as log_event

VERSION = 1
KEY_FILE = "control.json"
MAX_LINE = 4096
MAX_REPLY = 65536
READ_S = 2.0  # the console sends its line at once: a slot is not held longer
RAID_S = 30.0  # how long one raid action may take before the console is answered anyway
PREVIEW_S = 40.0  # a preview's two lines, before and after, at background priority
MAX_CONNECTIONS = 8
REPLACE_TRIES = 40  # 2 s of 50 ms tries to move control.json over a file being read
OPS = ("status", "stop", "raid", "tune")
RAID_ACTIONS = ("call", "form", "summon", "start", "stop", "end")
TUNE_ACTIONS = ("status", "set", "undo", "revert", "group", "retire", "preview")
TUNE_SHAPES = {
    "status": {"action"},
    "set": {"action", "scope", "target", "dial", "value", "previewed"},
    "undo": {"action", "change"},
    "revert": {"action", "to"},
    "group": {"action", "id", "name", "where"},
    "retire": {"action", "id"},
    "preview": {"action", "scope", "target", "dial", "value"},
}
TUNE_SCOPES = ("world", "group", "member")
_DIAL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz_")
KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class BadRequest(ValueError):
    """A request that is not in the strict format; its text is the short answer."""


class Forbidden(BadRequest):
    pass


# -- [control] in mind.toml ----------------------------------------------------------------------

@dataclass(frozen=True)
class ControlCfg:
    enabled: bool = True
    port: int = 0  # 0: a free port the system picks (control.json names it)


_RULES = {
    "enabled": (lambda v: type(v) is bool, "true or false"),
    "port": (lambda v: type(v) is int and 0 <= v <= 65535, "an integer 0-65535 (0: any free port)"),
}


def load_control_config(path: Path) -> ControlCfg:
    """The [control] table of the mind.toml at `path`; a missing file or table means the
    defaults (on, any free port). ConfigError for a file it cannot read or a bad value."""
    path = Path(path)
    if not path.exists():
        return ControlCfg()
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: cannot read [control]: {e.__class__.__name__}") from e
    table = data.get("control")
    if table is None:
        return ControlCfg()
    if not isinstance(table, dict):
        raise ConfigError("control: expected a table")
    values = {}
    for key, v in table.items():
        rule = _RULES.get(key)
        if rule is None:
            log.warning("%s: unknown key control.%s", path, key)
            continue
        if not rule[0](v):
            raise ConfigError(f"control.{key}: expected {rule[1]}, got {v!r}")
        values[key] = v
    return ControlCfg(**values)


# -- the key file --------------------------------------------------------------------------------

def _user_sid() -> str:
    """This process's user, as a string SID (Windows)."""
    import ctypes
    from ctypes import wintypes as w
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD,
                                           ctypes.POINTER(w.DWORD)]
    advapi.GetTokenInformation.restype = w.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    TOKEN_QUERY, TOKEN_USER = 0x0008, 1
    token = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise OSError(f"cannot open this process's token (Windows error {ctypes.get_last_error()})")
    try:
        size = w.DWORD()
        advapi.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(max(size.value, 1))
        if not advapi.GetTokenInformation(token, TOKEN_USER, buf, size, ctypes.byref(size)):
            raise OSError(f"cannot read this process's user (Windows error {ctypes.get_last_error()})")
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        text = w.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError(f"cannot name this process's user (Windows error {ctypes.get_last_error()})")
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def private_sddl() -> str:
    """The security descriptor control.json is created with on Windows: a protected DACL (no
    inherited entry) whose one entry gives this process's user full access."""
    return f"D:P(A;;FA;;;{_user_sid()})"


def _create_private(path: Path) -> int:
    """A new file only this process's user can open (it must not exist), as a writable fd. The
    permissions are set as it is created: there is no moment it is readable by anyone else."""
    if os.name != "nt":
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)  # whatever the umask
        return fd
    import ctypes
    import msvcrt
    from ctypes import wintypes as w
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", w.BOOL)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(w.ULONG)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD,
                                   ctypes.POINTER(SecurityAttributes), w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [w.HANDLE]
    descriptor = ctypes.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            private_sddl(), 1, ctypes.byref(descriptor), None):
        raise OSError(f"cannot build a private descriptor (Windows error {ctypes.get_last_error()})")
    try:
        attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        GENERIC_WRITE, CREATE_NEW, NORMAL = 0x40000000, 1, 0x80
        handle = kernel.CreateFileW(str(path), GENERIC_WRITE, 0, ctypes.byref(attrs), CREATE_NEW,
                                    NORMAL, None)
        if handle in (None, w.HANDLE(-1).value):
            err = ctypes.get_last_error()
            if err == 80:  # ERROR_FILE_EXISTS
                raise FileExistsError(str(path))
            raise OSError(f"cannot create {path.name} (Windows error {err})")
    finally:
        kernel.LocalFree(descriptor)
    try:
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except OSError:
        kernel.CloseHandle(handle)
        raise


def write_key_file(path: Path, record: dict) -> None:
    """record as JSON at path, readable by this user only: written to a private file beside it,
    then moved over whatever is there (a crash's leftover), so the key is never readable by
    anyone else, nor half written."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with suppress(FileNotFoundError):
        tmp.unlink()
    fd = _create_private(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another process has open (the console
                # reads the old one every few seconds): that read lasts a moment
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(.05)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def _key_ok(value) -> bool:
    return isinstance(value, str) and 32 <= len(value) <= 128 and set(value) <= KEY_CHARS


def read_key_file(path: Path) -> dict | None:
    """{"pid", "port", "key"} from control.json, or None when it is missing or not exactly
    the shape the endpoint writes."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > 1024:
        return None
    try:
        value = json.loads(raw.decode("ascii"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"v", "pid", "port", "key"}:
        return None
    pid, port = value["pid"], value["port"]
    if (value["v"] != VERSION or type(value["v"]) is not int or type(pid) is not int
            or not 0 < pid < 1 << 32 or type(port) is not int or not 0 < port < 65536
            or not _key_ok(value["key"])):
        return None
    return {"pid": pid, "port": port, "key": value["key"]}


# -- the request ---------------------------------------------------------------------------------

def _no_constant(name):
    raise ValueError(f"{name} is not JSON")


def raid_args(args) -> dict:
    """A raid action's arguments, exactly: {"action"} for every action but form, and
    {"action", "raid", "size", "heroic", "lead"} for form, where raid is a raid key, size is
    null (as guild life picks), 10 or 25, heroic is a boolean and lead is null (as guild life
    picks), "me", "rotate" or an officer's guid. BadRequest otherwise."""
    from leeroy_mind.guild.raids import CATALOG
    if not isinstance(args, dict) or args.get("action") not in RAID_ACTIONS:
        raise BadRequest("raid: an action, one of " + ", ".join(RAID_ACTIONS))
    action = args["action"]
    if action != "form":
        if set(args) != {"action"}:
            raise BadRequest(f"raid {action}: takes no arguments")
        return {"action": action}
    if set(args) != {"action", "raid", "size", "heroic", "lead"}:
        raise BadRequest("raid form: exactly raid, size, heroic and lead")
    raid, size, heroic, lead = args["raid"], args["size"], args["heroic"], args["lead"]
    if not isinstance(raid, str) or raid not in CATALOG:
        raise BadRequest("raid form: raid is one of " + ", ".join(CATALOG))
    if size is not None and (type(size) is not int or size not in CATALOG[raid].sizes):
        raise BadRequest("raid form: size is null, 10 or 25")
    if type(heroic) is not bool or (heroic and not CATALOG[raid].heroic):
        raise BadRequest(f"raid form: {CATALOG[raid].name} has no heroic mode"
                         if type(heroic) is bool else "raid form: heroic is true or false")
    if not (lead is None or lead in ("me", "rotate")
            or (type(lead) is int and 0 < lead < 1 << 32)):
        raise BadRequest('raid form: lead is null, "me", "rotate" or an officer\'s guid')
    return {"action": "form", "raid": raid, "size": size, "heroic": heroic, "lead": lead}


def _count(v, lo: int) -> bool:
    return type(v) is int and lo <= v < 1 << 32


def _plain(v, most: int) -> bool:
    return isinstance(v, str) and len(v) <= most and v.isprintable()


def _where(where) -> bool:
    """A group predicate's shape (tuning.groups checks what it means): up to 20 fields, each a
    list of up to 12 short words, whole numbers or booleans."""
    if not isinstance(where, dict) or len(where) > 20:
        return False
    for k, values in where.items():
        if not (isinstance(k, str) and 1 <= len(k) <= 32 and set(k) <= _DIAL_CHARS):
            return False
        if not isinstance(values, list) or len(values) > 12:
            return False
        if not all((isinstance(x, str) and _plain(x, 40)) or type(x) in (int, bool)
                   for x in values):
            return False
    return True


def tune_args(args) -> dict:
    """A tune action's arguments, exactly (TUNE_SHAPES), in raid_args' style: the shape here,
    what it means (the dial, the member, the range) in the mind's tuning service, with the live
    cast. BadRequest otherwise."""
    if not isinstance(args, dict) or args.get("action") not in TUNE_ACTIONS:
        raise BadRequest("tune: an action, one of " + ", ".join(TUNE_ACTIONS))
    action = args["action"]
    shape = TUNE_SHAPES[action]
    if set(args) != shape:
        if action == "status":
            raise BadRequest("tune status: takes no arguments")
        raise BadRequest(f"tune {action}: exactly " + ", ".join(sorted(shape - {"action"})))
    if "scope" in shape:
        if args["scope"] not in TUNE_SCOPES:
            raise BadRequest(f"tune {action}: scope is world, group or member")
        if not _plain(args["target"], 10):
            raise BadRequest(f"tune {action}: target is text: '' for the world, everyone or a "
                             "group id, or a member's guid")
        dial = args["dial"]
        if not (isinstance(dial, str) and 1 <= len(dial) <= 32 and set(dial) <= _DIAL_CHARS):
            raise BadRequest(f"tune {action}: dial is a dial's name")
        v = args["value"]
        if v is not None and not (type(v) is int and -10 <= v <= 10):
            raise BadRequest(f"tune {action}: value is a whole number from -10 to 10, or null")
    if action == "set" and type(args["previewed"]) is not bool:
        raise BadRequest("tune set: previewed is true or false")
    if action == "undo" and not _count(args["change"], 1):
        raise BadRequest("tune undo: change is a change's number")
    if action == "revert" and not _count(args["to"], 0):
        raise BadRequest("tune revert: to is a change's number (0: before any)")
    if action in ("group", "retire"):
        gid = args["id"]
        if not (_count(gid, 1) or (action == "group" and gid is None)):
            raise BadRequest(f"tune {action}: id is a group's number"
                             + (", or null for a new one" if action == "group" else ""))
    if action == "group":
        if not _plain(args["name"], 40):
            raise BadRequest("tune group: name is at most 40 printable characters")
        if not _where(args["where"]):
            raise BadRequest("tune group: where is fields mapped to lists of values")
    return {k: args[k] for k in sorted(shape, key=["action", "scope", "target", "dial", "value",
                                                     "previewed", "change", "to", "id", "name",
                                                     "where"].index)}


def parse_request(line: bytes, key: str) -> tuple[str, dict | None]:
    """(op, args) from one request line; Forbidden for a wrong key, BadRequest otherwise."""
    if not line.endswith(b"\n") or len(line) > MAX_LINE:
        raise BadRequest("bad request")
    try:
        value = json.loads(line.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise BadRequest("bad request: not a JSON line") from None
    if not isinstance(value, dict) or not {"v", "key", "op"} <= set(value) <= {
            "v", "key", "op", "args"}:
        raise BadRequest("bad request: exactly v, key, op (and args for a raid or tune action)")
    given = value["key"]
    if not isinstance(given, str) or not secrets.compare_digest(
            given.encode("utf-8", "surrogatepass"), key.encode("ascii")):
        raise Forbidden("forbidden")
    if type(value["v"]) is not int or value["v"] != VERSION:
        raise BadRequest(f"bad request: v is {VERSION}")
    op = value["op"]
    if op not in OPS:
        raise BadRequest("bad request: op is one of " + ", ".join(OPS))
    if op not in ("raid", "tune"):
        if "args" in value:
            raise BadRequest(f"bad request: {op} takes no args")
        return op, None
    if "args" not in value:
        raise BadRequest(f"bad request: {op} needs args")
    return op, (raid_args if op == "raid" else tune_args)(value["args"])


def encode(value) -> bytes:
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
            + "\n").encode("ascii")


def request(port: int, key: str, op: str, args: dict | None = None, *,
            timeout: float = 5.0) -> dict:
    """One request to the endpoint on 127.0.0.1:port and its answer (a dict with a boolean ok).
    OSError when it cannot be reached, ValueError when the answer is not one."""
    body = {"v": VERSION, "key": key, "op": op}
    if args is not None:
        body["args"] = args
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as conn:
        conn.sendall(encode(body))
        data = b""
        while not data.endswith(b"\n"):
            chunk = conn.recv(8192)
            if not chunk:
                break
            data += chunk
            if len(data) > MAX_REPLY:
                raise ValueError("the endpoint's answer is too long")
    try:
        reply = json.loads(data.decode("ascii"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError):
        raise ValueError("the endpoint's answer is not JSON") from None
    if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
        raise ValueError("the endpoint's answer has no ok")
    return reply


# -- the server ----------------------------------------------------------------------------------

class ControlEndpoint:
    """Served as one of the app's background tasks (attach_control): the mind's shutdown
    cancels it first, which closes the socket and removes control.json."""

    def __init__(self, app, cfg: ControlCfg, *, key: str | None = None,
                 read_s: float = READ_S, raid_s: float = RAID_S):
        self.app, self.cfg = app, cfg
        self.key = key or secrets.token_urlsafe(32)
        self.path = Path(app.data_dir) / KEY_FILE
        self.read_s, self.raid_s = read_s, raid_s
        self.port: int | None = None
        self.listening = asyncio.Event()
        self._connections = 0
        self._raid = asyncio.Lock()
        self._tune = asyncio.Lock()  # one tuning change at a time
        self._preview = asyncio.Lock()  # one preview at a time

    async def serve(self) -> None:
        from leeroy_mind.console.httpd import listen_socket
        sock = listen_socket("127.0.0.1", self.cfg.port)
        try:
            server = await asyncio.start_server(self._connection, sock=sock, limit=MAX_LINE)
        except BaseException:
            sock.close()
            raise
        try:
            self.port = sock.getsockname()[1]
            await asyncio.to_thread(write_key_file, self.path, {
                "v": VERSION, "pid": os.getpid(), "port": self.port, "key": self.key})
            log_event("control_listening", port=self.port)
            self.listening.set()
            await asyncio.Event().wait()  # until the shutdown cancels it
        finally:
            server.close()
            self._remove_key_file()
            with suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2)

    def _remove_key_file(self) -> None:
        for _ in range(10):  # a console reading it at that moment holds it open briefly
            if (read_key_file(self.path) or {}).get("key") != self.key:
                return
            try:
                self.path.unlink()
                return
            except PermissionError:
                time.sleep(.02)
            except OSError:
                return

    async def _connection(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        if self._connections >= MAX_CONNECTIONS:
            writer.close()
            return
        self._connections += 1
        after = None
        try:
            try:
                line = await asyncio.wait_for(reader.readuntil(b"\n"), self.read_s)
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError,
                    ConnectionError, OSError):
                reply = {"ok": False, "error": "bad request"}
            else:
                try:
                    reply, after = await self._answer(line)
                except Exception as e:  # its text can carry game data: the class only
                    log_event("control_failed", level=logging.WARNING, error=type(e).__name__)
                    reply = {"ok": False, "error": f"internal error ({type(e).__name__})"}
            writer.write(encode(reply))
            await asyncio.wait_for(writer.drain(), self.read_s)
        except (ConnectionError, OSError, TimeoutError):
            pass
        finally:
            self._connections -= 1
            writer.close()
            with suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 2)
        if after is not None:
            after()

    async def _answer(self, line: bytes):
        try:
            op, args = parse_request(line, self.key)
        except Forbidden:
            log_event("control_refused", level=logging.WARNING, reason="key")
            return {"ok": False, "error": "forbidden"}, None
        except BadRequest as e:
            log_event("control_refused", reason="format")
            return {"ok": False, "error": str(e)}, None
        if op == "status":
            return self.status(), None
        if op == "stop":
            already = self.app.stopping
            log_event("control_stop_requested", already=already)
            return {"ok": True, "stopping": True}, (None if already else self.app.request_stop)
        reply = await (self.raid(args) if op == "raid" else self.tune(args))
        log_event("control_request", op=op, action=args["action"],
                  outcome="ok" if reply["ok"] else "refused")
        return reply, None

    def status(self) -> dict:
        guild = getattr(self.app, "guild", None)
        raid = None
        if guild is not None and not self.app.stopping:
            try:
                raid = guild.console_status()
            except Exception as e:
                log_event("control_status_failed", level=logging.WARNING, error=type(e).__name__)
        return {"ok": True, "v": VERSION, "pid": os.getpid(), "stopping": self.app.stopping,
                "raid": raid}

    async def raid(self, args: dict) -> dict:
        from leeroy_mind.guild.raid_night import ConsoleRefused
        guild = getattr(self.app, "guild", None)
        if self.app.stopping:
            return {"ok": False, "error": "the mind is stopping"}
        if guild is None:
            return {"ok": False, "error": "guild life is not running in this mind"}
        if self._raid.locked():
            return {"ok": False, "error": "another raid action is still running"}
        await self._raid.acquire()
        # Never cancelled from here: an action waiting on the game's answer runs to its end
        # (and holds the next one off) even when the console has been answered meanwhile
        task = asyncio.ensure_future(guild.console_action(args))

        def finished(t):
            self._raid.release()
            if not t.cancelled():
                t.exception()  # retrieved: an answered caller may no longer be waiting
        task.add_done_callback(finished)
        done, _ = await asyncio.wait({task}, timeout=self.raid_s)
        if task not in done:
            return {"ok": False, "error": "the raid action is still waiting on the game; check "
                                          "the raid's state before trying again"}
        try:
            return {"ok": True, "message": task.result()}
        except ConsoleRefused as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:  # its text can carry game data
            log_event("control_raid_failed", level=logging.WARNING, action=args["action"],
                      error=type(e).__name__)
            return {"ok": False, "error": f"the raid action failed ({type(e).__name__})"}


    async def tune(self, args: dict) -> dict:
        """The owner's dials (leeroy_mind.tuning.service): writes one at a time, previews one
        at a time and bounded by PREVIEW_S; status at any time."""
        service = getattr(self.app, "tuning", None)
        if self.app.stopping:
            return {"ok": False, "error": "the mind is stopping"}
        if service is None:
            return {"ok": False, "error": "tuning is not running in this mind"}
        action = args["action"]
        if action == "status":
            return await service.handle(args)
        if action == "preview":
            if self._preview.locked():
                return {"ok": False, "error": "another preview is still running"}
            async with self._preview:
                try:
                    return await asyncio.wait_for(service.handle(args), PREVIEW_S)
                except TimeoutError:
                    return {"ok": False, "error": "The guild is talking a lot right now and the "
                                                  "preview timed out. Try again in a moment."}
        if self._tune.locked():
            return {"ok": False, "error": "another tuning change is still being written"}
        async with self._tune:
            return await service.handle(args)


def attach_control(app) -> ControlEndpoint | None:
    """The endpoint as a background task of the app, unless [control] turns it off."""
    try:
        cfg = load_control_config(app.data_dir / "mind.toml")
    except ConfigError as e:
        log_event("control_config_invalid", level=logging.WARNING, reason=str(e))
        cfg = ControlCfg()
    if not cfg.enabled:
        log_event("control_off")
        return None
    endpoint = ControlEndpoint(app, cfg)
    app.register_background(endpoint.serve)
    return endpoint
