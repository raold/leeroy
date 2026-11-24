#!/usr/bin/env python3
"""The flaky tests of the last N CI runs: tests that failed and then passed on the same commit.

    scripts/flaky-report.py [--runs 300] [--repo raold/leeroy] [--workflow ci] [--json]

Reads GitHub Actions through the gh CLI, read only. Every attempt of every run of one workflow
on one head commit is compared: a failed or timed-out attempt whose commit also has a passing
attempt (a rerun, or a push run and its pull request run) is a flake, and the tests its failed
jobs name (pytest's FAILED and ERROR lines) are listed, most often first. A failed job that
names no test (a hang cut off by its timeout, a setup step) is listed by job and step. A failure
that no run of its commit passed is an ordinary failure and is left out.

A flaky test is fixed at its cause (a real condition waited on, an injected clock or seed, no
shared state) or deleted with the reason in its pull request, never retried or skipped."""
import argparse
import json
import re
import subprocess
import sys
from collections import defaultdict

BAD = {"failure", "timed_out"}  # did not pass; a cancelled attempt is neither side
# A node id, whose parameters may hold spaces: pytest's short summary ("FAILED tests/x.py::t[a b]
# - why") and its -v progress ("tests/x.py::t[a b] FAILED [ 50%]")
NODE = r"\S+?::[^\s\[]+(?:\[.*?\])?"
SUMMARY = re.compile(rf"^(?:FAILED|ERROR) ({NODE})(?: - |$)")
PROGRESS = re.compile(rf"^({NODE}) (?:FAILED|ERROR)\b")
STAMP = re.compile(r"^\S+Z ")  # each job log line starts with its UTC time
COLOUR = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")  # pytest's colours, when a job has them


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def gh_json(*args: str):
    return json.loads(gh(*args))


def attempts(runs: list[dict], earlier) -> list[dict]:
    """Every attempt of `runs` (gh run list rows, which carry only the latest attempt):
    `earlier(run_id, n)` gives an earlier attempt's conclusion."""
    out = []
    for r in runs:
        for n in range(1, r["attempt"] + 1):
            conclusion = r["conclusion"] if n == r["attempt"] else earlier(r["databaseId"], n)
            out.append({**r, "attempt": n, "conclusion": conclusion})
    return out


def flaky(all_attempts: list[dict]) -> list[dict]:
    """The failed attempts whose workflow passed on the same head commit in another attempt."""
    groups = defaultdict(list)
    for a in all_attempts:
        groups[(a["workflowName"], a["headSha"])].append(a)
    return [a for group in groups.values()
            if any(x["conclusion"] == "success" for x in group)
            for a in group if a["conclusion"] in BAD]


def test_id(text: str) -> str | None:
    """The node id one job log line names as failed or in error, if it does."""
    text = COLOUR.sub("", STAMP.sub("", text.lstrip("﻿").rstrip("\r\n"), count=1))
    m = SUMMARY.match(text) or PROGRESS.match(text)
    return m[1] if m else None


def test_ids(lines) -> list[str]:
    """The test ids a pytest log names as failed or in error, in order, once each."""
    return list(dict.fromkeys(t for t in map(test_id, lines) if t))


def failed_logs(repo: str, attempt: dict) -> dict[str, list[str]]:
    """The test ids in each failed job's log, by job name, from `gh run view --log-failed`
    ("job<TAB>step<TAB>line"), read as it streams: a hung job's log can run to gigabytes. A
    failed fetch raises, so it never reads as a log that names no test."""
    found = defaultdict(dict)
    command = ["gh", "run", "view", str(attempt["databaseId"]), "--repo", repo,
               "--attempt", str(attempt["attempt"]), "--log-failed"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                          errors="replace") as p:
        for line in p.stdout:
            job, _, rest = line.partition("\t")
            name = test_id(rest.partition("\t")[2])
            if name:
                found[job].setdefault(name)
    if p.returncode:
        raise subprocess.CalledProcessError(p.returncode, command)
    return {job: list(names) for job, names in found.items()}


def jobs(repo: str, attempt: dict) -> list[dict]:
    out = gh("api", "--paginate", "--jq", ".jobs[] | @json",
             f"repos/{repo}/actions/runs/{attempt['databaseId']}/attempts/{attempt['attempt']}/jobs")
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def failures(repo: str, attempt: dict) -> list[str]:
    """What one failed attempt's failed jobs name: test ids, else 'job / step'."""
    logs = failed_logs(repo, attempt)
    named = []
    for job in jobs(repo, attempt):
        if job.get("conclusion") not in BAD:
            continue
        if logs.get(job["name"]):
            named += logs[job["name"]]
        else:
            steps = [s["name"] for s in job.get("steps") or []
                     if s.get("conclusion") not in ("success", "skipped", None)]
            named.append(f"{job['name']} / {steps[0] if steps else job['conclusion']}")
    return named


def report(repo: str, runs: int, workflow: str | None) -> list[dict]:
    args = ["run", "list", "--repo", repo, "--limit", str(runs), "--json",
            "databaseId,attempt,conclusion,headSha,headBranch,workflowName,createdAt,url"]
    if workflow:
        args += ["--workflow", workflow]
    rows = gh_json(*args)

    def earlier(run_id: int, n: int) -> str:
        return gh("api", "--jq", ".conclusion",
                  f"repos/{repo}/actions/runs/{run_id}/attempts/{n}").strip()
    tests = defaultdict(list)
    for a in sorted(flaky(attempts(rows, earlier)), key=lambda a: a["createdAt"]):
        for name in failures(repo, a):
            tests[name].append({"run": a["url"], "attempt": a["attempt"], "branch": a["headBranch"],
                                "workflow": a["workflowName"], "at": a["createdAt"]})
    return [{"test": t, "failures": seen}
            for t, seen in sorted(tests.items(), key=lambda kv: (-len(kv[1]), kv[0]))]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default="raold/leeroy")
    ap.add_argument("--runs", type=int, default=300, help="how many recent runs to read")
    ap.add_argument("--workflow", help="one workflow only, e.g. ci")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    found = report(a.repo, a.runs, a.workflow)
    if a.json:
        json.dump(found, sys.stdout, indent=2)
        print()
        return 0
    if not found:
        print(f"no flaky failures in the last {a.runs} runs")
    for item in found:
        print(f"{len(item['failures'])}  {item['test']}")
        for f in item["failures"]:
            print(f"     {f['at'][:16]}  {f['workflow']}  {f['branch']}  {f['run']}/attempts/{f['attempt']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
