import json
import socket
import time
import unittest

from leeroy_dojo.protocol import Bounds
from leeroy_dojo.transport import LoopbackServer


class Peer:
    """A raw controller socket with its own line buffer."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buffer = bytearray()

    def send(self, data: bytes | str) -> None:
        self.sock.sendall(data.encode() if isinstance(data, str) else data)

    def line(self, timeout: float = 3.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(65536)
            except (TimeoutError, ConnectionError):
                return None
            if not chunk:
                return None
            self.buffer += chunk
        end = self.buffer.index(b"\n")
        line = bytes(self.buffer[:end])
        del self.buffer[:end + 1]
        return json.loads(line)

    def closed(self, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(deadline - time.monotonic(), 0.01))
            try:
                if not self.sock.recv(65536):
                    return True
            except TimeoutError:
                return False
            except ConnectionError:
                return True
        return False

    def close(self) -> None:
        self.sock.close()


def status(i) -> str:
    return json.dumps({"v": 1, "type": "status", "id": f"q{i}"}) + "\n"


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.server = LoopbackServer(Bounds(max_line_bytes=1024), inbox_capacity=2, outbox_limit=2, linger_s=0.5)
        self.port = self.server.start()
        self.peers: list[Peer] = []

    def tearDown(self):
        for peer in self.peers:
            peer.close()
        self.server.stop()

    def peer(self) -> Peer:
        p = Peer(self.port)
        self.peers.append(p)
        return p

    def event(self, kind: str | None = None, timeout: float = 3.0):
        e = self.server.poll(timeout)
        self.assertIsNotNone(e, f"no {kind or 'event'}")
        if kind:
            self.assertEqual(e.kind, kind)
        return e

    def test_refuses_a_non_loopback_host(self):
        with self.assertRaises(ValueError):
            LoopbackServer().start("0.0.0.0")

    def test_lines_arrive_decoded_and_in_order(self):
        p = self.peer()
        serial = self.event("connected").connection
        p.send(status(1) + "not json\n\r\n   \n" + status(2).replace("\n", "\r\n"))
        first, second, third = self.event("line"), self.event("line"), self.event("line")
        self.assertEqual((first.connection, first.decoded.request.id), (serial, "q1"))
        self.assertEqual((second.decoded.request, second.decoded.code), (None, "malformed"))
        self.assertEqual(third.decoded.request.id, "q2")
        self.assertIsNone(self.server.poll(0.1), "blank lines are not requests")

    def test_a_second_controller_is_refused(self):
        p1 = self.peer()
        self.event("connected")
        p2 = self.peer()
        answer = p2.line()
        self.assertEqual((answer["type"], answer["code"], answer["id"]), ("error", "busy", None))
        self.assertTrue(p2.closed())
        self.assertEqual(self.server.stats.refused, 1)
        p1.send(status(1))
        self.assertEqual(self.event("line").decoded.request.id, "q1")

    def test_an_oversized_line_is_answered_and_closed(self):
        for payload in (b"x" * 2000, b'{"v":1,"type":"status","id":"q","pad":"' + b"y" * 1100 + b'"}\n'):
            with self.subTest(newline=payload.endswith(b"\n")):
                p = self.peer()
                self.event("connected")
                p.send(payload)
                answer = p.line()
                self.assertEqual((answer["code"], answer["id"]), ("line_too_long", None))
                self.assertTrue(p.closed())
                self.event("disconnected")
                self.assertIsNone(self.server.poll(0.05))

    def test_a_line_at_the_bound_is_accepted(self):
        p = self.peer()
        self.event("connected")
        head = '{"v":1,"type":"close","id":"c","session":"s","reason":"'
        line = head + "z" * (1024 - len(head) - 2) + '"}'
        self.assertEqual(len(line), 1024)
        p.send(line + "\n")
        self.assertEqual(self.event("line").decoded.code, "malformed")   # the reason is over the text bound

    def test_backpressure_pauses_reading_and_drops_nothing(self):
        p = self.peer()
        self.event("connected")
        p.send("".join(status(i) for i in range(40)))
        time.sleep(0.3)
        self.assertLessEqual(self.server.stats.max_inbox_lines, 2)
        ids = [self.event("line").decoded.request.id for _ in range(40)]
        self.assertEqual(ids, [f"q{i}" for i in range(40)])
        self.assertEqual(self.server.stats.lines_in, 40)
        self.assertGreater(self.server.stats.read_pauses, 0)

    def test_an_unread_outbox_pauses_reading(self):
        p = self.peer()
        serial = self.event("connected").connection
        big = {"v": 1, "type": "status_result", "id": "x", "pad": "w" * 1_000_000}
        count = 24   # more than loopback socket buffers hold, so the writer stays blocked until the peer reads
        for _ in range(count):
            self.assertTrue(self.server.send(serial, big))
        p.send("".join(status(i) for i in range(5)))
        time.sleep(0.5)
        self.assertEqual(self.server.stats.lines_in, 0, "nothing is read while responses wait unwritten")
        self.assertGreater(self.server.stats.read_pauses, 0)
        received = [p.line(10) for _ in range(count)]
        self.assertTrue(all(r is not None and len(r["pad"]) == 1_000_000 for r in received))
        ids = [self.event("line").decoded.request.id for _ in range(5)]
        self.assertEqual(ids, [f"q{i}" for i in range(5)])

    def test_sends_for_an_old_connection_are_dropped(self):
        p1 = self.peer()
        first = self.event("connected").connection
        p1.close()
        self.assertEqual(self.event("disconnected").connection, first)
        p2 = self.peer()
        second = self.event("connected").connection
        self.assertNotEqual(first, second)
        self.assertFalse(self.server.send(first, {"v": 1, "type": "status_result", "id": "old"}))
        self.assertTrue(self.server.send(second, {"v": 1, "type": "status_result", "id": "new"}))
        self.assertEqual(p2.line()["id"], "new")
        self.assertEqual(self.server.stats.dropped_sends, 1)

    def test_close_flushes_then_closes(self):
        p = self.peer()
        serial = self.event("connected").connection
        self.server.send(serial, {"v": 1, "type": "close_result", "id": "c1", "ok": True})
        self.server.close(serial)
        self.assertEqual(p.line()["id"], "c1")
        self.assertTrue(p.closed())
        self.assertEqual(self.event("disconnected").connection, serial)
        self.assertFalse(self.server.send(serial, {"v": 1}))

    def test_a_partial_line_is_never_delivered(self):
        p = self.peer()
        self.event("connected")
        p.send('{"v":1,"type":"step","id":"s1","session":"s","episode":"e","decision":0,"act')
        p.close()
        self.event("disconnected")
        self.assertIsNone(self.server.poll(0.1))


if __name__ == "__main__":
    unittest.main()
