#!/usr/bin/env python3
"""Smoke test for the mod-leeroy bridge (NDJSON over 127.0.0.1:7878).

Exit codes: 0 all checks passed, 1 a check failed, 2 could not connect or
another mind is already connected.
"""
import argparse
import json
import socket
import sys
import time

MEMBER_KEYS = {"guid", "name", "race", "class", "gender", "level", "rank",
               "online", "map", "zone", "spec_tab"}
SMOKE_TEXT = "bridge smoke test, please ignore"
BUSY = "another mind is connected"


class Conn:
    def __init__(self, host, port, timeout):
        self.timeout = timeout
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""
        self.eof = False

    def send_raw(self, text):
        self.sock.sendall(text.encode("utf-8") + b"\n")

    def send(self, obj):
        self.send_raw(json.dumps(obj))

    def read_line(self, deadline):
        """Next line as str, or None on timeout or EOF (see self.eof)."""
        while b"\n" not in self.buf:
            if self.eof:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                self.eof = True
                return None
            if not data:
                self.eof = True
                return None
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", "replace")

    def read_msg(self, deadline):
        line = self.read_line(deadline)
        if line is None:
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            return {"type": "<unparsable>", "raw": line}
        return msg if isinstance(msg, dict) else {"type": "<not an object>", "raw": line}

    def wait_for(self, pred, timeout=None):
        """Skip unrelated events until pred(msg) is true; None on timeout or EOF."""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            msg = self.read_msg(deadline)
            if msg is None or pred(msg):
                return msg

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Busy(Exception):
    pass


class Results:
    def __init__(self):
        self.failed = 0

    def ok(self, name):
        print("PASS %s" % name)

    def fail(self, name, why):
        self.failed += 1
        print("FAIL %s: %s" % (name, why))


def reply(conn, cmd_id):
    return conn.wait_for(lambda m: m.get("id") == cmd_id and m.get("type") in ("ack", "pong", "error"))


def read_first(conn, timeout):
    """First line of a connection; raises Busy if the bridge turns us away."""
    m = conn.read_msg(time.monotonic() + timeout)
    if m is not None and m.get("type") == "error" and m.get("message") == BUSY:
        raise Busy()
    return m


def run_checks(conn, args, hello):
    r = Results()

    if hello is None:
        r.fail("hello", "no line received")
    elif (hello.get("type") == "hello" and hello.get("v") == 1
          and hello.get("server") == "mod-leeroy" and hello.get("guild_id") == 21):
        r.ok("hello")
    else:
        r.fail("hello", "unexpected first line: %r" % (hello,))

    cast = conn.read_msg(time.monotonic() + args.timeout)
    members = []
    if cast is None:
        r.fail("cast", "no line received")
    elif cast.get("type") != "cast" or not isinstance(cast.get("members"), list):
        r.fail("cast", "unexpected second line: %r" % (cast,))
    else:
        members = cast["members"]
        bad = [m for m in members if not isinstance(m, dict) or set(m) != MEMBER_KEYS]
        if not members:
            r.fail("cast", "members is empty")
        elif bad:
            r.fail("cast", "member keys differ from the 11 expected: %r" % (bad[0],))
        elif args.expect_cast is not None and len(members) != args.expect_cast:
            r.fail("cast", "expected %d members, got %d" % (args.expect_cast, len(members)))
        else:
            r.ok("cast")

    conn.send({"v": 1, "type": "ping", "id": "smoke-ping"})
    m = reply(conn, "smoke-ping")
    if m is None:
        r.fail("ping", "no reply")
    elif m.get("type") == "pong":
        r.ok("ping")
    else:
        r.fail("ping", "expected pong, got %r" % (m,))

    conn.send({"v": 1, "type": "get_cast", "id": "smoke-getcast"})
    m = conn.wait_for(lambda x: x.get("type") == "cast")
    if m is None:
        r.fail("get_cast", "no cast event")
    else:
        r.ok("get_cast")

    conn.send_raw("not json")
    m = conn.wait_for(lambda x: x.get("type") == "error" and x.get("message") == "bad json")
    if m is None:
        r.fail("errors", "no 'bad json' error for a non-JSON line")
    elif "id" in m:
        r.fail("errors", "'bad json' error carries an id: %r" % (m,))
    else:
        conn.send({"v": 1, "type": "dance", "id": "smoke-x"})
        m = conn.wait_for(lambda x: x.get("type") == "error" and x.get("id") == "smoke-x")
        if m is None:
            r.fail("errors", "no error for an unknown type")
        elif m.get("message") != "unknown type: dance":
            r.fail("errors", "expected 'unknown type: dance', got %r" % (m.get("message"),))
        else:
            r.ok("errors")

    conn.send({"v": 1, "type": "say", "id": "smoke-g0", "guid": 0, "channel": "guild", "text": "x"})
    a = reply(conn, "smoke-g0")
    conn.send({"v": 1, "type": "say", "id": "smoke-gx", "guid": 4000000000, "channel": "guild", "text": "x"})
    b = reply(conn, "smoke-gx")
    if a is None or b is None:
        r.fail("say_rejects", "no reply to %s" % ("guid 0" if a is None else "unknown guid"))
    elif not (a.get("type") == "ack" and a.get("ok") is False and a.get("error") == "bad field: guid"):
        r.fail("say_rejects", "guid 0: expected ack ok false 'bad field: guid', got %r" % (a,))
    elif not (b.get("type") == "ack" and b.get("ok") is False and b.get("error") == "not a cast member"):
        r.fail("say_rejects", "unknown guid: expected 'not a cast member', got %r" % (b,))
    else:
        r.ok("say_rejects")

    if args.no_say:
        print("SKIP say_guild (--no-say)")
    else:
        online = [m for m in members if isinstance(m, dict) and m.get("online")]
        if not online:
            r.fail("say_guild", "no cast member is online")
        else:
            guid = online[0]["guid"]
            conn.send({"v": 1, "type": "say", "id": "smoke-say", "guid": guid,
                       "channel": "guild", "text": SMOKE_TEXT})
            ack = chat = None
            end = time.monotonic() + args.timeout
            while ack is None or chat is None:
                m = conn.read_msg(end)
                if m is None:
                    break
                if m.get("type") == "ack" and m.get("id") == "smoke-say":
                    ack = m
                elif (m.get("type") == "chat" and m.get("channel") == "guild"
                      and m.get("from_guid") == guid and m.get("text") == SMOKE_TEXT):
                    chat = m
            if ack is None:
                r.fail("say_guild", "no ack")
            elif ack.get("ok") is not True:
                r.fail("say_guild", "ack not ok: %r" % (ack,))
            elif chat is None:
                r.fail("say_guild", "no matching chat event within %ss" % args.timeout)
            elif chat.get("via_bridge") is not True:
                r.fail("say_guild", "chat event via_bridge is not true: %r" % (chat,))
            else:
                r.ok("say_guild")

    second = None
    try:
        second = Conn(args.host, args.port, args.timeout)
        m = second.read_msg(time.monotonic() + args.timeout)
        if m is None or m.get("type") != "error" or m.get("message") != BUSY:
            r.fail("second_connection", "expected %r, got %r" % (BUSY, m))
        elif second.read_line(time.monotonic() + args.timeout) is None and second.eof:
            r.ok("second_connection")
        else:
            r.fail("second_connection", "connection not closed after the error")
    except OSError as e:
        r.fail("second_connection", "could not connect: %s" % e)
    finally:
        if second:
            second.close()

    if args.listen > 0:
        print("listening for %ss" % args.listen)
        sys.stdout.flush()
        end = time.monotonic() + args.listen
        while True:
            line = conn.read_line(end)
            if line is None:
                break
            print(line)
            sys.stdout.flush()
    return r.failed


