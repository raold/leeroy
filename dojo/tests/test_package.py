import ast
import sys
import tomllib
import unittest
from pathlib import Path

import leeroy_dojo

PACKAGE = Path(leeroy_dojo.__file__).resolve().parent
PROJECT = PACKAGE.parent


class PackageTest(unittest.TestCase):
    def test_version_matches_pyproject(self):
        meta = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(meta["project"]["version"], leeroy_dojo.__version__)

    def test_imports_only_the_standard_library(self):
        allowed = set(sys.stdlib_module_names) | {"leeroy_dojo"}
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    self.assertIn(name.split(".")[0], allowed, f"{path.name} imports {name}")


if __name__ == "__main__":
    unittest.main()
