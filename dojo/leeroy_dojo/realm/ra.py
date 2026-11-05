"""A minimal client for the worldserver's remote-access console (RA), bound on the dojo's loopback port.

The protocol is line based: the server sends "Username: " and "Password: " prompts, a message of the day and then an
"AC>" prompt after each command's output. The password is sent on the socket only, never logged or returned.
"""

from __future__ import annotations

import socket
import time

PROMPT = b"AC>"


class RaError(Exception):
    pass


class RaClient:
    def __init__(self, port: int, user: str, password: str, host: str = "127.0.0.1", timeout: float = 30.0):
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise RaError("RA is only used on loopback")
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._buf = b""
        self._timeout = timeout
        try:
            self._read_until(b"Username: ")
            self._send(user)
            self._read_until(b"Password: ")
            self._send(password)
            banner = self._read_until(PROMPT, also=(b"Authentication failed",))
            if b"Authentication failed" in banner:
                raise RaError("RA authentication failed")
        except Exception:
            self.close()
            raise

    def _send(self, line: str) -> None:
        self._sock.sendall(line.encode("utf-8") + b"\r\n")

    def _read_until(self, marker: bytes, also: tuple[bytes, ...] = ()) -> bytes:
        deadline = time.monotonic() + self._timeout
        while True:
            for m in (marker,) + also:
                idx = self._buf.find(m)
                if idx >= 0:
                    out, self._buf = self._buf[:idx + len(m)], self._buf[idx + len(m):]
                    return out
            if time.monotonic() > deadline:
                raise RaError(f"no {marker!r} from RA within {self._timeout:.0f} s")
            chunk = self._sock.recv(65536)
            if not chunk:
                if also:
                    return self._buf
                raise RaError("RA closed the connection")
            # Telnet negotiation bytes (IAC ...) carry nothing for us.
            self._buf += chunk.replace(b"\xff\xf0", b"")

    def command(self, text: str) -> str:
        if "\n" in text or "\r" in text:
            raise RaError("one command per line")
        self._send(text)
        out = self._read_until(PROMPT)
        return out[:-len(PROMPT)].decode("utf-8", errors="replace").replace("\r\n", "\n").strip("\n")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "RaClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
