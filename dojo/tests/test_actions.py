"""The joint action catalog and its masks: the shared cases mod-dojo's TestActions.cpp also runs."""

import json
import unittest
from pathlib import Path

from leeroy_dojo import actions
from leeroy_dojo.scenario import load_manifest

REPO = Path(__file__).resolve().parents[2]
CASES = json.loads((REPO / "modules" / "mod-dojo" / "tests" / "fixtures" / "action_cases.json").read_text(encoding="utf-8"))
MANIFEST = REPO / "dojo" / "scenarios" / "frost_mirror_small_v1.json"


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.catalog = actions.build_catalog(CASES["kit"])

    def test_size_hash_and_names(self):
        want = CASES["catalog"]
        self.assertEqual(len(self.catalog.entries), want["size"])
        self.assertEqual(self.catalog.document["size"], want["size"])
        self.assertEqual(self.catalog.sha256, want["sha256"])
        self.assertEqual([a.name for a in self.catalog.abilities], want["abilities"])
        for name, index in want["names"].items():
            self.assertEqual(self.catalog.index(name), index, name)
        self.assertTrue(self.catalog.entries[0].noop)
        self.assertEqual(len({e.name for e in self.catalog.entries}), len(self.catalog.entries))

    def test_the_kit_is_the_manifests(self):
        """The catalog's casts are exactly the scenario's kit, in its order (ruling 4: nothing else is castable)."""
        kit = load_manifest(MANIFEST).data["loadout"]["kit"]
        self.assertEqual([(k["spell_id"], k["name"]) for k in CASES["kit"]], [(k["spell_id"], k["name"]) for k in kit])

    def test_bad_kits_are_refused(self):
        for case in CASES["bad_kits"]:
            with self.subTest(error=case["error"]):
                with self.assertRaises(actions.ActionError) as ctx:
                    actions.build_catalog(case["kit"])
                self.assertEqual(str(ctx.exception), case["error"])

    def test_ability_names(self):
        self.assertEqual(actions.ability_name("Frost Nova"), "frost_nova")
        self.assertEqual(actions.ability_name("  Ice--Lance! "), "ice_lance")


class MaskTest(unittest.TestCase):
    def test_shared_cases(self):
        catalog = actions.build_catalog(CASES["kit"])
        for case in CASES["masks"]:
            with self.subTest(case=case["name"]):
                rules = actions.Rules(**case.get("rules", {}))
                result = actions.compute_mask(catalog, actions.Knowledge.from_json(case["knowledge"]), rules)
                self.assertEqual(dict(zip(CASES["catalog"]["abilities"], result.ability_reasons)), case["abilities"])
                self.assertEqual(dict(zip(actions.FACINGS, result.facing_reasons)), case["facings"])
                self.assertEqual(sum(result.mask), case["open"])
                self.assertEqual(result.mask[0], 1)
                for name, bit in case["check"].items():
                    self.assertEqual(result.mask[catalog.index(name)], bit, name)

    def test_noop_is_always_open(self):
        catalog = actions.build_catalog(CASES["kit"])
        for alive in (True, False):
            result = actions.compute_mask(catalog, actions.Knowledge(alive=alive))
            self.assertEqual(result.mask[0], 1)

    def test_a_mask_is_a_pure_function(self):
        """The same knowledge gives the same mask, and computing it leaves the knowledge unchanged."""
        catalog = actions.build_catalog(CASES["kit"])
        knowledge = actions.Knowledge.from_json(CASES["masks"][0]["knowledge"])
        before = json.dumps(knowledge.__dict__, default=lambda o: o.__dict__, sort_keys=True)
        first = actions.compute_mask(catalog, knowledge).mask
        for _ in range(50):
            self.assertEqual(actions.compute_mask(catalog, knowledge).mask, first)
        self.assertEqual(json.dumps(knowledge.__dict__, default=lambda o: o.__dict__, sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
