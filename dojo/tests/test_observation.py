"""The v1 observation schema against the shared case both languages run, and the manifest's initial expectation."""

import json
import math
import unittest
from pathlib import Path

from leeroy_dojo import actions, observation
from leeroy_dojo.scenario import load_manifest

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "modules" / "mod-dojo" / "tests" / "fixtures"
SCENARIO = REPO / "dojo" / "scenarios" / "frost_mirror_small_v1.json"


def catalog() -> actions.Catalog:
    return actions.build_catalog(json.loads((FIXTURES / "action_cases.json").read_text(encoding="utf-8"))["kit"])


class SchemaTest(unittest.TestCase):
    def test_follows_the_shared_case(self):
        case = json.loads((FIXTURES / "observation_cases.json").read_text(encoding="utf-8"))
        schema = observation.build_schema(catalog())
        self.assertEqual(case["schema"], observation.SCHEMA)
        self.assertEqual([f.name for f in schema.fields], case["names"])
        self.assertEqual(schema.size, case["size"])
        self.assertEqual(schema.sha256, case["sha256"])

    def test_every_field_is_bounded_and_named_once(self):
        schema = observation.build_schema(catalog())
        self.assertEqual(len({f.name for f in schema.fields}), schema.size)
        for f in schema.fields:
            self.assertLessEqual(f.low, f.missing, f.name)
            self.assertLessEqual(f.missing, f.high, f.name)
            self.assertGreater(f.scale, 0, f.name)
        self.assertLessEqual(schema.size, 4096)   # the protocol's observation bound

    def test_slots_carry_presence_visibility_and_age(self):
        names = [f.name for f in observation.build_schema(catalog()).fields]
        for name in ("enemy.present", "enemy.visible", "enemy.age", "self.pending", "event.0.present", "event.0.age",
                     "enemy.kit.frost_nova.unknown", "context.class.mage", "context.tree.2", "match.remaining"):
            self.assertIn(name, names)
        self.assertLess(names.index("context.level"), names.index("self.alive"))
        self.assertLess(names.index("self.alive"), names.index("enemy.present"))
        self.assertLess(names.index("enemy.present"), names.index("event.0.present"))

    def test_decode_and_check(self):
        schema = observation.build_schema(catalog())
        values = [float(f.missing) for f in schema.fields]
        self.assertEqual(schema.check(values), [])
        self.assertEqual(schema.decode(values)["enemy.age"], 1.0)
        bad = list(values)
        bad[schema.index("self.health")] = 1.5
        bad[schema.index("self.alive")] = 0.5
        bad[schema.index("enemy.forward")] = math.nan
        problems = schema.check(bad)
        self.assertEqual(len(problems), 3, problems)
        with self.assertRaises(ValueError):
            schema.decode(values[:-1])


class InitialTest(unittest.TestCase):
    def setUp(self):
        self.manifest = load_manifest(SCENARIO).data
        kit = [{"spell_id": k["spell_id"], "name": k["name"], "target": t, "facing": f}
               for k, t, f in zip(self.manifest["loadout"]["kit"], ("enemy", "enemy", "self"), (True, True, False))]
        self.catalog = actions.build_catalog(kit)
        self.schema = observation.build_schema(self.catalog)
        self.context = {"class_id": 8, "level": 80, "tree_points": [0, 0, 71], "perceptual_delay_ms": 100}

    def test_from_the_manifest_alone(self):
        v = self.schema.decode(observation.expected_initial(self.schema, self.catalog, self.manifest, 0, self.context,
                                                            enemy_visible=False, start_offset_ms=0))
        self.assertEqual(self.schema.check(list(v.values())), [])
        self.assertEqual(v["self.health"], 1.0)
        self.assertEqual(v["self.mana"], 1.0)
        self.assertEqual(v["self.gcd"], 0.0)
        self.assertEqual(v["context.class.mage"], 1.0)
        self.assertEqual(v["context.tree.2"], 1.0)
        self.assertEqual(v["match.remaining"], 1.0)
        self.assertEqual(v["enemy.present"], 0.0)
        self.assertEqual(v["enemy.age"], 1.0)
        self.assertEqual(v["self.kit.frost_nova.in_range"], 1.0)
        self.assertEqual(v["self.kit.frostbolt.in_range"], 0.0)
        start = self.manifest["actors"][0]["start"]
        bounds = self.manifest["arena"]["bounds"]
        self.assertAlmostEqual(v["self.x"], (start["x"] - bounds["x"]) / bounds["radius"], places=6)
        self.assertAlmostEqual(v["self.facing.cos"], math.cos(start["o"]), places=6)
        self.assertEqual(v["self.pending"], 0.0)
        self.assertEqual(v["event.0.present"], 0.0)

    def test_the_two_sides_mirror_each_other(self):
        a = self.schema.decode(observation.expected_initial(self.schema, self.catalog, self.manifest, 0, self.context,
                                                            enemy_visible=True))
        b = self.schema.decode(observation.expected_initial(self.schema, self.catalog, self.manifest, 1, self.context,
                                                            enemy_visible=True))
        self.assertAlmostEqual(a["enemy.distance"], b["enemy.distance"], places=6)
        self.assertEqual(a["enemy.visible"], 1.0)
        self.assertGreater(a["enemy.facing_me"], 0.0)   # both start facing into the arena

    def test_context_against_the_manifest(self):
        self.assertEqual(observation.context_problems(self.context, self.manifest), [])
        wrong = dict(self.context, tree_points=[0, 0, 70], level=79)
        self.assertEqual(len(observation.context_problems(wrong, self.manifest)), 2)

    def test_compare_names_each_difference(self):
        expected = observation.expected_initial(self.schema, self.catalog, self.manifest, 0, self.context,
                                                enemy_visible=False)
        actual = list(expected)
        actual[self.schema.index("self.mana")] = 0.5
        self.assertEqual(observation.compare(expected, actual, self.schema), ["self.mana: 0.5 (expected 1.0)"])
        self.assertEqual(observation.compare(expected, actual, self.schema, skip=("self.mana",)), [])


if __name__ == "__main__":
    unittest.main()
