# Reviewer outcomes

Measure recorded pre-merge reviews against evidence-linked reverts and live-found
fixes. This is a partial ledger, not an assertion that unlisted PRs had no defects.
`outcomes.json` records the original PR separately from its fixing PR; reviewers
of the fix are never blamed for the original. A release-build failure has its own
column because a merge is not proof of a successful deployment.

```powershell
python scripts/review_quality.py collect --since 2026-11-01 --until 2026-11-24 --out <private-output>/reviews.json
python scripts/review_quality.py report --snapshot <private-output>/reviews.json --ledger scripts/review-quality/outcomes.json
python scripts/review_quality.py rules --ledger scripts/review-quality/outcomes.json
```

Collection uses authenticated `gh` read operations, paginates reviews/comments,
and fails on a potentially truncated PR cohort. It saves timestamps, reviewed
commits and public receipt URLs, not comment bodies. Formal reviews and Codex's
completed no-findings receipts count; ordinary comments, pending reviews,
dismissed reviews, author replies and post-merge reviews do not. Multiple passes
by the same account count once per PR. A review may have found issues; these are
review-participation counts, not approvals. Shared accounts cannot establish an
agent identity. Missing review records stay in an explicit unknown bucket.

For a local peer review conducted through a shared account, add an explicit
`review_receipts` row to the ledger: `pr`, `reviewer` (for example `claude-local`),
`at` (UTC), `commit` (full reviewed SHA), and `evidence` (the public PR attestation
URL). The identity must be stated in that attestation; do not infer it from the
branch name or PR author. Receipts after merging do not count. Use the same stable
identity across local reviews, and do not add a receipt for a review already
attributed by the collector. None of the initial incidents establishes a named
local reviewer, so the initial receipt list is intentionally empty.

Add a ledger row with a stable ID, kind, original PR (or null), fixing PR,
observed UTC timestamp and public evidence. Post-release fixes additionally need
release evidence; do not infer deployment from merging. Deduplicate one incident
under one ID, even if it took several fix commits. Reverts include post-merge
reverts; classify non-defect rollbacks separately outside this defect ledger.
An original outside the chosen cohort is reported unassigned, never silently
dropped. Lookback rates use only PRs at least 14 days old; recent PRs still appear
in raw counts. Co-reviewers share the same incident, so totals across reviewers
are not additive. This describes observed outcomes, not causal fault or a ranking.

Each evidenced miss can propose a short rule and the check that would catch it.
Until the shared `AGENTS.md` lands, `rules` only prints the candidate block.
Once it exists, run the following in a branch and review the diff:

```powershell
python scripts/review_quality.py rules --ledger scripts/review-quality/outcomes.json --apply-to AGENTS.md
```

It refuses to create a missing file and only replaces its own marked block.
No automatic merge, release, reviewer message, or instruction edit occurs.
Run `python -m unittest discover -s scripts/tests -p test_review_quality.py`.
