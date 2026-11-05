"""The shared golden fixture: modules/mod-dojo/tests/fixtures/dojo_protocol.jsonl, also run by the C++ tests."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from leeroy_dojo.lifecycle import Lifecycle
from leeroy_dojo.protocol import MAX_TEXT_BYTES, Bounds, decode_request, encode, is_blank_line

FIXTURES = Path(__file__).resolve().parents[2] / "modules" / "mod-dojo" / "tests" / "fixtures"
FIXTURE = FIXTURES / "dojo_protocol.jsonl"
GENERATOR = FIXTURES / "make_dojo_protocol.py"


def load_cases():
    # Lines end in "\n" only: str.splitlines() would also split inside a string holding U+0085 or U+2028.
    return [json.loads(line) for line in FIXTURE.read_bytes().decode("utf-8").split("\n") if line.strip()]


def same(a, b) -> bool:
    """JSON equality as nlohmann sees it: a bool is never a number, 1 and 1.0 are the same number."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


class GoldenFixtureTest(unittest.TestCase):
    def test_fixture_matches_its_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "dojo_protocol.jsonl"
            subprocess.run([sys.executable, "-I", str(GENERATOR)], check=True, capture_output=True,
                           env={**os.environ, "DOJO_FIXTURE_OUT": str(out)})
            self.assertEqual(out.read_bytes(), FIXTURE.read_bytes(), "re-run make_dojo_protocol.py")

    def test_every_case(self):
        cases = load_cases()
        self.assertEqual(cases[0]["kind"], "defaults")
        self.assertEqual(cases[0]["max_message_bytes"], MAX_TEXT_BYTES)
        worker = cases[0]["worker"]
        counts = {"info": 0, "frame": 0, "decode": 0, "session": 0}
        for case in cases[1:]:
            counts[case["kind"]] += 1
            with self.subTest(case["name"]):
                getattr(self, f"run_{case['kind']}")(case, worker)
        self.assertGreaterEqual(counts["info"], 5)
        self.assertGreaterEqual(counts["frame"], 10)
        self.assertGreaterEqual(counts["decode"], 60)
        self.assertGreaterEqual(counts["session"], 30)

    def message_fits(self, message: str, where: str):
        self.assertIsInstance(message, str, where)
        self.assertLessEqual(len(message.encode("utf-8")), MAX_TEXT_BYTES, f"{where}: message {message[:80]!r}...")

    def without_message(self, message: dict, where: str) -> dict:
        if message.get("type") == "error":
            self.message_fits(message.get("message"), where)
            message = {k: v for k, v in message.items() if k != "message"}
        return message

    def run_info(self, case, worker):
        try:
            Lifecycle(case["worker"])
            valid = True
        except ValueError:
            valid = False
        self.assertEqual(valid, case["valid"])

    def run_frame(self, case, worker):
        self.assertEqual(is_blank_line(case["line"].encode("utf-8")), case["blank"])

    def run_decode(self, case, worker):
        line = case["line"] if "line" in case else encode(case["json"])
        decoded = decode_request(line, Bounds.from_wire(case.get("bounds")))
        expect = case["expect"]
        self.message_fits(decoded.message, case["name"])
        if expect["code"] is None:
            self.assertIsNotNone(decoded.request, decoded.message)
            self.assertEqual((decoded.request.type, decoded.request.id), (expect["type"], expect["id"]))
        else:
            self.assertIsNone(decoded.request)
            self.assertEqual((decoded.code, decoded.id), (expect["code"], expect["id"]))

    def run_session(self, case, worker):
        bounds = Bounds.from_wire(case.get("bounds"))
        lc = Lifecycle(worker, bounds, booted=case.get("booted", True))
        for index, op in enumerate(case["script"]):
            where = f"{case['name']} #{index} {op['op']}"
            kind = op["op"]
            if kind == "callback":
                self.assertEqual(lc.accepts_callback(op["generation"]), op["accepted"], where)
                continue
            if kind == "connect":
                out = lc.connect(op["session"])
            elif kind == "disconnect":
                out = lc.disconnect()
            elif kind == "recv":
                line = op["line"] if "line" in op else encode(op["json"])
                out = lc.handle(decode_request(line, bounds))
            elif kind == "complete_reset":
                out = lc.complete_reset(op["generation"], op["token"], op["payload"])
            elif kind == "complete_step":
                out = lc.complete_step(op["generation"], op["token"], op["payload"])
            elif kind == "fault":
                out = lc.fault()
            elif kind == "drained":
                out = lc.drained()
            elif kind == "boot_complete":
                out = lc.boot_complete()
            else:
                self.fail(f"{where}: unknown op")
            sent = [self.without_message(json.loads(encode(m)), where) for m in out.send]
            expected_send = op.get("send", [])
            self.assertTrue(same(sent, expected_send), f"{where}\n sent {sent}\n want {expected_send}")
            self.assertTrue(same(out.effects, op.get("effects", [])),
                            f"{where}\n effects {out.effects}\n want {op.get('effects', [])}")
            self.assertEqual(out.stale, op.get("stale", False), where)
            if "state" in op:
                self.assertEqual(lc.state, op["state"], where)


if __name__ == "__main__":
    unittest.main()
