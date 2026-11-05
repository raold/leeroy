"""The controller clears every actor's history and every policy's memory at the episode boundary, and refuses a
result from another episode or an older generation afterwards."""

import unittest

from leeroy_dojo import fake_game
from leeroy_dojo.client import DojoClient
from leeroy_dojo.fake_worker import FakeWorker
from leeroy_dojo.memory import Controller, EpisodeMemory, StaleResult


class CountingPolicy:
    def __init__(self):
        self.resets = 0
        self.seen = []

    def reset(self):
        self.resets += 1
        self.seen.clear()


def choose(observations):
    return [fake_game.POLICIES[name](o["obs"], o["mask"]) for name, o in zip(("caster", "lancer"), observations)]


class BoundaryTest(unittest.TestCase):
    def setUp(self):
        self.worker = FakeWorker()
        port = self.worker.start()
        self.client = DojoClient(timeout=3.0)
        self.client.connect("127.0.0.1", port)
        self.client.hello()
        self.policies = [CountingPolicy(), CountingPolicy()]
        self.memory = EpisodeMemory(2, self.policies)
        self.controller = Controller(self.client, self.memory)

    def tearDown(self):
        self.client.disconnect()
        self.worker.stop()

    def play(self, decisions: int) -> list[dict]:
        results = []
        observations = None
        for _ in range(decisions):
            observations = observations or [{"obs": h.observations[-1], "mask": h.masks[-1]} for h in self.memory.histories]
            result = self.controller.step(choose(observations))
            results.append(result)
            observations = result["observations"]
            for p, o in zip(self.policies, observations):
                p.seen.append(o["obs"])
            if result["terminated"] or result["truncated"]:
                break
        return results

    def test_a_reset_clears_every_history_and_policy(self):
        first = self.controller.reset("m1", 3)
        self.play(5)
        self.assertGreater(len(self.memory.histories[0]), 1)
        self.assertTrue(self.policies[0].seen)
        old_generation = self.memory.generation
        second = self.controller.reset("m2", 4)
        self.assertGreater(second["generation"], first["generation"])
        self.assertEqual(self.memory.generation, second["generation"])
        self.assertGreater(self.memory.generation, old_generation)
        for h, o in zip(self.memory.histories, second["observations"]):
            self.assertEqual(len(h), 1)
            self.assertEqual(h.observations, [o["obs"]])
            self.assertEqual(h.requested, [])
        self.assertEqual([p.resets for p in self.policies], [2, 2])
        self.assertEqual([p.seen for p in self.policies], [[], []])

    def test_a_late_result_of_the_last_episode_is_refused(self):
        self.controller.reset("m1", 3)
        late = self.play(2)[-1]
        self.controller.reset("m2", 4)
        with self.assertRaises(StaleResult):
            self.memory.record(late)
        self.assertEqual(len(self.memory.histories[0]), 1)

    def test_an_older_reset_cannot_restart_the_memory(self):
        old = self.controller.reset("m1", 3)
        self.controller.reset("m2", 4)
        with self.assertRaises(StaleResult):
            self.memory.begin(old)

    def test_an_aborted_episode_takes_nothing_more(self):
        self.controller.reset("m1", 3)
        result = self.play(1)[-1]
        self.memory.abort("disconnect")
        with self.assertRaises(StaleResult):
            self.memory.check(result)
        self.controller.reset("m2", 5)
        self.assertIsNone(self.memory.ended)
        self.assertEqual(self.memory.episode, "m2")


if __name__ == "__main__":
    unittest.main()
