import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from leeroy_dojo.recording import (
    EpisodeRecorder, IncompleteEpisode, decision_record, load_transitions, verify_episode,
)


def record(decision: int, terminated: bool = False, truncated: bool = False) -> dict:
    return {"decision": decision, "actors": [{"actor": 0, "obs": [decision / 10], "mask": [1, 1], "action": 1}],
            "events": [{"kind": "damage", "amount": decision}], "terminated": terminated, "truncated": truncated}


class RecordingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dojo-recording-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name="e1", n=10, outcome="terminated", chunk_records=4) -> Path:
        recorder = EpisodeRecorder(self.tmp, name, {"scenario": "test"}, chunk_records=chunk_records)
        for d in range(n):
            last = d == n - 1
            recorder.add(record(d, last and outcome == "terminated", last and outcome == "truncated"))
            self.assertLessEqual(len(recorder._lines), chunk_records)
        return recorder.finish(outcome, reason="test")

    def test_round_trip_across_chunks(self):
        path = self.write()
        self.assertEqual(sorted(p.name for p in path.glob("chunk-*")),
                         ["chunk-000000.jsonl", "chunk-000001.jsonl", "chunk-000002.jsonl"])
        check = verify_episode(path)
        self.assertEqual((check.complete, check.usable, check.transitions, check.problems), (True, True, 10, []))
        self.assertEqual(load_transitions(path), [record(d, d == 9) for d in range(10)])
        footer = json.loads((path / "footer.json").read_text(encoding="utf-8"))
        self.assertEqual([c["records"] for c in footer["chunks"]], [4, 4, 2])
        header = json.loads((path / "header.json").read_text(encoding="utf-8"))
        self.assertEqual((header["kind"], header["episode"], header["scenario"]), ("leeroy-dojo-episode", "e1", "test"))

    def test_a_missing_final_chunk_is_identified(self):
        path = self.write()
        (path / "chunk-000002.jsonl").unlink()
        check = verify_episode(path)
        self.assertFalse(check.usable)
        self.assertIn("chunk 2 is missing (listed in the footer)", check.problems)
        with self.assertRaises(IncompleteEpisode):
            load_transitions(path)

    def test_a_missing_middle_chunk_breaks_the_rest(self):
        path = self.write()
        (path / "chunk-000001.jsonl").unlink()
        check = verify_episode(path)
        self.assertIn("chunk 1 is missing", check.problems)
        self.assertEqual(check.valid_chunks, 1)

    def test_a_crash_before_the_footer_is_incomplete(self):
        recorder = EpisodeRecorder(self.tmp, "e1", {}, chunk_records=4)
        for d in range(6):
            recorder.add(record(d))
        check = verify_episode(recorder.dir)   # two records still buffered, never written
        self.assertFalse(check.complete)
        self.assertEqual((check.valid_chunks, check.transitions), (1, 4))
        self.assertTrue(any("no footer" in p for p in check.problems))

    def test_an_unfinished_chunk_file_is_named(self):
        path = self.write()
        (path / "chunk-000003.jsonl.tmp").write_bytes(b'{"decision":10}\n')
        check = verify_episode(path)
        self.assertIn("chunk 3 is incomplete (chunk-000003.jsonl.tmp)", check.problems)

    def test_a_changed_byte_fails_the_checksum(self):
        path = self.write()
        chunk = path / "chunk-000001.jsonl"
        data = bytearray(chunk.read_bytes())
        data[data.index(b'"amount":5')+9] = ord("6")
        chunk.write_bytes(bytes(data))
        self.assertIn("chunk 1 fails its checksum", verify_episode(path).problems)

    def test_a_chunk_from_another_episode_breaks_the_chain(self):
        one = self.write("e1")
        other = EpisodeRecorder(self.tmp, "e2", {}, chunk_records=4)
        for d in range(8):
            other.add(dict(record(d, d == 7), events=[{"kind": "miss"}]))
        other.finish("terminated")
        shutil.copy(other.dir / "chunk-000001.jsonl", one / "chunk-000001.jsonl")
        self.assertIn("chunk 1 does not follow chunk 0", verify_episode(one).problems)

    def test_an_aborted_episode_is_complete_but_not_usable(self):
        path = self.write(outcome="aborted")
        check = verify_episode(path)
        self.assertEqual((check.complete, check.usable, check.outcome), (True, False, "aborted"))
        with self.assertRaises(IncompleteEpisode):
            load_transitions(path)

    def test_refuses_to_overwrite_an_episode(self):
        self.write()
        with self.assertRaises(FileExistsError):
            EpisodeRecorder(self.tmp, "e1", {})

    def test_a_large_record_flushes_on_bytes(self):
        recorder = EpisodeRecorder(self.tmp, "big", {}, chunk_records=100, chunk_bytes=2048)
        for d in range(5):
            recorder.add(dict(record(d, truncated=d == 4), pad="x" * 1500))
        recorder.finish("truncated")
        self.assertEqual([c["records"] for c in recorder.chunks], [2, 2, 1])   # flushed once 2048 bytes are reached
        self.assertEqual(recorder.max_buffered, 2)
        self.assertTrue(verify_episode(recorder.dir).usable)

    def test_the_footer_outcome_must_match_the_records(self):
        """The reviewer's probe: three non-terminal records under a "terminated" footer used to verify as usable."""
        cases = {
            "terminated": [record(0), record(1), record(2)],
            "truncated": [record(0), record(1, terminated=True)],
        }
        for outcome, records in cases.items():
            recorder = EpisodeRecorder(self.tmp, f"x-{outcome}", {}, chunk_records=4)
            for r in records:
                recorder.add(r)
            check = verify_episode(recorder.finish(outcome))
            self.assertFalse(check.usable, outcome)
            self.assertIn(f"does not end the episode as {outcome}", "; ".join(check.problems))
        empty = EpisodeRecorder(self.tmp, "x-empty", {})
        self.assertIn("a terminated episode has no records", verify_episode(empty.finish("terminated")).problems)
        aborted = EpisodeRecorder(self.tmp, "x-aborted", {})
        aborted.add(record(0))
        check = verify_episode(aborted.finish("aborted"))
        self.assertEqual((check.complete, check.usable, check.problems), (True, False, []))

    def test_a_terminal_record_must_be_the_last(self):
        recorder = EpisodeRecorder(self.tmp, "e1", {}, chunk_records=4)
        for r in (record(0, terminated=True), record(1), record(2, terminated=True)):
            recorder.add(r)
        check = verify_episode(recorder.finish("terminated"))
        self.assertFalse(check.usable)
        self.assertIn("decision 0 ended the episode but more records follow", check.problems)

    def test_records_need_their_flags(self):
        recorder = EpisodeRecorder(self.tmp, "e1", {})
        recorder.add({"decision": 0, "terminated": True})
        check = verify_episode(recorder.finish("terminated"))
        self.assertIn("decision 0 has no terminated and truncated flags", check.problems)

    def test_the_header_is_chained(self):
        """The reviewer's probe: a header replaced after the fact used to verify as usable."""
        path = self.write()
        footer = json.loads((path / "footer.json").read_text(encoding="utf-8"))
        header = (path / "header.json").read_bytes()
        self.assertEqual(footer["header_sha256"], hashlib.sha256(header).hexdigest())
        (path / "header.json").write_text(json.dumps({"kind": "leeroy-dojo-episode", "format": 1, "episode": "zzz",
                                                      "scenario_seed": 999}), encoding="utf-8")
        check = verify_episode(path)
        self.assertFalse(check.usable)
        self.assertIn("chunk 0 does not follow the header", check.problems)
        self.assertIn("header.json is not the header this episode was recorded with", check.problems)
        bare = EpisodeRecorder(self.tmp, "bare", {"scenario": "a"}).finish("aborted")
        (bare / "header.json").write_text(json.dumps({"kind": "leeroy-dojo-episode", "format": 1}), encoding="utf-8")
        self.assertFalse(verify_episode(bare).complete, "a header swap is caught without any chunk")

    def test_episode_ids_must_be_safe_directory_names(self):
        """The reviewer's probe: "..", "C:evil" and "e1:ads" are valid protocol ids but escape or alter the root."""
        for bad in ("..", ".", "C:evil", "e1:ads", ".hidden", "trailing.", "CON", "nul.txt", "com1", "a/b", "a\\b",
                    "", "x" * 65):
            with self.assertRaises(ValueError, msg=bad):
                EpisodeRecorder(self.tmp, bad, {})
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [])
        for good in ("e1", "smoke-a", "run_7.v2", "x" * 64, "CONSOLE", "com10"):
            self.assertTrue(EpisodeRecorder(self.tmp, good, {}).dir.parent == self.tmp, good)

    def test_decision_record_keeps_what_the_policy_saw(self):
        observations = [{"actor": 0, "obs": [1.0], "mask": [1, 0]}, {"actor": 1, "obs": [0.5], "mask": [1, 1]}]
        result = {"id": "s1", "decision": 0, "sim_ms": 1200, "elapsed_ms": 200, "substeps": 20,
                  "requested": [{"actor": 0, "action": 0, "action_id": 0}, {"actor": 1, "action": 1, "action_id": 1}],
                  "observations": [{"actor": 0, "obs": [0.9], "mask": [1, 1]}, {"actor": 1, "obs": [0.4], "mask": [1, 0]}],
                  "rewards": [{"actor": 0, "terminal": 0, "shaping": 0.01}, {"actor": 1, "terminal": 0, "shaping": -0.01}],
                  "events": [{"kind": "executed", "action_id": 1}], "terminated": False, "truncated": False,
                  "outcome": None}
        r = decision_record(observations, result, 1000, log_probs=[-0.1, -2.3])
        self.assertEqual(r["t_ms"], 200)
        self.assertEqual(r["actors"][1], {"actor": 1, "obs": [0.5], "mask": [1, 1], "action": 1, "action_id": 1,
                                          "log_prob": -2.3, "reward": {"terminal": 0, "shaping": -0.01},
                                          "next_obs": [0.4], "next_mask": [1, 0]})
        self.assertEqual(r["events"], result["events"])


if __name__ == "__main__":
    unittest.main()
