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
newline, sent within READ_S (2 s), with exactly the keys v (1), key, op and, for a raid, tune,
selfie, say or board action only, args. Anything else (an unknown or missing key, a wrong type,
an HTTP request) is refused and the connection closed; nothing after the first line is read. One
answer line comes back, {"ok": true, ...} or {"ok": false, "error": ...}, and the connection
closes.

    status          the raid night's state and which raid actions can run now, or why not
    stop            the mind's own clean shutdown, the one Ctrl+C runs: answered, then begun
    raid {action}   call | form {raid, size, heroic, lead} | summon | start | stop | end:
                    guild life's own transitions, on the mind's event loop, with its guards
    tune {action}   status | set {scope, target, dial, value, previewed} | undo {change} |
                    revert {to} | group {id, name, where} | retire {id} |
                    preview {scope, target, dial, value}: the owner's persona dials
                    (leeroy_mind.tuning), written by this mind only, one change at a time,
                    and in force from the next read; a preview's text goes only to the caller
    selfie {action} status | pick {guid, set, index} | undo {pick} | more {guid}: profile
                    photos (leeroy_mind.selfies), written by this mind only, one change at a
                    time until its files are written; status at any time
    say {channel, text[, to]}
                    guild | officer | group | whisper (to: a cast member's guid): the owner's
                    own line, said in game as his through native owner_say
                    (leeroy_mind.owner_say), one at a time and answered within SAY_S; group is
                    his raid when his group is one, else his party. say_args refuses a line the
                    game would (empty, over 255 bytes, a command), and every refusal comes back
                    as the sentence the console shows
    bank            the guild bank (leeroy_mind.guild.bankread): money, the six tabs' slots and the
                    money log, read by four read-only SELECTs through guild life's armory (the
                    database credentials never leave this process), one read at a time, within
                    BANK_READ_S, and an answer younger than BANK_CACHE_S given again as it is
    board {action}  reply {kind, to, text} | withdraw {id}: his reply on the Board to a Realm
                    forum post or a letter in his mailbox (leeroy_mind.board), stored in
                    mind.db waiting for the Realm night, or a waiting one taken back; one at a
                    time. board_args refuses a text the store would (empty, too long for its
                    kind, not UTF-8), in the words the console shows
    library {query[, k]}
                    the mind's own semantic search over library.db (read-only): one embedding call
                    on the embedding model Ollama already holds (never a chat model, never a
                    load), refused in words while the mind is stopping, the embedder is not
                    loaded or the library has no vectors, or another library call is running;
                    answers up to k (1-12) hits in the console's hit shape. The query is never
                    logged

Logs carry the op, the action (for say, the channel) and the outcome, never the key, a name, a
preview's text, a line's or a reply's."""
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
MAX_LINE = 8192  # a 400-character letter reply escaped (12 bytes an astral character) fits
MAX_REPLY = 65536
READ_S = 2.0  # the console sends its line at once: a slot is not held longer
RAID_S = 30.0  # how long one raid action may take before the console is answered anyway
PREVIEW_S = 40.0  # a preview's two lines, before and after, at background priority
SAY_S = 10.0  # one line of his, past the bridge's own 5 s ack wait
BANK_READ_S = 15.0  # the four bank queries, before the console is answered anyway
LIBRARY_READ_S = 15.0  # the query embedding and the ranking, before the console is answered anyway
BANK_CACHE_S = 30.0  # a bank snapshot younger than this is answered as it is
MAX_CONNECTIONS = 8
REPLACE_TRIES = 40  # 2 s of 50 ms tries to move control.json over a file being read
OPS = ("status", "stop", "raid", "tune", "selfie", "say", "bank", "board", "library")
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
SELFIE_ACTIONS = ("status", "pick", "more", "undo")
SELFIE_SHAPES = {
    "status": {"action"},
    "pick": {"action", "guid", "set", "index"},
    "more": {"action", "guid"},
    "undo": {"action", "pick"},
}
SAY_CHANNELS = ("guild", "officer", "group", "whisper")
MAX_SAY_CHARS = 1024  # what the console's box may hold; a line the game takes is 255 bytes
BOARD_ACTIONS = ("reply", "withdraw")
BOARD_SHAPES = {"reply": {"action", "kind", "to", "text"}, "withdraw": {"action", "id"}}
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


