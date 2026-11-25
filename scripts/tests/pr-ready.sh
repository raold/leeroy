#!/usr/bin/env bash
# scripts/pr-ready.sh against a stub gh returning canned JSON; no network, nothing real.
set -uo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fix="$tmp/fix"
mkdir -p "$fix"

cat >"$tmp/gh" <<'STUB'
#!/usr/bin/env bash
case "$*" in
    "pr view"*) cat "$FIX/pr.json" ;;
    *rulesets/*) cat "$FIX/ruleset.json" ;;
    *pulls/*/commits*) cat "$FIX/commits.json" ;;
    *pulls/*/comments*) cat "$FIX/rcomments.json" ;;
    "api graphql"*) cat "$FIX/threads.json" ;;
    *) echo "stub gh: unexpected $*" >&2; exit 9 ;;
esac
STUB
chmod +x "$tmp/gh"

# Scenario knobs, reset by reset().
reset() {
    BASE=main DRAFT=false MERGE=CLEAN
    LEAVE_OUT="" FAIL_CHECK="" THREAD=true
    REVIEW_AT="2026-11-25T10:00:00Z" REVIEWER="chatgpt-codex-connector" LOCAL_BODY="" TOP_MERGE=false
}

write() {
    local rollup="" ctx
    for ctx in shellcheck secrets leeroy-tests mind voice dojo-tests dojo voice-rehearsal; do
        [ "$ctx" = "$LEAVE_OUT" ] && continue
        local concl=SUCCESS
        [ "$ctx" = "$FAIL_CHECK" ] && concl=FAILURE
        rollup+="{\"__typename\":\"CheckRun\",\"name\":\"$ctx\",\"status\":\"COMPLETED\",\"conclusion\":\"$concl\"},"
    done
    rollup+='{"__typename":"StatusContext","context":"unrelated","state":"FAILURE"}'
    local comments="[]"
    [ -n "$LOCAL_BODY" ] && comments="[{\"author\":{\"login\":\"raold\"},\"body\":\"$LOCAL_BODY\",\"createdAt\":\"2026-11-25T10:00:00Z\"}]"
    cat >"$fix/pr.json" <<JSON
{"baseRefName":"$BASE","isDraft":$DRAFT,"mergeStateStatus":"$MERGE","headRefOid":"cccccccccccccccccccccccccccccccccccccccc",
 "statusCheckRollup":[$rollup],"comments":$comments,
 "reviews":[{"author":{"login":"$REVIEWER"},"submittedAt":"$REVIEW_AT"}]}
JSON
    cat >"$fix/ruleset.json" <<'JSON'
{"id":26388753,"rules":[{"type":"deletion"},{"type":"required_status_checks","parameters":{"required_status_checks":[
{"context":"shellcheck"},{"context":"secrets"},{"context":"leeroy-tests"},{"context":"mind"},
{"context":"voice"},{"context":"dojo-tests"},{"context":"dojo"},{"context":"voice-rehearsal"}]}}]}
JSON
    local merge_entry=""
    if [ "$TOP_MERGE" = true ]; then
        merge_entry=',{"sha":"cccccccccccccccccccccccccccccccccccccccc","parents":[{"sha":"b"},{"sha":"m"}],"commit":{"committer":{"date":"2026-11-25T11:00:00Z"}}}'
    fi
    cat >"$fix/commits.json" <<JSON
[[{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","parents":[{"sha":"p"}],"commit":{"committer":{"date":"2026-11-25T08:00:00Z"}}},
{"sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","parents":[{"sha":"a"}],"commit":{"committer":{"date":"2026-11-25T09:00:00Z"}}}$merge_entry]]
JSON
    echo '[[]]' >"$fix/rcomments.json"
    cat >"$fix/threads.json" <<JSON
{"data":{"repository":{"pullRequest":{"reviewThreads":{"pageInfo":{"hasNextPage":false},"nodes":[{"isResolved":true},{"isResolved":$THREAD}]}}}}}
JSON
}

pass=0 fail=0
run() { # name expected-rc expected-substring [extra args]
    local name=$1 want=$2 sub=$3 out rc
    shift 3
    write
    out=$(FIX="$fix" GH="$tmp/gh" PR_READY_SLEEP=0 bash "$here/pr-ready.sh" 7 "$@" 2>&1)
    rc=$?
    if [ "$rc" = "$want" ] && { [ -z "$sub" ] || [[ $out == *"$sub"* ]]; }; then
        pass=$((pass + 1)); echo "ok   $name"
    else
        fail=$((fail + 1)); echo "FAIL $name: rc=$rc (want $want), output: $out"
    fi
}

reset; run "ready" 0 ""
reset; BASE=feature; run "base not main" 1 "not main"
reset; DRAFT=true; run "draft" 1 "draft"
reset; MERGE=BLOCKED; run "not clean" 1 "BLOCKED"
reset; MERGE=UNKNOWN; run "unknown state gives up after retries" 1 "UNKNOWN"
reset; LEAVE_OUT=mind; run "required check missing" 1 "mind is missing"
reset; FAIL_CHECK=dojo; run "required check failing" 1 "dojo is FAILURE"
reset; THREAD=false; run "unresolved thread" 1 "unresolved review thread"
reset; TOP_MERGE=true; run "merge of main after reviewed commit is ready" 0 ""
reset; REVIEW_AT="2026-11-25T08:30:00Z"; run "last real commit unreviewed" 1 "no review after it"
reset; REVIEWER=someone-else; run "review by another account not accepted" 1 "no review after it"
reset; REVIEWER="chatgpt-codex-connector[bot]"; run "bot suffix accepted" 0 ""
reset; REVIEW_AT="2026-11-25T08:30:00Z" LOCAL_BODY="claude-local reviewed bbbbbbb"; run "claude-local prefix accepted" 0 ""
reset; REVIEW_AT="2026-11-25T08:30:00Z" LOCAL_BODY="claude-local reviewed aaaaaaa"; run "claude-local for another commit rejected" 1 "no review after it"
reset; run "json ready shape" 0 '"ready": true' --json
reset; DRAFT=true; run "json not-ready shape" 1 '"reasons": ["PR is a draft"]' --json
reset; run "json head" 0 '"head": "cccccccccccccccccccccccccccccccccccccccc"' --json

# Tool error: a failing gh gives exit 2.
printf '#!/usr/bin/env bash\nexit 1\n' >"$tmp/gh-bad"; chmod +x "$tmp/gh-bad"
out=$(GH="$tmp/gh-bad" PR_READY_SLEEP=0 bash "$here/pr-ready.sh" 7 2>&1); rc=$?
if [ "$rc" = 2 ]; then pass=$((pass + 1)); echo "ok   tool error is exit 2"; else fail=$((fail + 1)); echo "FAIL tool error: rc=$rc $out"; fi

echo "pr-ready: $pass passed, $fail failed"
[ "$fail" = 0 ]
