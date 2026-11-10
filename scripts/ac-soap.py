#!/usr/bin/env python3
"""Send one worldserver console command over SOAP on this machine and print its answer.

Usage: ac-soap.py [--timeout S] <command words...>     e.g.  ac-soap.py server info

The credentials live outside the repo, in %LOCALAPPDATA%\\leeroy\\remote-console.cnf (or the
file LEEROY_REMOTE_CONSOLE names): lines `user=`, `password=` and `soap=127.0.0.1:<port>`,
`#` comments allowed. Only a loopback address is ever dialled. Nothing secret is printed.

Exit 0: the server answered. 1: it refused, faulted or did not answer. 2: not set up (no
credentials file, no soap line, a non-loopback address) or bad usage.
"""
import argparse
import base64
import html
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def credentials_path() -> Path:
    named = os.environ.get("LEEROY_REMOTE_CONSOLE")
    if named:
        return Path(named)
    return Path(os.environ.get("LOCALAPPDATA", "")) / "leeroy" / "remote-console.cnf"


def read_credentials(path: Path) -> dict:
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip()
    return cfg


def envelope(command: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:ns1="urn:AC"><SOAP-ENV:Body><ns1:executeCommand><command>'
            + html.escape(command) + '</command></ns1:executeCommand></SOAP-ENV:Body>'
            '</SOAP-ENV:Envelope>').encode("utf-8")


def field(text: str, tag: str):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return html.unescape(m.group(1)).strip() if m else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="one worldserver console command over local SOAP")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("command", nargs="+")
    args = ap.parse_args(argv)
    command = " ".join(args.command).strip()
    if not command:
        print("ac-soap: no command given", file=sys.stderr)
        return 2
    path = credentials_path()
    try:
        cfg = read_credentials(path)
    except OSError:
        print(f"ac-soap: remote console not set up (no {path})", file=sys.stderr)
        return 2
    host, _, port = cfg.get("soap", "").rpartition(":")
    host = host.strip("[]")
    if not cfg.get("user") or "password" not in cfg or not port.isdigit():
        print("ac-soap: the credentials file needs user=, password= and soap=host:port",
              file=sys.stderr)
        return 2
    if host not in LOOPBACK:
        print("ac-soap: refusing a non-loopback SOAP address", file=sys.stderr)
        return 2
    auth = base64.b64encode(f"{cfg['user']}:{cfg['password']}".encode("utf-8")).decode("ascii")
    url = f"http://{'[::1]' if host == '::1' else host}:{port}/"
    request = urllib.request.Request(url, data=envelope(command), method="POST", headers={
        "Authorization": "Basic " + auth, "Content-Type": "text/xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        why = field(body, "faultstring") or ("the account was refused" if e.code == 401
                                             else f"HTTP {e.code}")
        print(f"ac-soap: {why}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print(f"ac-soap: no answer from the worldserver ({type(e).__name__})", file=sys.stderr)
        return 1
    fault = field(text, "faultstring")
    if fault is not None:
        print(f"ac-soap: {fault}", file=sys.stderr)
        return 1
    result = field(text, "result")
    print(result if result is not None else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