def run_send(conn, args, hello):
    if hello is None or hello.get("type") != "hello":
        print("FAIL hello: unexpected first line %r" % (hello,))
        return 1
    print(json.dumps(hello))
    cast = conn.read_msg(time.monotonic() + args.timeout)
    if cast is None or cast.get("type") != "cast":
        print("FAIL cast: unexpected second line %r" % (cast,))
        return 1
    print(json.dumps(cast))
    pending = set()
    for n, raw in enumerate(args.send, 1):
        try:
            cmd = json.loads(raw)
        except ValueError as e:
            print("FAIL --send #%d is not JSON: %s" % (n, e))
            return 1
        if not isinstance(cmd, dict):
            print("FAIL --send #%d is not a JSON object" % n)
            return 1
        cmd["v"] = 1
        cmd.setdefault("id", "smoke-%d" % n)
        cmd["ts"] = int(time.time() * 1000)
        pending.add(cmd["id"])
        conn.send(cmd)
        print("SENT " + json.dumps(cmd))
    end = time.monotonic() + args.wait
    good = set()
    while True:
        line = conn.read_line(end)
        if line is None:
            break
        print(line)
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict) and m.get("id") in pending:
            if m.get("type") == "pong" or (m.get("type") == "ack" and m.get("ok") is True):
                good.add(m["id"])
    missing = sorted(pending - good)
    if missing:
        print("FAIL no ack ok / pong for: %s" % ", ".join(missing))
        return 1
    return 0


def main():
    p = argparse.ArgumentParser(description="Smoke test for the mod-leeroy bridge.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7878)
    p.add_argument("--timeout", type=float, default=5, help="seconds to wait for each reply")
    p.add_argument("--no-say", action="store_true", help="skip the guild say check")
    p.add_argument("--expect-cast", type=int, metavar="N", help="require exactly N cast members")
    p.add_argument("--listen", type=float, default=0, metavar="SECONDS",
                   help="print every event for SECONDS after the checks")
    p.add_argument("--send", action="append", default=[], metavar="JSON",
                   help="send this command (repeatable) instead of running the checks")
    p.add_argument("--wait", type=float, default=5, metavar="SECONDS",
                   help="with --send: print received lines for SECONDS (default 5)")
    args = p.parse_args()

    try:
        conn = Conn(args.host, args.port, args.timeout)
    except OSError as e:
        print("cannot connect to %s:%d: %s" % (args.host, args.port, e))
        return 2
    try:
        try:
            hello = read_first(conn, args.timeout)
        except Busy:
            print("another mind is connected")
            return 2
        if args.send:
            return run_send(conn, args, hello)
        return 1 if run_checks(conn, args, hello) else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
