"""Episode outcomes and rewards against the shared cases mod-dojo's TestOutcome.cpp also runs."""

import json
import unittest
from pathlib import Path

from leeroy_dojo import outcome

REPO = Path(__file__).resolve().parents[2]
CASES = REPO / "modules" / "mod-dojo" / "tests" / "fixtures" / "outcome_cases.json"


class SharedCasesTest(unittest.TestCase):
    def test_every_case(self):
        doc = json.loads(CASES.read_text(encoding="utf-8"))
        self.assertEqual(doc["shaping_scale"], outcome.SHAPING_SCALE)
        for case in doc["cases"]:
            with self.subTest(case=case["name"]):
                actors = [outcome.Standing(a["team"], a["alive"], a["health"]) for a in case["actors"]]
                got = outcome.decide(actors, case["time_up"])
                self.assertEqual((got.reason, got.winner), (case["outcome"]["reason"], case["outcome"]["winner"]))
                self.assertAlmostEqual(outcome.potential(actors), case["potential"], places=9)
                rewards = outcome.rewards(actors, case["before"], got)
                self.assertEqual(len(rewards), len(case["rewards"]))
                for r, want in zip(rewards, case["rewards"]):
                    self.assertAlmostEqual(r["terminal"], want["terminal"], places=9)
                    self.assertAlmostEqual(r["shaping"], want["shaping"], places=9)

    def test_zero_sum(self):
        for dead_a in (False, True):
            for dead_b in (False, True):
                for time_up in (False, True):
                    actors = [outcome.Standing(0, not dead_a, 0.37), outcome.Standing(1, not dead_b, 0.81)]
                    got = outcome.decide(actors, time_up)
                    self.assertEqual(got.over, dead_a or dead_b or time_up)
                    r = outcome.rewards(actors, -0.2, got)
                    self.assertAlmostEqual(r[0]["terminal"] + r[1]["terminal"], 0.0)
                    self.assertAlmostEqual(r[0]["shaping"] + r[1]["shaping"], 0.0)


if __name__ == "__main__":
    unittest.main()
