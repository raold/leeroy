import copy
import unittest

from leeroy_dojo.realm.offline import reached


def sample():
    return dict(token="run", error="", samples=1000,
        native=dict(stage="running", event=dict(id="run", instance_id=42)),
        bodies=[dict(slot=i, guid=i, alive=True, owned=True, pinned=True, map=649, instance=42)
                for i in range(1, 11)],
        slime=dict(mechanics_enabled=True, max_gap_ms=30, inside_samples=20, escapes=2,
            outside_ms=3000, hits=1, late_hits=0, unknown_hits=0,
            pools=[dict(guid=123, casts=21, age_ms=20000, last_ms=20, minimum_radius=2.0, maximum_radius=8.0)]))


class SlimeGateTest(unittest.TestCase):
    def test_requires_real_growth_escape_and_all_owned_bodies(self):
        self.assertTrue(reached(sample(), target="toc-slime-pools", token="run"))
        changes = (
            ("late_hits", 1), ("unknown_hits", 1), ("inside_samples", 0), ("escapes", 0),
            ("outside_ms", 2999), ("max_gap_ms", 201), ("max_gap_ms", 0),
            ("mechanics_enabled", False), ("mechanics_enabled", 1), ("pools", []),
            ("pools", [None]),
        )
        for key, value in changes:
            obs = sample()
            obs["slime"][key] = value
            with self.subTest(key=key, value=value):
                self.assertFalse(reached(obs, target="toc-slime-pools", token="run"))
        for key, value in (("casts", 14), ("age_ms", 19999), ("last_ms", 1501),
                           ("maximum_radius", 2), ("maximum_radius", float("nan")), ("guid", 0)):
            obs = sample()
            obs["slime"]["pools"][0][key] = value
            with self.subTest(pool_key=key):
                self.assertFalse(reached(obs, target="toc-slime-pools", token="run"))
        for key, value in (("alive", False), ("owned", False), ("pinned", False), ("instance", 99),
                           ("map", 631), ("guid", 1)):
            obs = sample()
            obs["bodies"][-1][key] = value
            with self.subTest(body_key=key):
                self.assertFalse(reached(obs, target="toc-slime-pools", token="run"))
        for key in sample()["slime"]:
            obs = sample()
            del obs["slime"][key]
            with self.subTest(missing=key):
                self.assertFalse(reached(obs, target="toc-slime-pools", token="run"))

    def test_control_requires_disabled_responder_and_real_damage(self):
        obs = sample()
        obs["slime"].update(mechanics_enabled=False, inside_samples=0, escapes=0, outside_ms=0)
        self.assertTrue(reached(obs, target="toc-slime-pools-control", token="run"))
        for key, value in (("mechanics_enabled", True), ("hits", 0), ("unknown_hits", 1)):
            bad = copy.deepcopy(obs)
            bad["slime"][key] = value
            with self.subTest(key=key):
                self.assertFalse(reached(bad, target="toc-slime-pools-control", token="run"))


if __name__ == "__main__":
    unittest.main()
