"""scripts/dojo/patches.ps1 against a synthetic playerbots checkout: verify, refuse and apply the dojo's series."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "dojo" / "patches.ps1"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

BASE = """void PlayerbotAI::UpdateAI(uint32 elapsed, bool minimal)
{
    if (!bot->Unit::IsFalling())
        bot->SetFallInformation(0, bot->GetPositionZ());

    if (bot->IsAlive() && HasCheat(BotCheatMask::health))
        bot->SetFullHealth();

    if (UpdateAIReaction(elapsed, minimal, true))
        return;

    UpdateAIInternal(elapsed, minimal);
}
"""
GATE = """    if (IsExternallyControlled())
    {
        ++_externalControlSkippedUpdates;
        return;
    }

"""


@unittest.skipUnless(PWSH and GIT, "needs pwsh and git")
class PatchesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dojo-patches-"))
        self.pb = self.tmp / "playerbots"
        self.source = self.tmp / "source"
        (self.pb / "src" / "Bot").mkdir(parents=True)
        self.git("init", "-q", "-b", "master")
        self.ai = self.pb / "src" / "Bot" / "PlayerbotAI.cpp"
        self.ai.write_text(BASE, encoding="utf-8", newline="\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "baseline")
        self.baseline = self.git("rev-parse", "HEAD").strip()
        text = BASE.replace("        bot->SetFallInformation(0, bot->GetPositionZ());\n\n",
                            "        bot->SetFallInformation(0, bot->GetPositionZ());\n\n" + GATE)
        self.ai.write_text(text, encoding="utf-8", newline="\n")
        self.git("commit", "-q", "-am", "Let an external controller own a bot")
        series = self.source / "patches" / "dojo" / "mod-playerbots"
        series.mkdir(parents=True)
        self.git("format-patch", "--quiet", "--no-signature", "-o", str(series), f"{self.baseline}..HEAD")
        data = self.tmp / "data"
        (data / "manifests").mkdir(parents=True)
        provenance = data / "manifests" / "provenance.json"
        provenance.write_text(json.dumps({"kind": "leeroy-dojo-provenance", "baseline": {"playerbots": {"head": self.baseline}}}),
                              encoding="utf-8")
        self.layout = self.tmp / "layout.json"
        self.layout.write_text(json.dumps({"roots": {"source": str(self.source), "playerbots": str(self.pb),
                                                     "data": str(data)}, "provenance": str(provenance)}),
                               encoding="utf-8")

    def tearDown(self):
        def unlock(func, path, _):
            os.chmod(path, 0o700)
            func(path)
        shutil.rmtree(self.tmp, onerror=unlock)

    def git(self, *args: str, repo: Path | None = None) -> str:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@invalid", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@invalid")
        return subprocess.run([GIT, "-C", str(repo or self.pb), *args], check=True, capture_output=True, text=True,
                              env=env).stdout

    def run_script(self, *extra: str) -> tuple[int, dict]:
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@invalid", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@invalid")
        result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-Layout",
                                 str(self.layout), "-Json", *extra], capture_output=True, text=True, encoding="utf-8",
                                timeout=120, env=env)
        return result.returncode, json.loads(result.stdout)

    def test_the_series_matches(self):
        code, report = self.run_script()
        self.assertEqual(code, 0, report)
        self.assertTrue(report["ok"])
        self.assertEqual(report["gate"], "ok")
        self.assertEqual(len(report["series"]), 1)
        self.assertEqual(report["series"][0]["patch_id"], report["commits"][0]["patch_id"])

    def test_an_extra_commit_is_refused(self):
        self.ai.write_text(self.ai.read_text(encoding="utf-8") + "// more\n", encoding="utf-8", newline="\n")
        self.git("commit", "-q", "-am", "something else")
        code, report = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("are not the 1 patches", " ".join(report["problems"]))

    def test_uncommitted_changes_are_refused(self):
        self.ai.write_text(self.ai.read_text(encoding="utf-8") + "// dirty\n", encoding="utf-8", newline="\n")
        code, report = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", " ".join(report["problems"]))

    def test_apply_brings_a_baseline_checkout_up_to_the_series(self):
        self.git("reset", "-q", "--hard", self.baseline)
        code, report = self.run_script()
        self.assertEqual(code, 1)
        self.assertIn("-Apply applies the missing tail", " ".join(report["problems"]))
        code, report = self.run_script("-Apply")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["applied"], [p.name for p in sorted((self.source / "patches" / "dojo" / "mod-playerbots").glob("*.patch"))])
        self.assertIn("IsExternallyControlled", self.ai.read_text(encoding="utf-8"))

    def test_a_misplaced_gate_is_refused(self):
        text = self.ai.read_text(encoding="utf-8").replace(GATE, "")
        text = text.replace("    UpdateAIInternal(elapsed, minimal);\n", GATE + "    UpdateAIInternal(elapsed, minimal);\n")
        self.ai.write_text(text, encoding="utf-8", newline="\n")
        self.git("commit", "-q", "-am", "move the gate")
        series = self.source / "patches" / "dojo" / "mod-playerbots"
        for p in series.glob("*.patch"):
            p.unlink()
        self.git("format-patch", "--quiet", "--no-signature", "-o", str(series), f"{self.baseline}..HEAD")
        code, report = self.run_script()
        self.assertEqual(code, 1)
        self.assertEqual(report["gate"], "misplaced")

    def test_only_git_runs(self):
        text = SCRIPT.read_text(encoding="utf-8")
        lowered = text.lower()
        for forbidden in ("start-process", "invoke-expression", "mysql", "cmake", "worldserver", "bash", "reset --hard",
                          "push", "rebase"):
            self.assertNotIn(forbidden, lowered)
        self.assertNotIn("ReadLines(", text)


if __name__ == "__main__":
    unittest.main()
