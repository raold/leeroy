"""Exercise the actual subprocess handshake, including failed and missing checks."""
import contextlib
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from leeroy_dojo.realm.mechanics import MechanicConnection, feed_arguments
from leeroy_dojo.realm.offline import TARGETS
from leeroy_dojo.realm.world import WorldError


class MechanicFeedStartupTest(unittest.TestCase):
    def test_adapter_accepts_every_offline_target_map_and_refuses_unrelated_maps(self):
        arguments = ["--layout", "unused-layout.json", "--guild", "1", "--map"]
        for target, (_, map_id, _) in TARGETS.items():
            with self.subTest(target=target):
                self.assertEqual(feed_arguments(arguments + [str(map_id)]).map, map_id)
        for map_id in (0, 1, 999):
            with self.subTest(map_id=map_id), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as refusal:
                    feed_arguments(arguments + [str(map_id)])
                self.assertEqual(refusal.exception.code, 2)

    def feed(self, packet, *, wait=True):
        layout = SimpleNamespace(roots={"source": "."}, path=Path("unused-layout.json"))
        feed = MechanicConnection(layout, guild=1, map_id=649)
        program = "import sys; print(" + repr(json.dumps(packet)) + ", flush=True); "
        program += "sys.stdin.readline()" if wait else "pass"
        feed.command = [sys.executable, "-u", "-c", program]
        return feed

    def test_parent_waits_for_successful_preflight_then_closes_child(self):
        feed = self.feed(dict(kind="dojo-mechanic-feed", ready=False, preflight=True))
        with feed:
            self.assertTrue(feed.checked)
            self.assertFalse(feed.ready)  # A check is not a bridge acknowledgement.
            self.assertEqual(feed.error, "")
            self.assertIsNone(feed.process.poll())
        self.assertEqual(feed.process.returncode, 0)
        self.assertFalse(feed.thread.is_alive())

    def test_missing_failed_or_out_of_order_check_refuses_and_reaps_child(self):
        cases = [
            dict(kind="dojo-mechanic-feed", ready=True),
            dict(kind="dojo-mechanic-feed", ready=False, error="WorldError"),
            dict(kind="dojo-mechanic-feed", ready=True, preflight=True),
            dict(kind="dojo-mechanic-feed", ready=False, preflight=True, error="failed"),
            dict(kind="dojo-mechanic-feed", ready=False),
        ]
        for packet in cases:
            with self.subTest(packet=packet):
                feed = self.feed(packet, wait=False)
                with self.assertRaises(WorldError):
                    feed.__enter__()
                self.assertIsNotNone(feed.process.poll())
                self.assertFalse(feed.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
