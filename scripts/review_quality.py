#!/usr/bin/env python3
"""Evidence-linked review outcomes. Read-only GitHub collection; offline reporting."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

REPO = "raold/leeroy"
KINDS = {"revert", "post_release_fix", "release_build_fix"}
BOT = "chatgpt-codex-connector[bot]"


def date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise ValueError("GitHub read failed; check gh authentication and repository access")
    return json.loads(result.stdout)


def pages(endpoint):
    # REST pagination covers reviews and comments beyond the first 100, too.
    return [row for page in gh("api", "--paginate", "--slurp", endpoint + "?per_page=100") for row in page]


def collect(repo, since, until):
    date(since + "T00:00:00Z")
    date(until + "T00:00:00Z")
    if since > until or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("invalid repository or date range")
    pulls = gh("pr", "list", "--repo", repo, "--state", "merged", "--search",
               f"merged:{since}..{until}", "--limit", "1000", "--json", "number,title,url,mergedAt,author,headRefOid")
    if len(pulls) == 1000:
        raise ValueError("cohort may be truncated; collect a shorter date range")
    for pr in pulls:
        endpoint = f"repos/{repo}"
        observations = []
        for review in pages(f"{endpoint}/pulls/{pr['number']}/reviews"):
            when = review.get("submitted_at")
            login = (review.get("user") or {}).get("login")
            if not when or not login or review["state"] not in {"APPROVED", "CHANGES_REQUESTED", "COMMENTED"}:
                continue
            # An author's replies in a review thread are not an independent review.
            if login == (pr.get("author") or {}).get("login"):
                continue
            observations.append(dict(reviewer=login, at=when, url=review["html_url"],
                                     commit=review["commit_id"], kind="review"))
        for comment in pages(f"{endpoint}/issues/{pr['number']}/comments"):
            if (comment.get("user") or {}).get("login") != BOT:
                continue
            body = comment.get("body") or ""
            # No-findings reviews often have no formal review submission. Read the
            # signed service's machine receipt, not arbitrary prose saying 'review'.
            match = re.search(r"<!-- codex-review-state:v1 (.*?) -->", body)
            if match:
                try:
                    state = json.loads(match[1])["code_review"]
                except (ValueError, KeyError, TypeError):
                    continue
                if state.get("status") == "completed" and state.get("commit_sha"):
                    observations.append(dict(reviewer=BOT, at=comment["updated_at"], url=comment["html_url"],
                                             commit=state["commit_sha"], kind="completed_receipt"))
            elif "<!-- codex-review-receipt:v1 -->" in body:
                match = re.search(r"Reviewed commit:\*\* `([0-9a-f]{7,40})`", body)
                if match:
                    observations.append(dict(reviewer=BOT, at=comment["created_at"], url=comment["html_url"],
                                             commit=match[1], kind="completed_receipt"))
        pr["reviews"] = [r for r in observations if date(r["at"]) <= date(pr["mergedAt"])]
        pr.pop("author", None)
    return dict(format=1, repository=repo, since=since, until=until,
                collected_at=datetime.now(timezone.utc).isoformat(), pulls=pulls)


def validate_ledger(ledger):
    if ledger.get("format") != 1:
        raise ValueError("unsupported ledger format")
    for receipt in ledger.get("review_receipts", []):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", receipt.get("reviewer", "")):
            raise ValueError("manual review receipts need an explicit reviewer identity")
        if type(receipt.get("pr")) is not int or receipt["pr"] <= 0:
            raise ValueError("manual review receipts need a positive PR number")
        if not re.fullmatch(r"[0-9a-f]{40}", receipt.get("commit", "")):
            raise ValueError("manual review receipts need the reviewed commit's full SHA")
        if not receipt.get("evidence", "").startswith(f"https://github.com/{ledger['repository']}/pull/{receipt['pr']}"):
            raise ValueError("manual review receipts need a public attestation on that PR")
        date(receipt["at"])
    seen = set()
    for item in ledger["outcomes"]:
        if item["id"] in seen or item["kind"] not in KINDS:
            raise ValueError("duplicate outcome or unknown kind")
        seen.add(item["id"])
        if not item.get("evidence") or not all(str(u).startswith("https://github.com/") for u in item["evidence"]):
            raise ValueError("each outcome needs public evidence URLs")
        if item["kind"] == "post_release_fix" and not item.get("release_evidence"):
            raise ValueError("post-release fixes require release evidence, not just a merge date")
        if item.get("original_pr") is not None and (type(item["original_pr"]) is not int or item["original_pr"] <= 0):
            raise ValueError("original_pr must be positive or null (unattributed)")
        date(item["observed_at"])
        if item.get("rule") and not item.get("check"):
            raise ValueError("a proposed rule needs an executable check or an explicit pending check")


def report(snapshot, ledger, *, as_of=None, mature_days=14):
    validate_ledger(ledger)
    if snapshot.get("format") != 1 or snapshot["repository"] != ledger["repository"]:
        raise ValueError("snapshot and ledger repositories/formats must match")
    if mature_days < 0:
        raise ValueError("mature_days must be nonnegative")
    now = date(as_of or snapshot["collected_at"])
    pulls = {p["number"]: p for p in snapshot["pulls"] if date(p["mergedAt"]) <= now}
    rows = defaultdict(lambda: dict(reviewed_prs=set(), mature_prs=set(), revert=set(),
                                   post_release_fix=set(), release_build_fix=set(), affected_prs=set()))
    reviewers = {}
    for number, pr in pulls.items():
        names = {r["reviewer"] for r in pr["reviews"] if date(r["at"]) <= date(pr["mergedAt"])}
        # Local reviewers often share the PR author's login. Count a named local
        # reviewer only from an explicit, evidence-backed receipt, never by guessing
        # from branch prefixes, author identity or a claim that 'review passed'.
        names.update(r["reviewer"] for r in ledger.get("review_receipts", [])
                     if r["pr"] == number and date(r["at"]) <= date(pr["mergedAt"]))
        reviewers[number] = names or {"unknown / no recorded review"}
        for name in reviewers[number]:
            rows[name]["reviewed_prs"].add(number)
            if (now - date(pr["mergedAt"])).days >= mature_days:
                rows[name]["mature_prs"].add(number)
    unassigned = []
    for item in ledger["outcomes"]:
        if date(item["observed_at"]) > now:
            continue
        original = item.get("original_pr")
        if original not in reviewers:
            unassigned.append(item["id"])
            continue
        if date(item["observed_at"]) < date(pulls[original]["mergedAt"]):
            raise ValueError(f"{item['id']}: outcome predates the original merge")
        for name in reviewers[original]:
            row = rows[name]
            row[item["kind"]].add(item["id"])
            if item["kind"] in {"revert", "post_release_fix"}:
                row["affected_prs"].add(original)
    output = []
    for name, row in sorted(rows.items()):
        mature_affected = row["affected_prs"] & row["mature_prs"]
        output.append(dict(reviewer=name, **{key: sorted(value) for key, value in row.items()},
                           mature_affected_prs=sorted(mature_affected),
                           mature_affected_rate=(len(mature_affected) / len(row["mature_prs"]))
                           if row["mature_prs"] else None))
    return dict(repository=snapshot["repository"], as_of=now.isoformat(), mature_days=mature_days,
                cohort=dict(since=snapshot["since"], until=snapshot["until"], merged_prs=len(pulls)),
                reviewers=output, unassigned_outcomes=unassigned)


def markdown(result):
    lines = ["# Review outcomes", "", f"As of {result['as_of']}; merged cohort "
             f"{result['cohort']['since']} through {result['cohort']['until']}.", "",
             "Counts associate an outcome with every recorded pre-merge reviewer of the original PR. "
             "They are not causal blame or approval counts. Identities come from GitHub accounts or explicit local-review receipts; shared logins alone do not identify agents.", "",
             "| Reviewer identity | Reviewed PRs | Reverts | Post-release fixes | Release-build fixes | Mature affected / reviewed |",
             "|---|---:|---:|---:|---:|---:|"]
    for r in result["reviewers"]:
        mature = f"{len(r['mature_affected_prs'])} / {len(r['mature_prs'])}" if r["mature_prs"] else "pending"
        lines.append(f"| {r['reviewer']} | {len(r['reviewed_prs'])} | {len(r['revert'])} | "
                     f"{len(r['post_release_fix'])} | {len(r['release_build_fix'])} | {mature} |")
    lines += ["", f"Maturity threshold: {result['mature_days']} days. Recent PRs remain in raw counts; "
              "only mature PRs enter the rate. Zero means no linked outcomes recorded, not proven defect-free.",
              "Co-reviews share outcomes; columns must not be summed across reviewers.",
              "Unassigned outcomes (unknown original or outside cohort): " + (", ".join(result["unassigned_outcomes"]) or "none"), ""]
    return "\n".join(lines)


def rules(ledger):
    validate_ledger(ledger)
    lines = ["<!-- review-outcomes:start -->", "## Checks learned from released misses", ""]
    for item in ledger["outcomes"]:
        if not item.get("rule"):
            continue
        lines += [f"- {item['rule']} Check: {item['check']}. "
                  f"Evidence: [{item['id']}]({item['evidence'][0]})."]
    return "\n".join(lines + ["<!-- review-outcomes:end -->", ""])


def apply_rules(path, block):
    # Do not create AGENTS.md while another branch owns its introduction.
    if path.name != "AGENTS.md" or not path.is_file() or path.is_symlink():
        raise ValueError("AGENTS.md must already exist as a regular file; keep the candidate rules pending")
    text = path.read_text(encoding="utf-8")
    start, end = "<!-- review-outcomes:start -->", "<!-- review-outcomes:end -->"
    if text.count(start) != text.count(end) or text.count(start) > 1:
        raise ValueError("ambiguous managed rule block")
    if start in text:
        a, b = text.index(start), text.index(end)
        if b < a:
            raise ValueError("reversed managed rule markers")
        text = text[:a] + block.rstrip() + text[b + len(end):]
    else:
        text = text.rstrip() + "\n\n" + block
    path.write_text(text, encoding="utf-8")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--repo", default=REPO)
    c.add_argument("--since", required=True)
    c.add_argument("--until", required=True)
    c.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("report")
    r.add_argument("--snapshot", type=Path, required=True)
    r.add_argument("--ledger", type=Path, required=True)
    r.add_argument("--as-of")
    r.add_argument("--mature-days", type=int, default=14)
    r.add_argument("--json", action="store_true")
    a = sub.add_parser("rules")
    a.add_argument("--ledger", type=Path, required=True)
    a.add_argument("--apply-to", type=Path)
    args = p.parse_args(argv)
    try:
        if args.command == "collect":
            result = collect(args.repo, args.since, args.until)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        else:
            ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
            if args.command == "report":
                result = report(json.loads(args.snapshot.read_text(encoding="utf-8")), ledger,
                                as_of=args.as_of, mature_days=args.mature_days)
                print(json.dumps(result, indent=2) if args.json else markdown(result))
            elif args.apply_to:
                apply_rules(args.apply_to, rules(ledger))
            else:
                print(rules(ledger))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        p.exit(2, f"review-quality: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