def create_private(path: Path) -> int:
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
    """record as JSON at path, readable by this user only (write_private)."""
    write_private(path, json.dumps(record, separators=(",", ":"), ensure_ascii=True)
                  .encode("ascii"))


def write_private(path: Path, data: bytes) -> None:
    """data at path, readable by this user only: written to a private file beside it, then
    moved over whatever is there (a crash's leftover), so the key is never readable by anyone
    else, nor half written."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with suppress(FileNotFoundError):
        tmp.unlink()
    fd = create_private(tmp)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
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


def private_problem(fd: int) -> str | None:
    """Why someone other than this process's user may read the open file `fd`, or None when no
    one can. It is read from the open file, so it is the file that will be read: on Windows an
    owner that is not this user (an owner may change the DACL), a missing DACL (everyone may
    open it) or an entry in it that allows anyone else (deny entries take nothing away from
    him); elsewhere, another owner or a group or other bit in its mode. OSError when it cannot
    be read."""
    if os.name != "nt":
        st = os.fstat(fd)
        if st.st_uid != os.getuid():
            return "it belongs to another user"
        if st.st_mode & 0o077:
            return f"its mode is {oct(st.st_mode & 0o777)}"
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes as w
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class AclSize(ctypes.Structure):
        _fields_ = [("AceCount", w.DWORD), ("AclBytesInUse", w.DWORD),
                    ("AclBytesFree", w.DWORD)]

    class AceHeader(ctypes.Structure):
        _fields_ = [("AceType", ctypes.c_ubyte), ("AceFlags", ctypes.c_ubyte),
                    ("AceSize", w.WORD)]
    advapi.GetSecurityInfo.argtypes = [w.HANDLE, ctypes.c_int, w.DWORD,
                                       *([ctypes.POINTER(ctypes.c_void_p)] * 5)]
    advapi.GetSecurityInfo.restype = w.DWORD
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, ctypes.c_int]
    advapi.GetAclInformation.restype = w.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = w.BOOL
    advapi.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertStringSidToSidW.restype = w.BOOL
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi.EqualSid.restype = w.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    SE_FILE_OBJECT, OWNER, DACL, ACL_SIZE_INFORMATION = 1, 1, 4, 2
    ALLOWED, DENIED, INHERIT_ONLY = 0, 1, 0x08
    handle = msvcrt.get_osfhandle(fd)
    me = ctypes.c_void_p()
    if not advapi.ConvertStringSidToSidW(_user_sid(), ctypes.byref(me)):
        raise OSError(f"cannot read this process's user (Windows error {ctypes.get_last_error()})")
    owner, dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        err = advapi.GetSecurityInfo(handle, SE_FILE_OBJECT, OWNER | DACL, ctypes.byref(owner),
                                     None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if err:
            raise OSError(f"cannot read the file's owner and access list (Windows error {err})")
        if not owner.value or not advapi.EqualSid(owner, me):
            return "it belongs to another user"
        if not dacl.value:
            return "it has no access list, so everyone may open it"
        size = AclSize()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size),
                                        ACL_SIZE_INFORMATION):
            raise OSError(f"cannot read the access list (Windows error {ctypes.get_last_error()})")
        for i in range(size.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, i, ctypes.byref(ace)):
                raise OSError(f"cannot read an access entry (Windows error "
                              f"{ctypes.get_last_error()})")
            header = AceHeader.from_address(ace.value)
            if header.AceFlags & INHERIT_ONLY or header.AceType == DENIED:
                continue  # for what is made inside it, or a refusal
            if header.AceType != ALLOWED:
                return "its access list has an entry of a kind this check does not read"
            if not advapi.EqualSid(ace.value + 8, me):  # after the header and the access mask
                return "its access list lets others open it"
        return None
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)
        kernel.LocalFree(me)


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


def selfie_args(args) -> dict:
    """A photo action's arguments, exactly (SELFIE_SHAPES), in tune_args' style: guid a
    member's guid (1 below 2**32), set a set's number (1 below 2**31), index a photo of the set
    (0-3), pick a change's number (from 1); whether they name a cast member's ready set is the
    mind's selfie service's to say. BadRequest otherwise."""
    if not isinstance(args, dict) or args.get("action") not in SELFIE_ACTIONS:
        raise BadRequest("selfie: an action, one of " + ", ".join(SELFIE_ACTIONS))
    action = args["action"]
    shape = SELFIE_SHAPES[action]
    if set(args) != shape:
        if action == "status":
            raise BadRequest("selfie status: takes no arguments")
        raise BadRequest(f"selfie {action}: exactly " + ", ".join(sorted(shape - {"action"})))
    if "guid" in shape and not _count(args["guid"], 1):
        raise BadRequest(f"selfie {action}: guid is a member's guid")
    if action == "pick":
        if not (type(args["set"]) is int and 1 <= args["set"] < 1 << 31):
            raise BadRequest("selfie pick: set is a set's number")
        if not (type(args["index"]) is int and 0 <= args["index"] <= 3):
            raise BadRequest("selfie pick: index is a photo's place in its set, 0-3")
    if action == "undo" and not (type(args["pick"]) is int and 1 <= args["pick"] < 1 << 63):
        raise BadRequest("selfie undo: pick is a change's number")
    return {k: args[k] for k in ("action", "guid", "set", "index", "pick") if k in shape}


def say_args(args) -> dict:
    """A say action's arguments, exactly: {"channel", "text"}, plus "to" (a member's guid) for a
    whisper only; text is at most MAX_SAY_CHARS characters that clean to a line the game takes
    (owner_say.text_verdict). The cleaned arguments, or BadRequest with the sentence the console
    shows."""
    from leeroy_mind.owner_say import WORDS, text_verdict
    if not isinstance(args, dict) or args.get("channel") not in SAY_CHANNELS:
        raise BadRequest("A line goes to guild, officer, group or whisper.")
    channel = args["channel"]
    whisper = channel == "whisper"
    if set(args) != ({"channel", "text", "to"} if whisper else {"channel", "text"}):
        raise BadRequest("A whisper takes exactly channel, text and to." if whisper
                         else f"A {channel} line takes exactly channel and text.")
    text = args["text"]
    if not isinstance(text, str):
        raise BadRequest("A line is text.")
    if len(text) > MAX_SAY_CHARS:
        raise BadRequest(WORDS["too_long"])
    verdict, cleaned = text_verdict(text)
    if verdict != "ok":
        raise BadRequest(WORDS[verdict])
    if not whisper:
        return {"channel": channel, "text": cleaned}
    if not _count(args["to"], 1):
        raise BadRequest("A whisper goes to a member's guid.")
    return {"channel": channel, "text": cleaned, "to": args["to"]}


def _row_id(v) -> bool:
    return type(v) is int and 1 <= v < 1 << 63


def board_args(args) -> dict:
    """A board action's arguments, exactly (BOARD_SHAPES): kind "post" or "letter", to (the post
    or letter he answers) and id (a reply) row numbers from 1 below 2**63, never a boolean; text
    a string of at most board.RAW_MAX characters that encodes as UTF-8 and cleans to a reply its
    kind may hold (board.text_problem). The arguments with the cleaned text, or BadRequest with
    the sentence the console shows."""
    from leeroy_mind.board import KINDS, text_problem
    if not isinstance(args, dict) or args.get("action") not in BOARD_ACTIONS:
        raise BadRequest("board: an action, one of " + ", ".join(BOARD_ACTIONS))
    action = args["action"]
    shape = BOARD_SHAPES[action]
    if set(args) != shape:
        raise BadRequest(f"board {action}: exactly " + ", ".join(sorted(shape - {"action"})))
    if action == "withdraw":
        if not _row_id(args["id"]):
            raise BadRequest("board withdraw: id is a reply's number")
        return {"action": "withdraw", "id": args["id"]}
    kind, to, text = args["kind"], args["to"], args["text"]
    if not isinstance(kind, str) or kind not in KINDS:
        raise BadRequest("board reply: kind is post or letter")
    if not _row_id(to):
        raise BadRequest(f"board reply: to is the {kind}'s number")
    if not isinstance(text, str):
        raise BadRequest("board reply: text is text")
    problem, cleaned = text_problem(kind, text)
    if problem is not None:
        raise BadRequest(problem)
    return {"action": "reply", "kind": kind, "to": to, "text": cleaned}


def library_args(args) -> dict:
    """A library call's arguments: {"query"} or {"query", "k"}; the query 1-200 characters once
    its whitespace is collapsed, k 1-12 (12 when absent). The cleaned arguments."""
    from leeroy_mind.console.libraryview import K, QUERY_MAX, clean_query
    if not isinstance(args, dict) or "query" not in args or not set(args) <= {"query", "k"}:
        raise BadRequest("A library search takes query, and optionally k.")
    query = clean_query(args["query"])
    if query is None:
        raise BadRequest(f"A library question is 1 to {QUERY_MAX} characters.")
    k = args.get("k", K)
    if type(k) is not int or not 1 <= k <= K:
        raise BadRequest(f"k is 1 to {K}.")
    return {"query": query, "k": k}


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
        raise BadRequest("bad request: exactly v, key, op (and args for a raid, tune, selfie, "
                         "say or board action)")
    given = value["key"]
    if not isinstance(given, str) or not secrets.compare_digest(
            given.encode("utf-8", "surrogatepass"), key.encode("ascii")):
        raise Forbidden("forbidden")
    if type(value["v"]) is not int or value["v"] != VERSION:
        raise BadRequest(f"bad request: v is {VERSION}")
    op = value["op"]
    if op not in OPS:
        raise BadRequest("bad request: op is one of " + ", ".join(OPS))
    parse_args = {"raid": raid_args, "tune": tune_args, "selfie": selfie_args,
                  "say": say_args, "board": board_args, "library": library_args}.get(op)
    if parse_args is None:
        if "args" in value:
            raise BadRequest(f"bad request: {op} takes no args")
        return op, None
    if "args" not in value:
        raise BadRequest(f"bad request: {op} needs args")
    return op, parse_args(value["args"])


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
                 read_s: float = READ_S, raid_s: float = RAID_S, say_s: float = SAY_S,
                 bank_s: float = BANK_READ_S, library_s: float = LIBRARY_READ_S, clock=time.monotonic):
        self.app, self.cfg = app, cfg
        self.key = key or secrets.token_urlsafe(32)
        self.path = Path(app.data_dir) / KEY_FILE
        self.read_s, self.raid_s, self.say_s = read_s, raid_s, say_s
        self.port: int | None = None
        self.listening = asyncio.Event()
        self._connections = 0
        self._raid = asyncio.Lock()
        self._tune = asyncio.Lock()  # one tuning change at a time
        self._preview = asyncio.Lock()  # one preview at a time
        self._selfie = asyncio.Lock()  # one photo change at a time, until its files are written
        self._say = asyncio.Lock()  # one line of his in flight
        self._board = asyncio.Lock()  # one Board reply or withdrawal at a time
        self._library = asyncio.Lock()  # one library search in flight
        self.bank_s, self._clock = bank_s, clock
        self.library_s = library_s
        self._bank_task: asyncio.Future | None = None  # the read in flight, shared by askers
        self._bank_cache: tuple[float, dict] | None = None

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
        if op == "say":
            reply = await self.say(args)
            log_event("control_request", op=op, channel=args["channel"],
                      outcome="ok" if reply["ok"] else "refused")
            return reply, None
        if op == "bank":
            reply = await self.bank()
            log_event("control_request", op=op, outcome="ok" if reply["ok"] else "refused")
            return reply, None
        if op == "library":
            reply = await self.library(args)
            log_event("control_request", op=op, outcome="ok" if reply["ok"] else "refused")
            return reply, None
        reply = await {"raid": self.raid, "tune": self.tune, "selfie": self.selfie,
                       "board": self.board}[op](args)
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
                "raid": raid, **self._health()}

    def _health(self) -> dict:
        """For the health check: bridge_connected (the bridge connection is open and has had
        its hello), cast (the cast members in the bridge's last snapshot) and schema_version
        (mind.db's user_version). Each is None when this app cannot tell."""
        def read(get):
            try:
                return get()
            except Exception:
                return None
        app = self.app
        return {"bridge_connected": read(lambda: bool(app.bridge.greeted)),
                "cast": read(lambda: len(app.world.cast_guids())),
                "schema_version": read(lambda: int(app.store.conn.execute(
                    "PRAGMA user_version").fetchone()[0]))}

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

    async def selfie(self, args: dict) -> dict:
        """Profile photos (leeroy_mind.selfies.service): status at any time; a pick, undo or
        "4 more" one at a time, the lock held through the whole change, its files written off
        the loop included, with no timeout (a cancelled wait would free the lock while those
        files are still being written). Pick, undo and more answers carry the member's guid."""
        service = getattr(self.app, "selfies", None)
        if self.app.stopping:
            return {"ok": False, "error": "the mind is stopping"}
        if service is None:
            return {"ok": False, "error": "photos are not running in this mind"}
        if args["action"] == "status":
            return await service.handle(args)
        if self._selfie.locked():
            return {"ok": False, "error": "another photo change is still being written"}
        async with self._selfie:
            return await service.handle(args)

    async def bank(self) -> dict:
        """The guild bank, read through guild life's armory in a worker thread. Askers share
        the read in flight; one that runs past bank_s is answered, and the read goes on and
        fills the cache. Errors are words, never the database's text (it can hold a secret)."""
        guild = getattr(self.app, "guild", None)
        if self.app.stopping:
            return {"ok": False, "error": "the mind is stopping"}
        if guild is None:
            return {"ok": False, "error": "Guild life is not running in this mind."}
        cached = self._bank_cache
        if cached is not None and self._clock() - cached[0] < BANK_CACHE_S:
            return cached[1]
        task = self._bank_task
        if task is None or task.done():
            try:
                guild_id = int(self.app.store.get_meta("guild_id") or 0)
            except (TypeError, ValueError):
                guild_id = 0
            if not 0 < guild_id <= 0xFFFFFFFF:
                return {"ok": False, "error": "The mind doesn't know the guild yet "
                                              "(no game hello since it started)."}
            armory = getattr(guild, "armory", None)
            if armory is None or not getattr(armory, "available", True):
                return {"ok": False, "error": "The mind can't read the game database "
                                              "(worldserver.conf or mysql.exe not found)."}
            task = self._bank_task = asyncio.ensure_future(self._read_bank(armory, guild_id))
        done, _ = await asyncio.wait({task}, timeout=self.bank_s)
        if task not in done:
            return {"ok": False, "error": "The game database is slow to answer. "
                                          "Try again in a moment."}
        return task.result()

    async def _read_bank(self, armory, guild_id: int) -> dict:
        try:
            bank = await asyncio.to_thread(armory.bank, guild_id)
        except Exception as e:  # its text can carry credentials or game data: the class only
            log_event("control_bank_failed", level=logging.WARNING, error=type(e).__name__)
            if not getattr(armory, "available", True) or (
                    type(e).__name__ == "RosterError"
                    and str(e).startswith(("mysql did not run", "guild armory unavailable"))):
                return {"ok": False, "error": "The mind can't read the game database "
                                              "(worldserver.conf or mysql.exe not found)."}
            return {"ok": False, "error": f"The game database didn't answer ({type(e).__name__})."}
        reply = {"ok": True, "read_at": round(time.time(), 3), "guild": guild_id, **bank}
        self._bank_cache = (self._clock(), reply)
        return reply

    async def library(self, args: dict) -> dict:
        """The mind's semantic search over library.db. Read-only and cheap, but it uses the
        embedding model, so it only asks for one Ollama already holds (GET /api/ps): it never
        loads a model, and uses no chat model. Errors are words; the query is never logged."""
        from leeroy_mind.console.libraryview import hit
        from leeroy_mind.library.search_worker import EmbedFailed, SearchBusy, SearchClosed
        from leeroy_mind.llm.client import LLMError
        from leeroy_mind.nightly.gpu import norm
        if self.app.stopping:
            return {"ok": False, "error": "the mind is stopping"}
        worker = getattr(self.app, "library_search", None)
        library = getattr(self.app, "library", None)
        if worker is None or library is None:
            return {"ok": False, "error": "The mind has no library open (library.db is missing "
                                          "or unreadable)."}
        if self._library.locked():
            return {"ok": False, "error": "Another library search is running."}
        async with self._library:
            model = self.app.cfg.models.embedding
            arbiter = getattr(self.app, "arbiter", None)
            if arbiter is None:
                return {"ok": False, "error": "The mind can't tell whether the embedding model "
                                              "is loaded."}
            try:
                loaded = {norm(n) for n in await arbiter.admin.loaded_models()}
            except (LLMError, OSError):
                return {"ok": False, "error": "Ollama didn't say which models are loaded."}
            if norm(model) not in loaded:
                return {"ok": False, "error": "The embedding model is not loaded right now, so "
                                              "the mind can't search deeper."}
            if not library._has_vectors() or library.get_meta("embed_model") not in (None, model):
                return {"ok": False, "error": "The library has no embeddings for this model "
                                              "yet, so there is no deeper search."}
            try:
                chunks = await worker.search(args["query"], args["k"], llm=self.app.scheduler,
                                             timeout=self.library_s, require_vector=True)
            except TimeoutError:
                return {"ok": False, "error": "The embedding model is slow to answer. Try again "
                                              "in a moment."}
            except SearchBusy:
                return {"ok": False, "error": "Another library search is running."}
            except EmbedFailed:  # no vector was used: these would be text hits, not a deeper search
                return {"ok": False, "error": "The mind couldn't run a deeper search just now."}
            except SearchClosed:
                return {"ok": False, "error": "The library is closing."}
            except Exception as e:  # never its text: it could carry the question
                log_event("control_library_failed", level=logging.WARNING, error=type(e).__name__)
                return {"ok": False, "error": f"The library search failed ({type(e).__name__})."}
        hits = [hit(c) for c in chunks]
        reply = {"ok": True, "hits": hits}
        while hits and len(encode(reply)) > MAX_REPLY - 1024:  # ascii escapes can swell a reply
            hits.pop()
        return reply

    async def say(self, args: dict) -> dict:
        """His line (leeroy_mind.owner_say): one at a time, answered within say_s. Never
        cancelled from here: a line still waiting on the game holds the next off until the
        game (or the bridge's own ack wait) answers, so two are never in flight."""
        from leeroy_mind.owner_say import NO_ANSWER, STOPPING
        speech = getattr(self.app, "speech", None)
        if self.app.stopping:
            return {"ok": False, "error": STOPPING}
        if speech is None:
            return {"ok": False, "error": "Replies are not running in this mind."}
        if self._say.locked():
            return {"ok": False, "error": "Another line is still being sent."}
        await self._say.acquire()
        task = asyncio.ensure_future(speech.handle(args))

        def finished(t):
            self._say.release()
            if not t.cancelled():
                t.exception()  # retrieved: an answered caller may no longer be waiting
        task.add_done_callback(finished)
        done, _ = await asyncio.wait({task}, timeout=self.say_s)
        if task not in done:
            return {"ok": False, "error": NO_ANSWER}
        try:
            return task.result()
        except Exception as e:  # its text can carry the line or a name
            log_event("control_say_failed", level=logging.WARNING, error=type(e).__name__)
            return {"ok": False, "error": f"The line could not be sent ({type(e).__name__})."}

    async def board(self, args: dict) -> dict:
        """His Board reply or withdrawal (leeroy_mind.board.BoardDesk), one at a time: a second
        while one is being saved is refused, never queued."""
        desk = getattr(self.app, "board", None)
        if self.app.stopping:
            return {"ok": False, "error": "The mind is stopping."}
        if desk is None:
            return {"ok": False, "error": "Board replies are not running in this mind."}
        if self._board.locked():
            return {"ok": False, "error": "Another reply is still being saved."}
        async with self._board:
            try:
                return await desk.handle(args)
            except Exception as e:  # its text can carry the reply or a name
                log_event("control_board_failed", level=logging.WARNING, error=type(e).__name__)
                return {"ok": False,
                        "error": f"The reply could not be saved ({type(e).__name__})."}


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
