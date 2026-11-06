"""The pure page modules under node's own test runner (skipped without node). Run from the test
folder with relative names: the test live-guard refuses a launch whose arguments name the mind's
package."""
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS = Path(__file__).parent / "js"


def test_node_tests_pass():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    files = sorted(p.name for p in TESTS.glob("*.test.mjs"))
    assert files
    r = subprocess.run([node, "--test", *files], cwd=TESTS, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
