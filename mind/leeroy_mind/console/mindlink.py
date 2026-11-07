"""The console's side of the mind's control endpoint (leeroy_mind.endpoint).

The key comes from control.json in the mind's data dir, and goes only to a listener the console
has checked: control.json must name a pid of the mind's own process family (as the status
loop identified it, by its command line), and the TCP table must show that pid listening on
127.0.0.1 at the port it names. A file a crash left, a port another program took since, or a
machine where the table cannot be read: the key is not sent, and the console says why.

Calls are blocking (a few seconds at most): the console makes them in worker threads."""
from pathlib import Path

from .. import endpoint
from . import netinfo

STATUS_S = 3.0
STOP_S = 5.0
RAID_S = endpoint.RAID_S + 5.0
SAY_S = endpoint.SAY_S + 5.0  # the mind answers a line within its own SAY_S
BANK_S = endpoint.BANK_READ_S + 5.0  # the mind answers a bank read within its own BANK_READ_S


class LinkError(Exception):
    """The endpoint was not used; the text says why. unanswered: it was located and checked,
    but did not answer (it may be busy: another try can reach it)."""

    def __init__(self, message: str, *, unanswered: bool = False):
        super().__init__(message)
        self.unanswered = unanswered


class MindLink:
    def __init__(self, data_dir: Path, *, tcp=netinfo.tcp_table, request=endpoint.request):
        self.path = Path(data_dir) / endpoint.KEY_FILE
        self._tcp, self._request = tcp, request

    def locate(self, pids) -> dict:
        """{"pid", "port", "key"} of the running mind's endpoint; LinkError otherwise."""
        pids = {p for p in (pids or ()) if type(p) is int}
        if not pids:
            raise LinkError("the mind is not running")
        record = endpoint.read_key_file(self.path)
        if record is None:
            raise LinkError("the mind has no control endpoint (it is off in mind.toml, the "
                            "running mind predates it, or it is still starting)")
        if record["pid"] not in pids:
            raise LinkError("control.json is not the running mind's (a stopped run left it)")
        conns = self._tcp()
        if conns is None:
            raise LinkError("the endpoint's owner cannot be verified here (no TCP table)")
        if not any(c.state == netinfo.LISTEN and c.local == "127.0.0.1"
                   and c.local_port == record["port"] and c.pid == record["pid"] for c in conns):
            raise LinkError("the mind is not listening where control.json says")
        return record

    def call(self, pids, op: str, args: dict | None = None, *, timeout: float = STATUS_S) -> dict:
        """The endpoint's answer to one request; LinkError when it cannot be had."""
        record = self.locate(pids)
        try:
            return self._request(record["port"], record["key"], op, args, timeout=timeout)
        except (OSError, ValueError) as e:
            raise LinkError(f"the control endpoint did not answer ({type(e).__name__})",
                            unanswered=True) from None
