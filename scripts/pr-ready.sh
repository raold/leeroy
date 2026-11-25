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

# Not named gh: with the default GH the wrapper would call itself. Word splitting
# is intended so GH can carry arguments.
# shellcheck disable=SC2086
ghc() { command $gh_cmd "$@"; }

fetch_pr() {
    ghc pr view "$pr" --repo "$repo" \
        --json baseRefName,isDraft,mergeStateStatus,headRefOid,statusCheckRollup \
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

ghc api "repos/$repo/rulesets/$ruleset" >"$tmp/ruleset.json" || err "reading ruleset $ruleset failed"
ghc api --paginate --slurp "repos/$repo/pulls/$pr/commits" >"$tmp/commits.json" || err "reading commits failed"
ghc api --paginate --slurp "repos/$repo/pulls/$pr/comments" >"$tmp/rcomments.json" || err "reading review comments failed"
ghc api --paginate --slurp "repos/$repo/pulls/$pr/reviews" >"$tmp/reviews.json" || err "reading reviews failed"
ghc api --paginate --slurp "repos/$repo/issues/$pr/comments" >"$tmp/icomments.json" || err "reading PR comments failed"
ghc api graphql -F owner="${repo%%/*}" -F name="${repo##*/}" -F number="$pr" -f query='
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
    sha = last["sha"]
    # Commits a review is bound to, by SHA: a review of an older revision proves nothing.
    seen = []
    for r in flatten(load("reviews")):
        if login((r.get("user") or {}).get("login")) == bot and r.get("commit_id"):
            seen.append(r["commit_id"])
    for c in rcomments:
        if login((c.get("user") or {}).get("login")) == bot:
            seen += [c[k] for k in ("commit_id", "original_commit_id") if c.get(k)]
    for c in flatten(load("icomments")):
        body = c.get("body") or ""
        if login((c.get("user") or {}).get("login")) == bot:
            m = re.search(r"<!-- codex-review-state:v1 (.*?) -->", body)
            if m:
                try:
                    st = json.loads(m[1])["code_review"]
                except (ValueError, KeyError, TypeError):
                    st = {}
                if st.get("status") == "completed" and st.get("commit_sha"):
                    seen.append(st["commit_sha"])
            elif "<!-- codex-review-receipt:v1 -->" in body:
                m = re.search(r"Reviewed commit:\*\* `([0-9a-f]{7,40})`", body)
                if m:
                    seen.append(m[1])
        seen += re.findall(r"claude-local reviewed ([0-9a-f]{7,40})", body)
    # The last real commit, or a merge of main made after it, counts as reviewed.
    covered = [c["sha"] for c in commits[commits.index(last):]]
    if not any(s and len(s) >= 7 and c.startswith(s) for s in seen for c in covered):
        reasons.append(f"last non-merge commit {sha[:7]} has no review of that commit")

ready = not reasons
if as_json:
    print(json.dumps({"ready": ready, "reasons": reasons, "head": pr["headRefOid"]}))
else:
    for r in reasons:
        print(r)
sys.exit(0 if ready else 10)
PY
rc=$?
[ "$rc" -eq 0 ] || [ "$rc" -eq 10 ] || err "evaluation failed"
[ "$rc" -eq 0 ] || rc=1
exit "$rc"
