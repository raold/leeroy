#!/usr/bin/env bash
# Pre-merge readiness gate for leeroy pull requests.
#   pr-ready.sh <pr-number> [--json]
# Exit 0 ready, 1 not ready (reasons printed), 2 tool error.
# GH=<cmd> overrides the gh binary (tests); PR_READY_SLEEP overrides the 10 s UNKNOWN retry wait.
set -euo pipefail

repo=raold/leeroy
ruleset=26388753
gh_cmd=${GH:-gh}
bot=chatgpt-codex-connector

err() { printf 'pr-ready: %s\n' "$*" >&2; exit 2; }

pr=""
json=0
for a in "$@"; do
    case "$a" in
        --json) json=1 ;;
        -*) err "unknown option $a" ;;
        *) [ -z "$pr" ] || err "one PR number only"; pr=$a ;;
    esac
done
case "$pr" in ''|*[!0-9]*) err "usage: pr-ready.sh <pr-number> [--json]" ;; esac

py=${PR_READY_PYTHON:-}
if [ -z "$py" ]; then
    if python3 -c '' >/dev/null 2>&1; then py=python3; else py=python; fi
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# gh needs word splitting here so GH can carry arguments.
# shellcheck disable=SC2086
gh() { $gh_cmd "$@"; }

fetch_pr() {
    gh pr view "$pr" --repo "$repo" \
        --json baseRefName,isDraft,mergeStateStatus,headRefOid,statusCheckRollup,comments,reviews \
        >"$tmp/pr.json" || err "gh pr view $pr failed"
}

fetch_pr
state=$("$py" -c 'import json,sys; print(json.load(open(sys.argv[1]))["mergeStateStatus"])' "$tmp/pr.json")
tries=1
while [ "$state" = UNKNOWN ] && [ "$tries" -lt 6 ]; do
    sleep "${PR_READY_SLEEP:-10}"
    fetch_pr
    state=$("$py" -c 'import json,sys; print(json.load(open(sys.argv[1]))["mergeStateStatus"])' "$tmp/pr.json")
    tries=$((tries + 1))
done

gh api "repos/$repo/rulesets/$ruleset" >"$tmp/ruleset.json" || err "reading ruleset $ruleset failed"
gh api --paginate --slurp "repos/$repo/pulls/$pr/commits" >"$tmp/commits.json" || err "reading commits failed"
gh api --paginate --slurp "repos/$repo/pulls/$pr/comments" >"$tmp/rcomments.json" || err "reading review comments failed"
gh api graphql -F owner="${repo%%/*}" -F name="${repo##*/}" -F number="$pr" -f query='
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100){ pageInfo{hasNextPage} nodes{ isResolved } }
    }
  }
}' >"$tmp/threads.json" || err "reading review threads failed"

set +e
"$py" - "$tmp" "$json" "$bot" <<'PY'
import json, re, sys

tmp, as_json, bot = sys.argv[1], sys.argv[2] == "1", sys.argv[3]


def load(name):
    with open(f"{tmp}/{name}.json", encoding="utf-8") as f:
        return json.load(f)


def flatten(pages):
    out = []
    for p in pages:
        out.extend(p if isinstance(p, list) else [p])
    return out


def login(name):
    return (name or "").removesuffix("[bot]")


pr, ruleset = load("pr"), load("ruleset")
commits, rcomments = flatten(load("commits")), flatten(load("rcomments"))
threads = load("threads")["data"]["repository"]["pullRequest"]["reviewThreads"]
reasons = []

if pr["baseRefName"] != "main":
    reasons.append(f"base is {pr['baseRefName']}, not main")
if pr["isDraft"]:
    reasons.append("PR is a draft")
if pr["mergeStateStatus"] != "CLEAN":
    reasons.append(f"mergeStateStatus is {pr['mergeStateStatus']}, not CLEAN")

required = []
for rule in ruleset.get("rules", []):
    if rule.get("type") == "required_status_checks":
        required += [c["context"] for c in rule["parameters"]["required_status_checks"]]
if not required:
    reasons.append("ruleset lists no required status checks")
for ctx in required:
    runs = [r for r in pr["statusCheckRollup"] or []
            if r.get("name") == ctx or r.get("context") == ctx]
    if not runs:
        reasons.append(f"required check {ctx} is missing")
        continue
    for r in runs:
        res = r.get("conclusion") or r.get("state") or r.get("status") or "PENDING"
        if res != "SUCCESS":
            reasons.append(f"required check {ctx} is {res}")
            break

if threads["pageInfo"]["hasNextPage"]:
    reasons.append("more than 100 review threads; cannot verify all resolved")
open_threads = sum(1 for t in threads["nodes"] if not t["isResolved"])
if open_threads:
    reasons.append(f"{open_threads} unresolved review thread(s)")

real = [c for c in commits if len(c.get("parents", [])) < 2]
if not real:
    reasons.append("PR has no non-merge commit")
else:
    last = real[-1]
    sha, cdate = last["sha"], last["commit"]["committer"]["date"]
    reviewed = any(login(r["author"]["login"]) == bot and r["submittedAt"] > cdate
                   for r in pr["reviews"] if r.get("author") and r.get("submittedAt"))
    reviewed = reviewed or any(login(c["user"]["login"]) == bot and c["created_at"] > cdate
                               for c in rcomments)
    if not reviewed:
        for c in pr["comments"]:
            for m in re.finditer(r"claude-local reviewed ([0-9a-f]{7,40})", c["body"]):
                if sha.startswith(m.group(1)):
                    reviewed = True
    if not reviewed:
        reasons.append(f"last non-merge commit {sha[:7]} has no review after it")

ready = not reasons
if as_json:
    print(json.dumps({"ready": ready, "reasons": reasons, "head": pr["headRefOid"]}))
else:
    for r in reasons:
        print(r)
sys.exit(0 if ready else 1)
PY
rc=$?
if [ "$rc" -gt 1 ]; then err "evaluation failed"; fi
exit "$rc"
