"""Scenario manifests: the shared accept/refuse cases mod-dojo's TestManifest.cpp also runs, and the pinned hash."""

import copy
import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from leeroy_dojo import scenario
from leeroy_dojo.__main__ import main

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "dojo" / "scenarios" / "frost_mirror_small_v1.json"
CASES = REPO / "modules" / "mod-dojo" / "tests" / "fixtures" / "scenario_cases.json"


def apply(case: dict) -> object:
    if "raw" in case:
        return case["raw"]
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for op in ("set", "remove"):
        if op in case:
            path = case[op]
            node = doc
            for key in path[:-1]:
                node = node[key]
            last = path[-1]
            if op == "remove":
                del node[last]
            elif isinstance(last, int) and last >= len(node):
                node.append(case["value"])
            else:
                node[last] = case["value"]
    return doc


class SharedCasesTest(unittest.TestCase):
    def test_every_shared_case(self):
        cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
        self.assertGreater(len(cases), 30)
        for case in cases:
            with self.subTest(case["name"]):
                doc = apply(case)
                if isinstance(doc, str):
                    try:
                        doc = json.loads(doc)
                    except ValueError:
                        errors = ["manifest is not valid JSON"]
                    else:
                        errors = scenario.validate(doc)
                else:
                    errors = scenario.validate(doc)
                self.assertEqual(not errors, case["ok"], errors)
                if not case["ok"] and "error" in case:
                    self.assertIn(case["error"], "\n".join(errors))


class PinTest(unittest.TestCase):
    def test_the_pinned_hash_is_the_canonical_hash(self):
        m = scenario.load_manifest(MANIFEST)
        self.assertEqual(m.sha256, scenario.pinned_hash(MANIFEST))
        self.assertEqual(m.id, "frost_mirror_small_v1")

    def test_canonical_form(self):
        doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
        text = scenario.canonical(doc).decode("ascii")
        self.assertNotIn('": ', text)
        self.assertTrue(text.startswith('{"actors":'))
        shuffled = dict(reversed(list(doc.items())))
        self.assertEqual(scenario.sha256(shuffled), scenario.sha256(doc))
        changed = copy.deepcopy(doc)
        changed["loadout"]["initial"]["max_mana"] += 1
        self.assertNotEqual(scenario.sha256(changed), scenario.sha256(doc))

    def test_frozen_loadout(self):
        data = scenario.load_manifest(MANIFEST).data
        loadout = data["loadout"]
        self.assertEqual(sum(t["rank"] for t in loadout["talents"]), 71)
        self.assertEqual([k["name"] for k in loadout["kit"]], ["Frostbolt", "Ice Lance", "Frost Nova"])
        self.assertFalse(data["rules"]["rated"])
        self.assertEqual({a["team"] for a in data["actors"]}, {"alliance", "horde"})

    def test_load_refuses_oversize_and_bad_json(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-scenario-"))
        try:
            big = tmp / "big.json"
            big.write_bytes(b" " * (scenario.MAX_BYTES + 1))
            with self.assertRaisesRegex(scenario.ScenarioError, "larger"):
                scenario.load_manifest(big)
            bad = tmp / "bad.json"
            bad.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(scenario.ScenarioError, "JSON"):
                scenario.load_manifest(bad)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cli_pin_writes_the_hash_beside_the_manifest(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-scenario-"))
        try:
            copy_path = tmp / "s.json"
            shutil.copy(MANIFEST, copy_path)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["scenario", "pin", str(copy_path)]), 0)
            pinned = (tmp / "s.sha256").read_text(encoding="ascii").split()
            self.assertEqual(pinned, [scenario.pinned_hash(MANIFEST), "s.json"])
            self.assertEqual(out.getvalue().strip(), pinned[0])
            doc = json.loads(copy_path.read_text(encoding="utf-8"))
            doc["rules"]["rated"] = True
            copy_path.write_text(json.dumps(doc), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["scenario", "check", str(copy_path)]), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
