"""The flaky report on invented runs and log lines only; it never calls gh or GitHub here."""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("flaky_report", Path(__file__).resolve().parents[1] / "flaky-report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def run(run_id, sha, conclusion, attempt=1, workflow="ci"):
    return {"databaseId": run_id, "headSha": sha, "conclusion": conclusion, "attempt": attempt,
            "workflowName": workflow, "headBranch": "b", "createdAt": "2026-11-24T00:00:00Z",
            "url": f"https://example.invalid/runs/{run_id}"}


class FakeGh:
    """`gh run view --log-failed`: these lines, then this exit status."""
    def __init__(self, lines, returncode=0):
        self.lines, self.code = lines, returncode

    def __call__(self, command, **kw):
        self.stdout, self.returncode = iter(self.lines), None
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.returncode = self.code
        return False


class FlakyReportTests(unittest.TestCase):
    def test_a_failure_its_commit_passed_elsewhere_is_flaky_and_a_lone_one_is_not(self):
        runs = [run(1, "aaa", "success", attempt=2),  # attempt 1 failed, the rerun passed
                run(2, "bbb", "failure"), run(3, "bbb", "success"),  # a push and its PR
                run(4, "ccc", "failure"), run(5, "ccc", "failure"),  # a real failure
                run(6, "bbb", "failure", workflow="build"),  # another workflow's own result
                run(7, "eee", "timed_out"), run(8, "eee", "success")]  # a hang, then a pass
        earlier = {(1, 1): "failure"}
        found = report.flaky(report.attempts(runs, lambda rid, n: earlier[(rid, n)]))
        self.assertEqual(sorted((a["databaseId"], a["attempt"]) for a in found), [(1, 1), (2, 1), (7, 1)])

    def test_cancelled_attempts_are_neither_side(self):
        runs = [run(1, "aaa", "cancelled"), run(2, "aaa", "success"), run(3, "ddd", "failure"),
                run(4, "ddd", "cancelled")]
        self.assertEqual(report.flaky(report.attempts(runs, None)), [])

    def test_test_ids_from_summary_and_verbose_lines_once_each(self):
        lines = [
            "2026-11-22T20:41:00.1234567Z tests/console/e2e/test_x.py::test_a FAILED [ 50%]\n",
            "2026-11-22T20:41:01.1234567Z \x1b[31mFAILED\x1b[0m tests/console/e2e/test_x.py::test_a - Assert\n",
            "2026-11-22T20:41:01.2234567Z ERROR tests/test_y.py::test_b[one-2] - sqlite3.OperationalError\n",
            "2026-11-22T20:41:01.3234567Z E       AssertionError: FAILED tests/not.py::a_test\n",
            "2026-11-22T20:41:01.4234567Z tests/test_y.py::test_c PASSED [ 75%]\n",
        ]
        self.assertEqual(report.test_ids(lines),
                         ["tests/console/e2e/test_x.py::test_a", "tests/test_y.py::test_b[one-2]"])

    def test_parameters_with_spaces_stay_whole_and_apart(self):
        lines = [
            "2026-11-22T20:41:00.1Z tests/test_phantoms.py::test_under_a_second[Thank you.] FAILED [ 10%]\n",
            "2026-11-22T20:41:00.2Z FAILED tests/test_phantoms.py::test_under_a_second[Thank you so much.] - x\n",
            "2026-11-22T20:41:00.3Z FAILED tests/test_phantoms.py::test_under_a_second[Thank you.]\n",
        ]
        self.assertEqual(report.test_ids(lines),
                         ["tests/test_phantoms.py::test_under_a_second[Thank you.]",
                          "tests/test_phantoms.py::test_under_a_second[Thank you so much.]"])

    def test_failed_logs_by_job_and_a_failed_fetch_raises(self):
        attempt = run(9, "fff", "failure")
        lines = ["mind\tRun pytest\t2026-11-22T20:41:00.1Z FAILED tests/a.py::t[x y] - boom\n",
                 "console-e2e\tRun pytest\t2026-11-22T20:41:00.2Z tests/e2e/b.py::u FAILED [ 9%]\n",
                 "mind\tRun pytest\t2026-11-22T20:41:00.3Z 1 failed, 9 passed\n"]
        with patch.object(report.subprocess, "Popen", FakeGh(lines)):
            self.assertEqual(report.failed_logs("o/r", attempt),
                             {"mind": ["tests/a.py::t[x y]"], "console-e2e": ["tests/e2e/b.py::u"]})
        with patch.object(report.subprocess, "Popen", FakeGh(lines, returncode=1)):
            with self.assertRaises(subprocess.CalledProcessError):
                report.failed_logs("o/r", attempt)

    def test_a_timed_out_job_without_test_ids_is_named_by_its_step(self):
        jobs = [{"name": "console-e2e", "conclusion": "timed_out",
                 "steps": [{"name": "Set up", "conclusion": "success"},
                           {"name": "Run pytest", "conclusion": "cancelled"}]},
                {"name": "mind", "conclusion": "failure", "steps": []},
                {"name": "voice", "conclusion": "success", "steps": []}]
        with patch.object(report, "failed_logs", lambda repo, a: {"mind": ["tests/a.py::t"]}), \
                patch.object(report, "jobs", lambda repo, a: jobs):
            self.assertEqual(report.failures("o/r", run(9, "fff", "failure")),
                             ["console-e2e / Run pytest", "tests/a.py::t"])


if __name__ == "__main__":
    unittest.main()
