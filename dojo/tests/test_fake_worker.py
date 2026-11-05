import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from leeroy_dojo import fake_game
from leeroy_dojo.client import ConnectionLost, DojoClient, DojoError, UnexpectedAnswer
from leeroy_dojo.fake_worker import FakeWorker
from leeroy_dojo.protocol import encode


def wait(condition, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def choose(observations):
    return [fake_game.POLICIES[name](o["obs"], o["mask"]) for name, o in zip(("caster", "lancer"), observations)]


class FakeWorkerTest(unittest.TestCase):
    def setUp(self):
        self.worker = FakeWorker(inbox_capacity=4)
        self.port = self.worker.start()
        self.client = self.connect()

    def tearDown(self):
        self.client.disconnect()
        self.worker.stop()

    def connect(self, **options) -> DojoClient:
        client = DojoClient(timeout=3.0, **options)
        client.connect("127.0.0.1", self.port)
        client.hello()
        return client

    def answer(self, client, rid):
        while True:
            message = client.read_message(3.0)
            self.assertIsNotNone(message, f"no answer to {rid}")
            if message.get("id") == rid:
                return message

    def test_hello_describes_the_fake_worker(self):
        hello = self.client.hello_result
        self.assertEqual(hello["worker"]["kind"], "fake")
        self.assertEqual(hello["actions"], list(fake_game.ACTIONS))
        self.assertEqual(hello["bounds"]["max_line_bytes"], 262144)
        self.assertEqual(hello["clock"]["command_delay_ms"], 150)

    def test_an_episode_runs_to_its_outcome_once_per_decision(self):
        reset = self.client.reset("e1", 7)
        observations, decisions = reset["observations"], 0
        while True:
            result = self.client.step(choose(observations))
            self.assertEqual(result["decision"], decisions)
            decisions += 1
            observations = result["observations"]
            if result["terminated"]:
                break
        self.assertIn(result["outcome"]["reason"], ("death", "simultaneous_death"))
        self.assertEqual(self.worker.applied, {("e1", d): 1 for d in range(decisions)})
        with self.assertRaises(DojoError) as refused:
            self.client.step([0, 0])
        self.assertEqual(refused.exception.code, "not_ready")

    def test_duplicate_delivery_applies_once(self):
        self.client.reset("e1", 7)
        message = self.client.step_message([fake_game.BOLT, fake_game.NOOP])
        line = encode(message)
        self.client.send_line(line)
        self.client.send_line(line)
        first, second = self.answer(self.client, message["id"]), self.answer(self.client, message["id"])
        self.assertEqual(first, second)
        self.client.send_line(line)
        self.assertEqual(self.answer(self.client, message["id"]), first)
        self.assertEqual(self.worker.applied[("e1", 0)], 1)
        conflicting = dict(message, actions=[{"actor": 0, "action": 0}, {"actor": 1, "action": 0}])
        self.client.send_line(encode(conflicting))
        self.assertEqual(self.answer(self.client, message["id"])["code"], "conflicting_duplicate")

    def test_a_retry_after_a_timeout_applies_once(self):
        client = self.client
        client.reset("e1", 7)
        client.timeout, client.retries = 0.2, 3
        self.worker.hold_steps()
        threading.Timer(0.5, self.worker.release_steps).start()
        result = client.step([fake_game.LANCE, fake_game.LANCE])
        self.assertGreaterEqual(client.resends, 1)
        self.assertEqual(self.worker.applied[("e1", 0)], 1)
        self.assertEqual(sum(client.owed.values()), client.resends)
        client.timeout = 3.0
        # status() would raise UnexpectedAnswer on any duplicate answer that was not owed.
        self.assertEqual(client.status()["decision"], 1)
        self.assertEqual((client.discarded, client.owed), (client.resends, {}))
        self.assertEqual(result["decision"], 0)

    def test_an_answer_nobody_asked_for_is_refused(self):
        """An answer the client did not send for (or more copies than it sent) breaks the contract: no guessing."""
        self.client.send_line(encode({"v": 1, "type": "status", "id": "stray"}))
        with self.assertRaises(UnexpectedAnswer) as refused:
            self.client.status()
        self.assertEqual(refused.exception.answer["id"], "stray")

    def test_resends_stop_at_the_workers_copy_bound(self):
        client = self.client
        self.assertEqual(client.hello_result["bounds"]["max_copies"], 4)   # the inbox capacity
        client.reset("e1", 7)
        client.timeout, client.retries = 0.05, 50
        self.worker.hold_steps()
        threading.Timer(0.6, self.worker.release_steps).start()
        with self.assertRaises(TimeoutError):
            client.step([fake_game.LANCE, fake_game.LANCE])
        self.assertEqual(client.resends, 3)
        self.assertTrue(wait(lambda: self.worker.applied[("e1", 0)] == 1))
        client.timeout = 3.0
        self.assertEqual(client.status()["decision"], 1, "the four owed answers were all discarded")
        self.assertEqual(client.discarded, 4)

    def test_copies_beyond_the_inbox_are_answered_busy(self):
        """The reviewer's probe over the socket: thousands of copies of a held step, one world advance."""
        self.client.reset("e1", 7)
        self.worker.hold_steps()
        message = self.client.step_message([fake_game.LANCE, 0])
        line = encode(message) + "\n"
        self.client.send_line((line * 200).encode()[:-1])
        answers = [self.answer(self.client, message["id"]) for _ in range(200 - 4)]
        self.assertTrue(all(a["code"] == "busy" for a in answers))
        self.worker.release_steps()
        results = [self.answer(self.client, message["id"]) for _ in range(4)]
        self.assertTrue(all(r["type"] == "step_result" for r in results))
        self.assertIsNone(self.client.read_message(0.3), "nothing more is owed")
        self.assertEqual(self.worker.applied[("e1", 0)], 1)
        server = self.worker.server
        self.assertLessEqual(server.stats.max_outbox, server.outbox_limit + server.inbox_capacity + 4,
                             "unread answers stop the reader line by line, not one socket read later")

    def test_bookkeeping_is_bounded(self):
        worker = FakeWorker(history=3)
        port = worker.start()
        try:
            client = DojoClient(timeout=3.0)
            client.connect("127.0.0.1", port)
            client.hello()
            client.reset("e1", 7)
            for _ in range(5):
                client.step([0, 0])
            for n in range(2, 7):
                client.reset(f"e{n}", 7)
            self.assertEqual(list(worker.applied), [("e1", 2), ("e1", 3), ("e1", 4)])
            self.assertEqual(list(worker.resets), ["e4", "e5", "e6"])
            self.assertEqual(len(worker.aborted), 0)
            client.disconnect()
            self.assertTrue(wait(lambda: ("e6", "disconnect") in worker.aborted))
        finally:
            worker.stop()

    def test_close_while_paused_at_a_boundary(self):
        self.client.reset("e1", 7)
        self.client.step([0, 0])
        closed = self.client.close("done")
        self.assertEqual(closed["type"], "close_result")
        self.assertTrue(wait(lambda: self.worker.lifecycle.state == "ready"))
        self.assertIn(("e1", "closed"), self.worker.aborted)

    def test_close_with_a_step_in_flight(self):
        self.client.reset("e1", 7)
        self.worker.hold_steps()
        step = self.client.step_message([fake_game.BOLT, 0])
        self.client.send_line(encode(step))
        self.assertTrue(wait(lambda: self.worker.lifecycle.state == "stepping"))
        self.client.send_line(encode({"v": 1, "type": "close", "id": "c1", "session": self.client.session}))
        aborted = self.answer(self.client, step["id"])
        self.assertEqual((aborted["code"], aborted["reason"]), ("aborted", "closed"))
        self.assertEqual(self.answer(self.client, "c1")["type"], "close_result")
        with self.assertRaises(ConnectionLost):
            self.client.read_message(2.0)
        self.worker.release_steps()
        self.assertTrue(wait(lambda: self.worker.lifecycle.state == "ready"))
        self.assertEqual(self.worker.applied[("e1", 0)], 0, "an abandoned step never moves the world")

    def test_connection_loss_before_commit(self):
        self.client.reset("e1", 7)
        self.client.step([0, 0])
        line = encode(self.client.step_message([fake_game.LANCE, 0]))
        self.client.sock.sendall(line[: len(line) // 2].encode())   # half a request, no newline
        self.client.disconnect()
        self.assertTrue(wait(lambda: ("e1", "disconnect") in self.worker.aborted))
        self.assertNotIn(("e1", 1), self.worker.applied)
        self.assertEqual(self.worker.lifecycle.state, "ready")

    def test_connection_loss_after_commit_and_reconnect(self):
        self.client.reset("e1", 7)
        old_session = self.client.session
        step = self.client.step_message([fake_game.LANCE, 0])
        self.client.send_line(encode(step))
        self.client.disconnect()
        self.assertTrue(wait(lambda: ("e1", "disconnect") in self.worker.aborted))
        self.assertEqual(self.worker.applied[("e1", 0)], 1, "the step was applied once; only its answer was lost")
        client = self.connect()
        self.assertNotEqual(client.session, old_session)
        with self.assertRaises(DojoError) as refused:
            client.request(dict(step, id="retry", session=old_session))
        self.assertEqual(refused.exception.code, "bad_session")
        with self.assertRaises(DojoError) as refused:
            client.request(dict(step, id="retry2", session=client.session))
        self.assertEqual(refused.exception.code, "not_ready")
        self.assertEqual(client.reset("e2", 7)["decision"], 0)
        self.assertEqual(self.worker.applied[("e1", 0)], 1)
        client.disconnect()

    def test_reconnect_while_a_step_is_in_flight_drains_first(self):
        self.client.reset("e1", 7)
        self.worker.hold_steps()
        self.client.send_line(encode(self.client.step_message([fake_game.LANCE, 0])))
        self.assertTrue(wait(lambda: self.worker.lifecycle.state == "stepping"))
        self.client.disconnect()
        self.assertTrue(wait(lambda: self.worker.lifecycle.state == "closing"))
        client = self.connect()
        self.assertEqual(client.hello_result["state"], "closing")
        with self.assertRaises(DojoError) as refused:
            client.reset("e2", 7)
        self.assertEqual(refused.exception.code, "not_ready")
        self.worker.release_steps()
        self.assertTrue(wait(lambda: self.worker.lifecycle.state == "ready"))
        self.assertEqual(client.reset("e2", 7)["generation"], 3)
        self.assertEqual(self.worker.applied[("e1", 0)], 0)
        client.disconnect()

    def test_a_paused_worker_pushes_back_without_dropping(self):
        self.worker.pause()
        lines = "".join(encode({"v": 1, "type": "status", "id": f"q{i}"}) + "\n" for i in range(30))
        self.client.send_line(lines.encode()[:-1])
        time.sleep(0.3)
        self.assertLessEqual(self.worker.server.stats.max_inbox_lines, 4)
        self.worker.resume()
        ids = [self.client.read_message(3.0)["id"] for _ in range(30)]
        self.assertEqual(ids, [f"q{i}" for i in range(30)])

    def test_an_oversized_request_aborts_the_episode(self):
        self.client.reset("e1", 7)
        self.client.send_line(b"{" + b" " * 300_000 + b"}")
        message = self.client.read_message(3.0)
        self.assertEqual((message["code"], message["id"]), ("line_too_long", None))
        with self.assertRaises(ConnectionLost):
            self.client.read_message(2.0)
        self.assertTrue(wait(lambda: ("e1", "disconnect") in self.worker.aborted))

    def test_the_same_seed_and_policies_replay(self):
        def episode(name):
            reset = self.client.reset(name, 11)
            observations, out = reset["observations"], []
            while True:
                result = self.client.step(choose(observations))
                out.append({k: result[k] for k in ("decision", "requested", "observations", "events", "rewards",
                                                    "terminated", "outcome", "elapsed_ms")})
                observations = result["observations"]
                if result["terminated"]:
                    return out
        self.assertEqual(episode("e1"), episode("e2"))

    def test_the_receipt_names_the_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipt.json"
            self.worker.write_receipt(path)
            receipt = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((receipt["host"], receipt["port"], receipt["worker"]), ("127.0.0.1", self.port, "fake"))


if __name__ == "__main__":
    unittest.main()
