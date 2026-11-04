"""The shared golden fixture: modules/mod-dojo/tests/fixtures/dojo_protocol.jsonl, also run by the C++ tests."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from leeroy_dojo.lifecycle import Lifecycle
from leeroy_dojo.protocol import Bounds, decode_request, encode

FIXTURES = Path(__file__).resolve().parents[2] / "modules" / "mod-dojo" / "tests" / "fixtures"
FIXTURE = FIXTURES / "dojo_protocol.jsonl"
GENERATOR = FIXTURES / "make_dojo_protocol.py"


def load_cases():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


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


def without_message(message: dict) -> dict:
    if message.get("type") == "error":
        assert isinstance(message.get("message"), str), message
        message = {k: v for k, v in message.items() if k != "message"}
    return message


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
        worker = cases[0]["worker"]
        counts = {"decode": 0, "session": 0}
        for case in cases[1:]:
            counts[case["kind"]] += 1
            with self.subTest(case["name"]):
                if case["kind"] == "decode":
                    self.run_decode(case)
                else:
                    self.run_session(case, worker)
        self.assertGreaterEqual(counts["decode"], 40)
        self.assertGreaterEqual(counts["session"], 25)

    def run_decode(self, case):
        line = case["line"] if "line" in case else encode(case["json"])
        decoded = decode_request(line, Bounds.from_wire(case.get("bounds")))
        expect = case["expect"]
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
                out = lc.complete_reset(op["generation"], op["payload"])
            elif kind == "complete_step":
                out = lc.complete_step(op["generation"], op["payload"])
            elif kind == "fault":
                out = lc.fault()
            elif kind == "drained":
                out = lc.drained()
            elif kind == "boot_complete":
                out = lc.boot_complete()
            else:
                self.fail(f"{where}: unknown op")
            sent = [without_message(json.loads(encode(m))) for m in out.send]
            expected_send = op.get("send", [])
            self.assertTrue(same(sent, expected_send), f"{where}\n sent {sent}\n want {expected_send}")
            self.assertTrue(same(out.effects, op.get("effects", [])),
                            f"{where}\n effects {out.effects}\n want {op.get('effects', [])}")
            self.assertEqual(out.stale, op.get("stale", False), where)
            if "state" in op:
                self.assertEqual(lc.state, op["state"], where)


if __name__ == "__main__":
    unittest.main()
