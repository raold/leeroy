"""The Task 6 acceptance driver's own logic, without a server: log evidence, position search and output parsing."""

import json
import math
import tempfile
import unittest
from pathlib import Path

from leeroy_dojo.realm import control


def trace_line(seq: int, **fields) -> str:
    record = {"seq": seq, "at_ms": 0, "actor": 0, "producer": "control", "kind": "ownership", "action_id": 0,
              "origin": "", "action": "", "input_ms": 0, "due_ms": 0, "exec_ms": 0, "outcome": "", "detail": ""}
    record.update(fields)
    return "2026-11-04 13:00:00 INFO dojo-trace " + json.dumps(record, sort_keys=True)


class ShutdownEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dojo-control-"))
        self.server = self.tmp / "Server.log"
        self.bots = self.tmp / "Playerbots.log"
        self.bots.write_text("Bot Dojofrosta is externally controlled by mod-dojo (epoch 7)\n"
                             "Bot Dojofrostb is externally controlled by mod-dojo (epoch 8)\n"
                             "Bot Dojofrosta is released by mod-dojo (epoch 3; 10 updates skipped, 0 inputs refused)\n",
                             encoding="utf-8")

    def write(self, *lines: str) -> None:
        self.server.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_held_to_the_end(self):
        self.write(trace_line(5, outcome="acquired"),
                   trace_line(11, kind="command", producer="console", action_id=40, outcome="queued"),
                   trace_line(12, kind="command", producer="console", action_id=40, outcome="dropped", detail="shutdown"),
                   trace_line(13, outcome="held", detail="shutdown initiated: epoch 7 stays until the actor logs out"))
        evidence = control.shutdown_evidence(self.server, self.bots, 10, 40, [7, 8])
        self.assertTrue(evidence["ok"], evidence)
        self.assertEqual(len(evidence["pending_dropped"]), 1)
        self.assertEqual(evidence["playerbots_released"], [])   # epoch 3 was the earlier release test

    def test_a_stock_action_or_a_release_fails_it(self):
        self.write(trace_line(13, outcome="held", detail="shutdown initiated: epoch 7"),
                   trace_line(14, kind="command", producer="console", action_id=40, outcome="dropped"),
                   trace_line(15, kind="cast", producer="stock", outcome="observed"))
        self.assertFalse(control.shutdown_evidence(self.server, self.bots, 10, 40, [7, 8])["ok"])
        self.bots.write_text("Bot Dojofrostb is released by mod-dojo (epoch 8; 1 updates skipped, 0 inputs refused)\n",
                             encoding="utf-8")
        self.write(trace_line(13, outcome="held", detail="shutdown initiated: epoch 7"),
                   trace_line(14, kind="command", producer="console", action_id=40, outcome="dropped"))
        self.assertFalse(control.shutdown_evidence(self.server, self.bots, 10, 40, [7, 8])["ok"])

    def test_without_the_shutdown_record_it_fails(self):
        self.write(trace_line(14, kind="command", producer="console", action_id=40, outcome="dropped"))
        self.assertFalse(control.shutdown_evidence(self.server, self.bots, 10, 40, [7, 8])["ok"])


class FakeConsole:
    """A round arena floor at z 10 with one pillar of radius 3 at (10, 0)."""

    @staticmethod
    def height(p):
        if math.hypot(p[0], p[1]) >= 40:
            return -200000.0
        if p[2] > 25 and math.hypot(p[0] - 10, p[1]) < 3:
            return 40.0   # the pillar's top, found only from above it
        return 10.0

    def los(self, a, b):
        ground = [self.height(p) for p in (a, b)]
        ax, ay, bx, by = a[0], a[1], b[0], b[1]
        dx, dy = bx - ax, by - ay
        t = max(0.0, min(1.0, ((10 - ax) * dx + (0 - ay) * dy) / (dx * dx + dy * dy or 1)))
        blocked = math.hypot(ax + t * dx - 10, ay + t * dy) < 3
        return {"map": 559, "los": not blocked, "ground": ground}


class PositionsTest(unittest.TestCase):
    def test_finds_a_clear_and_a_blocked_pair(self):
        found = control.find_positions(FakeConsole(), (0.0, 0.0))
        clear, blocked = found["clear"], found["blocked"]
        self.assertTrue(18 <= clear["distance"] <= 24)
        self.assertLessEqual(blocked["distance"], 26)
        self.assertTrue(FakeConsole().los(clear["a"], clear["b"])["los"])
        self.assertTrue(FakeConsole().los(clear["a"], clear["mirror"])["los"])
        self.assertFalse(FakeConsole().los(blocked["a"], blocked["b"])["los"])
        self.assertAlmostEqual(math.dist(clear["a"][:2], clear["near"][:2]), 6.0, places=3)


class ParsingTest(unittest.TestCase):
    def test_json_after_a_prefix(self):
        self.assertEqual(control._json_after("dojo-los", 'x\ndojo-los {"los":true}\n'), {"los": True})
        with self.assertRaises(control.ControlError):
            control._json_after("dojo-los", "nothing here")

    def test_stock_records_are_only_owned_ones(self):
        records = [{"producer": "stock"}, {"producer": "stock (released)"}, {"producer": "executor"}]
        self.assertEqual(control.stock_records(records), [{"producer": "stock"}])


if __name__ == "__main__":
    unittest.main()
