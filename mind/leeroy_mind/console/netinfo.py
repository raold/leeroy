"""Which TCP connections exist, and which process owns each (Windows GetExtendedTcpTable).
Read-only: nothing connects anywhere. It tells the console whether the mind holds its game
bridge connection without touching the bridge, which takes one mind at a time."""
import os
import socket
import struct
from dataclasses import dataclass

LISTEN, ESTABLISHED = 2, 5


@dataclass(frozen=True)
class Conn:
    local: str
    local_port: int
    remote: str
    remote_port: int
    state: int
    pid: int


def tcp_table() -> list[Conn] | None:
    """Every IPv4 TCP connection with its owning pid; None where this cannot be read."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes as w
    try:
        iphlp = ctypes.WinDLL("iphlpapi")
    except OSError:
        return None
    fn = iphlp.GetExtendedTcpTable
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, ctypes.c_int,
                   w.ULONG]
    fn.restype = w.DWORD
    size = w.DWORD(0)
    TCP_TABLE_OWNER_PID_ALL, AF_INET, INSUFFICIENT = 5, 2, 122
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value or 1)
        rc = fn(buf, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_ALL, 0)
        if rc == 0:
            break
        if rc != INSUFFICIENT:
            return None
    else:
        return None
    raw = buf.raw
    (count,) = struct.unpack_from("<I", raw, 0)
    out = []
    for i in range(count):
        state, laddr, lport, raddr, rport, pid = struct.unpack_from("<6I", raw, 4 + 24 * i)
        out.append(Conn(socket.inet_ntoa(struct.pack("<I", laddr)),
                        socket.ntohs(lport & 0xFFFF), socket.inet_ntoa(struct.pack("<I", raddr)),
                        socket.ntohs(rport & 0xFFFF), state, pid))
    return out


def linked(conns: list[Conn] | None, port: int, pids) -> bool | None:
    """Whether one of `pids` has an established connection to `port` on loopback; None when the
    table could not be read (or no pid is known)."""
    if conns is None or not pids:
        return None
    pids = set(pids)
    return any(c.state == ESTABLISHED and c.remote_port == port and c.pid in pids
               and c.remote.startswith("127.") for c in conns)


def listening(conns: list[Conn] | None, port: int) -> bool | None:
    if conns is None:
        return None
    return any(c.state == LISTEN and c.local_port == port for c in conns)
