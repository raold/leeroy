import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("review_quality", Path(__file__).parents[1] / "review_quality.py")
q = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(q)


def snapshot():
    def pr(n, when, names):
        return dict(number=n, mergedAt=when, reviews=[dict(reviewer=s, at=when) for s in names])
    return dict(format=1, repository=q.REPO, since="2026-11-01", until="2026-11-24",
                collected_at="2026-11-24T00:00:00Z", pulls=[
                    pr(1, "2026-11-01T00:00:00Z", ["a", "a", "b"]),
                    pr(2, "2026-11-22T00:00:00Z", ["a"]),
                    pr(3, "2026-11-02T00:00:00Z", [])])


def ledger():
    def item(i, original, kind="post_release_fix"):
        return dict(id=i, original_pr=original, fix_pr=99, kind=kind, observed_at="2026-11-23T00:00:00Z",
                    evidence=["https://github.com/raold/leeroy/pull/99"], release_evidence="https://github.com/raold/leeroy/pull/99")
    return dict(format=1, repository=q.REPO, outcomes=[item("one", 1), item("two", 1, "revert"),
                                                       item("recent", 2), item("unknown", None), item("outside", 8)])


class Outcomes(unittest.TestCase):
    def test_shared_reviews_deduplicate_and_use_original(self):
        result = q.report(snapshot(), ledger())
        a, b, unknown = result["reviewers"]
        self.assertEqual(a["reviewed_prs"], [1, 2])
        self.assertEqual(a["post_release_fix"], ["one", "recent"])
        self.assertEqual(b["post_release_fix"], ["one"])
        self.assertEqual(a["mature_affected_rate"], 1.0)
        self.assertEqual(a["mature_affected_prs"], [1])
        self.assertEqual(unknown["reviewed_prs"], [3])
        self.assertEqual(result["unassigned_outcomes"], ["unknown", "outside"])

    def test_postmerge_review_does_not_count(self):
        s = snapshot()
        s["pulls"][0]["reviews"].append(dict(reviewer="late", at="2026-11-02T00:00:00Z"))
        self.assertNotIn("late", [r["reviewer"] for r in q.report(s, ledger())["reviewers"]])

    def test_build_fix_not_in_escape_rate(self):
        l = ledger()
        l["outcomes"] = [dict(l["outcomes"][0], kind="release_build_fix")]
        a = q.report(snapshot(), l)["reviewers"][0]
        self.assertEqual(a["release_build_fix"], ["one"])
        self.assertEqual(a["mature_affected_rate"], 0)

    def test_explicit_local_reviewer_receipt_and_postmerge_exclusion(self):
        l = ledger()
        receipt = dict(pr=1, reviewer="claude-local", at="2026-11-01T00:00:00Z", commit="a"*40,
                       evidence="https://github.com/raold/leeroy/pull/1#issuecomment-123")
        l["review_receipts"] = [receipt, dict(receipt, reviewer="late-local", at="2026-11-02T00:00:00Z")]
        rows = {r["reviewer"]: r for r in q.report(snapshot(), l)["reviewers"]}
        self.assertEqual(rows["claude-local"]["post_release_fix"], ["one"])
        self.assertNotIn("late-local", rows)
        l["review_receipts"][0]["evidence"] = "https://github.com/raold/leeroy/pull/10"
        with self.assertRaises(ValueError):
            q.report(snapshot(), l)
        l["review_receipts"][0]["evidence"] = receipt["evidence"] = "https://github.com/raold/leeroy/pull/1"
        l["review_receipts"][0]["commit"] = "abc"
        with self.assertRaises(ValueError):
            q.report(snapshot(), l)

    def test_future_outcomes_excluded(self):
        self.assertEqual(q.report(snapshot(), ledger(), as_of="2026-11-20T00:00:00Z")["reviewers"][0]["affected_prs"], [])

    def test_recent_only_has_no_rate(self):
        s = snapshot()
        s["pulls"] = [s["pulls"][1]]
        self.assertIsNone(q.report(s, ledger())["reviewers"][0]["mature_affected_rate"])

    def test_duplicate_ids_and_missing_release_evidence_refused(self):
        for mutate in (lambda l: l["outcomes"].append(copy.deepcopy(l["outcomes"][0])),
                       lambda l: l["outcomes"][0].pop("release_evidence")):
            l = ledger()
            mutate(l)
            with self.assertRaises(ValueError):
                q.report(snapshot(), l)

    def test_repository_mismatch_and_impossible_timeline_refused(self):
        l = ledger()
        l["repository"] = "wrong/repo"
        with self.assertRaises(ValueError):
            q.report(snapshot(), l)
        l = ledger()
        l["outcomes"][0]["observed_at"] = "2026-10-01T00:00:00Z"
        with self.assertRaises(ValueError):
            q.report(snapshot(), l)

    def test_rules_wait_for_agents_and_preserve_other_instructions(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "AGENTS.md"
            with self.assertRaises(ValueError):
                q.apply_rules(path, "block")
            path.write_text("# Instructions\nDo this.\n", encoding="utf-8")
            block = q.rules(ledger())
            q.apply_rules(path, block)
            first = path.read_text(encoding="utf-8")
            q.apply_rules(path, block)
            self.assertEqual(first, path.read_text(encoding="utf-8"))
            self.assertTrue(first.startswith("# Instructions\nDo this.\n"))

    def test_checked_in_ledger_validates(self):
        q.validate_ledger(json.loads((Path(__file__).parents[1] / "review-quality/outcomes.json").read_text()))

    def test_collect_receipts_authors_and_pagination(self):
        calls = []
        def fake(*args):
            calls.append(args)
            if args[0] == "pr":
                return [dict(number=7, mergedAt="2026-11-02T00:00:00Z", author={"login": "writer"})]
            if "/reviews?" in args[-1]:
                base = dict(submitted_at="2026-11-01T00:00:00Z", state="COMMENTED", html_url="url", commit_id="a"*40)
                return [[dict(base, user={"login": "writer"}), dict(base, user={"login": "human"})],
                        [dict(base, user={"login": "late"}, submitted_at="2026-11-03T00:00:00Z")]]
            return [[dict(user={"login": q.BOT}, updated_at="2026-11-01T00:00:00Z", html_url="receipt",
                          body='<!-- codex-review-state:v1 {"code_review":{"status":"completed","commit_sha":"abc"}} -->')]]
        with patch.object(q, "gh", side_effect=fake):
            result = q.collect(q.REPO, "2026-11-01", "2026-11-03")
        self.assertEqual({r["reviewer"] for r in result["pulls"][0]["reviews"]}, {"human", q.BOT})
        self.assertTrue(all("--paginate" in c for c in calls[1:]))


if __name__ == "__main__":
    unittest.main()
