import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from leeroy_dojo.__main__ import main
from leeroy_dojo.smoke import run_fake_smoke

PROJECT = Path(__file__).resolve().parents[1]


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dojo-smoke-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_fake_smoke_passes_every_check(self):
        report = run_fake_smoke(self.tmp / "out")
        failed = [c for c in report.checks if c["status"] != "ok"]
        self.assertEqual(failed, [])
        names = {c["check"] for c in report.checks}
        for name in ("duplicate delivery", "cached replay", "forced reconnect", "old session refused",
                     "exactly once", "deterministic replay", "recording b excluded", "missing final chunk",
                     "crash before footer"):
            self.assertIn(name, names)
        self.assertEqual(report.decisions["smoke-b"], 5)
        self.assertEqual(report.decisions["smoke-a"], report.decisions["smoke-c"])

    def test_the_output_holds_only_synthetic_data(self):
        out = self.tmp / "out"
        run_fake_smoke(out)
        text = "\n".join(p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file()).lower()
        for marker in ("acore_", "guid", "guild", "persona", "mind-data", "voice-data", "password", "@"):
            self.assertNotIn(marker, text)
        header = json.loads((out / "episodes" / "smoke-a" / "header.json").read_text(encoding="utf-8"))
        self.assertEqual(header["actors"], [{"slot": 0, "team": 0}, {"slot": 1, "team": 1}])
        self.assertEqual(header["worker"]["kind"], "fake")

    def test_refuses_a_used_output_directory(self):
        out = self.tmp / "out"
        run_fake_smoke(out)
        with self.assertRaises(FileExistsError):
            run_fake_smoke(out)

    def test_cli(self):
        result = subprocess.run([sys.executable, "-m", "leeroy_dojo", "protocol-smoke", "--fake", "--json",
                                 "--out", str(self.tmp / "cli")], cwd=PROJECT, capture_output=True, text=True,
                                timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(main(["protocol-smoke"]), 2)
        episodes = self.tmp / "cli" / "episodes"
        self.assertEqual(main(["verify-episode", str(episodes / "smoke-a")]), 0)
        self.assertEqual(main(["verify-episode", str(episodes / "smoke-b")]), 1)


if __name__ == "__main__":
    unittest.main()
