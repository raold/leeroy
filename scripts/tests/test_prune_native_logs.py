"""Native rotations in temporary directories only; no installed logs or services."""
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prune", Path(__file__).resolve().parents[1] / "prune-native-logs.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
NOW = datetime(2026, 11, 21, 12, 30, 40)


class PruneTests(unittest.TestCase):
    def test_retains_busy_days_and_exact_boundary_for_every_appender(self):
        with tempfile.TemporaryDirectory(prefix="leeroy-log-test-") as temp:
            root = Path(temp)
            for base in ("Playerbots", "Server", "Errors", "Custom Appender"):
                for second in range(60):
                    (root / f"{base}.log.2026-11-20 12-30-{second:02}").write_text("recent")
                (root / f"{base}.log.2026-11-07 12-30-40").write_text("boundary")
                (root / f"{base}.log.2026-11-07 12-30-39").write_text("expired")
            self.assertEqual(module.prune(root, now=NOW), 4)
            self.assertEqual(len(list(root.iterdir())), 4 * 61)
            self.assertEqual(module.prune(root, now=NOW), 0)

    def test_preserves_active_unknown_invalid_future_nested_and_directories(self):
        with tempfile.TemporaryDirectory(prefix="leeroy-log-test-") as temp:
            root = Path(temp)
            names = ["Server.log", "Server.log.bak", "Server.log.1", "notes.txt",
                     "Server.log.2026-11-01", "Server.log.2026-02-30 12-30-40",
                     "Server.log.2026-12-01 12-30-40"]
            for name in names:
                (root / name).write_text("keep")
            nested = root / "Server.log.2026-01-01 12-30-40"
            nested.mkdir()
            (nested / "Server.log.2026-01-01 12-30-40").write_text("keep")
            self.assertEqual(module.prune(root, now=NOW), 0)
            self.assertTrue(all((root / name).exists() for name in names))

    def test_symlink_target_is_untouched(self):
        with tempfile.TemporaryDirectory(prefix="leeroy-log-test-") as temp:
            root = Path(temp)
            target = root / "unrelated.txt"
            target.write_text("keep")
            try:
                (root / "Server.log.2026-01-01 12-30-40").symlink_to(target)
            except OSError:
                self.skipTest("symlink privilege unavailable")
            self.assertEqual(module.prune(root, now=NOW), 0)
            self.assertEqual(target.read_text(), "keep")

    def test_filename_not_mtime_and_custom_retention(self):
        with tempfile.TemporaryDirectory(prefix="leeroy-log-test-") as temp:
            root = Path(temp)
            recent = root / "Server.log.2026-11-20 12-30-40"
            recent.touch()
            os.utime(recent, (0, 0))
            (root / "Server.log.2026-11-19 12-30-39").touch()
            self.assertEqual(module.prune(root, days=2, now=NOW), 1)
            self.assertTrue(recent.exists())

    def test_invalid_retention_refuses_before_deleting(self):
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                module.prune(Path("not-a-directory"), value, now=NOW)

    def test_busy_and_concurrently_removed_archives_do_not_fail_startup(self):
        with tempfile.TemporaryDirectory(prefix="leeroy-log-test-") as temp:
            root = Path(temp)
            (root / "Server.log.2026-01-01 12-30-40").touch()
            for error in (PermissionError, FileNotFoundError):
                with patch.object(Path, "unlink", side_effect=error):
                    self.assertEqual(module.prune(root, now=NOW), 0)


if __name__ == "__main__":
    unittest.main()
