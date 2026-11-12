"""Native rotations in temporary directories only; no server or installed logs."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("prune", Path(__file__).resolve().parents[1] / "prune-native-logs.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PruneTests(unittest.TestCase):
    def test_keeps_newest_ten_per_appender_and_all_active_or_unrelated_files(self):
        with tempfile.TemporaryDirectory(prefix="leeroy-log-test-") as temp:
            root = Path(temp)
            bases = ("Playerbots", "Server", "Errors", "Custom Appender")
            for base in bases:
                (root / f"{base}.log").write_text("active")
                for day in range(1, 16):
                    (root / f"{base}.log.2026-11-{day:02} 12-30-40").write_text("rotation")
            unrelated = ["Server.log.bak", "Server.log.1", "Server.log.2026-11-01", "notes.txt"]
            for name in unrelated:
                (root / name).write_text("keep")
            (root / "nested").mkdir()
            (root / "nested" / "Server.log.2026-11-01 12-30-40").write_text("keep")
            self.assertEqual(module.prune(root), 20)
            for base in bases:
                self.assertTrue((root / f"{base}.log").is_file())
                self.assertEqual(len(list(root.glob(f"{base}.log.2026-11-* 12-30-40"))), 10)
                self.assertFalse((root / f"{base}.log.2026-11-05 12-30-40").exists())
                self.assertTrue((root / f"{base}.log.2026-11-06 12-30-40").exists())
            self.assertTrue(all((root / name).exists() for name in unrelated))
            self.assertTrue((root / "nested" / "Server.log.2026-11-01 12-30-40").exists())
            self.assertEqual(module.prune(root), 0)

    def test_directories_and_symlinks_are_not_rotations(self):
        with tempfile.TemporaryDirectory(prefix="leeroy-log-test-") as temp:
            root = Path(temp)
            (root / "Server.log.2026-11-01 12-30-40").mkdir()
            target = root / "unrelated.txt"
            target.write_text("keep")
            link = root / "Server.log.2026-11-02 12-30-40"
            try:
                link.symlink_to(target)
            except OSError:
                pass  # Windows may not grant symlink creation to a regular account.
            self.assertEqual(module.prune(root, keep=0), 0)
            self.assertEqual(target.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
