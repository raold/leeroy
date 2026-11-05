"""The controlled baselines: masks respected, deterministic for a seed and episode, and the authored script's choices."""

import json
import unittest
from pathlib import Path

from leeroy_dojo import actions, observation, policies

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "modules" / "mod-dojo" / "tests" / "fixtures"


def frost() -> tuple[actions.Catalog, observation.Schema]:
    catalog = actions.build_catalog(json.loads((FIXTURES / "action_cases.json").read_text(encoding="utf-8"))["kit"])
    return catalog, observation.build_schema(catalog)


class Situation:
    """A schema-shaped observation built from a few named values (everything else 0)."""

    def __init__(self, schema: observation.Schema, **values: float):
        self.schema = schema
        self.obs = [0.0] * schema.size
        base = {"self.alive": 1, "self.health": 1, "self.mana": 1, "enemy.present": 1, "enemy.visible": 1,
                "enemy.alive": 1, "enemy.health": 1}
        for k in schema.kit:
            base[f"self.kit.{k}.known"] = 1
            base[f"self.kit.{k}.in_range"] = 1
        base.update({k.replace("__", "."): v for k, v in values.items()})
        for name, v in base.items():
            self.obs[schema.index(name)] = float(v)


class FrostDuelistTest(unittest.TestCase):
    def setUp(self):
        self.catalog, self.schema = frost()
        self.policy = policies.FrostDuelist(self.schema, self.catalog)
        self.all = [1] * len(self.catalog.entries)

    def choose(self, mask=None, **values) -> str:
        obs = Situation(self.schema, **values).obs
        return self.catalog.entries[self.policy.act(obs, mask or self.all)].name

    def test_bolts_a_visible_enemy_in_range(self):
        self.assertEqual(self.choose(**{"enemy.distance": 0.75}), "frostbolt/hold/toward")

    def test_never_interrupts_its_own_cast_or_pending_command(self):
        self.assertEqual(self.choose(**{"self.casting": 1}), "noop")
        self.assertEqual(self.choose(**{"self.pending": 1}), "noop")
        self.assertEqual(self.choose(**{"self.queued": 1}), "noop")
        self.assertEqual(self.choose(**{"self.alive": 0}), "noop")

    def test_closes_in_out_of_range_and_waits_on_the_global_cooldown(self):
        self.assertEqual(self.choose(**{"enemy.distance": 1, "self.kit.frostbolt.in_range": 0}), "none/forward/toward")
        self.assertEqual(self.choose(**{"enemy.distance": 0.75, "self.gcd": 1000 / 1500}), "noop")
        self.assertEqual(self.choose(**{"enemy.distance": 0.75, "self.gcd": 500 / 1500}), "frostbolt/hold/toward")

    def test_novas_a_close_enemy_and_lances_with_fingers_of_frost(self):
        self.assertEqual(self.choose(**{"enemy.distance": 6 / 40}), "frost_nova/hold/keep")
        self.assertEqual(self.choose(**{"enemy.distance": 6 / 40, "enemy.rooted": 1}), "frostbolt/hold/toward")
        self.assertEqual(self.choose(**{"enemy.distance": 0.75, "self.fingers_of_frost": 0.5}), "ice_lance/hold/toward")
        self.assertEqual(self.choose(**{"enemy.distance": 0.75, "enemy.frozen": 1}), "ice_lance/hold/toward")

    def test_respects_the_mask(self):
        mask = list(self.all)
        mask[self.catalog.index("frostbolt/hold/toward")] = 0
        self.assertEqual(self.choose(mask, **{"enemy.distance": 0.75}), "noop")
        mask[self.catalog.index("frost_nova/hold/keep")] = 0
        self.assertEqual(self.choose(mask, **{"enemy.distance": 6 / 40}), "noop")

    def test_walks_on_while_the_enemy_is_out_of_sight(self):
        self.assertEqual(self.choose(**{"enemy.visible": 0}), "none/forward/keep")


class RandomLegalTest(unittest.TestCase):
    def setUp(self):
        self.catalog, self.schema = frost()

    def run_episode(self, seed: int, episode: str, masks: list[list[int]]) -> list[int]:
        p = policies.RandomLegal(self.schema, self.catalog, seed)
        p.reset(episode)
        obs = [0.0] * self.schema.size
        return [p.act(obs, m) for m in masks]

    def test_only_open_actions_and_the_same_sequence_for_a_seed_and_episode(self):
        masks = []
        for i in range(50):
            m = [1 if (j + i) % 3 == 0 else 0 for j in range(len(self.catalog.entries))]
            m[0] = 1
            masks.append(m)
        a = self.run_episode(7, "e1", masks)
        self.assertEqual(a, self.run_episode(7, "e1", masks))
        self.assertNotEqual(a, self.run_episode(7, "e2", masks))
        self.assertNotEqual(a, self.run_episode(8, "e1", masks))
        for choice, m in zip(a, masks):
            self.assertEqual(m[choice], 1)

    def test_a_plain_reset_keeps_the_episode(self):
        p = policies.RandomLegal(self.schema, self.catalog, 3)
        p.reset("ep")
        first = [p.act([0.0] * self.schema.size, [1] * len(self.catalog.entries)) for _ in range(5)]
        p.reset()
        self.assertEqual(first, [p.act([0.0] * self.schema.size, [1] * len(self.catalog.entries)) for _ in range(5)])


class MakeTest(unittest.TestCase):
    def test_from_a_description(self):
        catalog, schema = frost()
        for name in policies.POLICIES:
            p = policies.POLICIES[name](schema, catalog, 5)
            again = policies.make(p.describe(), schema, catalog)
            self.assertEqual((type(again), again.seed), (type(p), 5))
        with self.assertRaises(ValueError):
            policies.make({"name": "learned", "version": 1}, schema, catalog)
        with self.assertRaises(ValueError):
            policies.make({"name": "frost_duelist", "version": 99}, schema, catalog)

    def test_wire_names(self):
        self.assertEqual(policies.wire_name("frostbolt/hold/toward"), "frostbolt.hold.toward")


if __name__ == "__main__":
    unittest.main()
