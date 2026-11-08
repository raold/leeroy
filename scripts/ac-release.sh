#!/usr/bin/env bash
# Release merged main commits onto the installed release branch. Never fetches.
# Usage: ac-release.sh [--native] [--allow-raid ID]... [--dry-run] COMMIT...
# AC_ROOT/LEEROY_MIND_DATA select the installation; AC_RELEASE_PYTHON and
# AC_RELEASE_MYSQL select executables (also used by the isolated self-test).
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
# Even a dry-run git status must not refresh the index on disk.
export GIT_OPTIONAL_LOCKS=0

native=0 dry=0 failed=0 changed=0 lock_owned=0 build_lock_owned=0 build_attempted=0
snapshot_ready=0 binary_ready=0 completed=0 services_touched=0
allow=() commits=() resolved=()
pre=unavailable branch=unavailable cnf='' release_lock='' snapshot='' binary_backup=''
python="${AC_RELEASE_PYTHON:-$AC_ROOT/mind/.venv/Scripts/python.exe}"
mysql="${AC_RELEASE_MYSQL:-$MYSQL_DIR/mysql.exe}"
data="${LEEROY_MIND_DATA:-$AC_ROOT/mind-data}"
helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ac-release-data.py"
build_lock="$AC_ROOT/.superpowers/live-build.lock"
# ac-server.sh's forced taskkill is best effort. Prove absence independently before
# touching binaries or restoring data; unreadable/malformed/empty task lists fail closed.
# Keep this stdlib check inline so the printed rollback survives an older checkout.
native_check='import csv,sys
try:
    rows=[r for r in csv.reader(sys.stdin,strict=True) if r]
except (csv.Error,UnicodeError):
    rows=[]
valid=bool(rows) and all(len(r)==5 and r[0].strip() and r[1].isdigit() for r in rows)
held=any(r and r[0].strip().casefold() in ("authserver.exe","worldserver.exe") for r in rows)
if not valid or held:
    print("FAIL: native stop not confirmed",file=sys.stderr)
    sys.exit(1)'

native_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s\n' "$1"; fi; }
ok() { printf 'ok: %s\n' "$*"; }
bad() { printf 'FAIL: %s\n' "$*" >&2; failed=1; }
quote() { printf '%q' "$1"; }
mind() {
    PYTHONPATH="$(native_path "$AC_ROOT/mind")" "$python" "$AC_ROOT/scripts/ac-mind.py" "$1" \
        --root "$(native_path "$AC_ROOT")" --data-dir "$(native_path "$data")"
}
native_stopped() {
    local listing
    if ! listing=$(tasklist //FO CSV //NH 2>/dev/null); then
        printf 'FAIL: native stop status unavailable\n' >&2; return 1
    fi
    printf '%s\n' "$listing" | "$python" -c "$native_check"
}
stop_services() {
    if ((native)); then
        bash "$AC_ROOT/scripts/ac-server.sh" stop || return 1
        native_stopped
    else mind stop; fi
}
start_services() { if ((native)); then bash "$AC_ROOT/scripts/ac-server.sh" start; else mind start; fi; }
data_op() { "$python" "$helper" "$@"; }

rollback_note() {
    printf 'Rollback (branch %s; allowed raid IDs:' "$branch"
    if ((${#allow[@]})); then printf ' %s' "${allow[@]}"; else printf ' none'; fi
    printf '):\n'
    if [[ $pre == unavailable ]]; then printf '  no checkout change; reset target unavailable\n'; return; fi
    printf '  cd %s && ' "$(quote "$AC_ROOT")"
    if ((native)); then
        printf 'bash scripts/ac-server.sh stop && (set -o pipefail; tasklist //FO CSV //NH | %s -c %s)' \
            "$(quote "$python")" "$(quote "$native_check")"
    else
        printf '%s scripts/ac-mind.py stop --root %s --data-dir %s' "$(quote "$python")" "$(quote "$(native_path "$AC_ROOT")")" "$(quote "$(native_path "$data")")"
    fi
    printf ' && git reset --hard %s' "$pre"
    # Inline stdlib code survives the reset even if an older checkout lacks this helper.
    if [[ -n $snapshot ]]; then
        printf ' && %s -c %s %s %s' "$(quote "$python")" \
            "$(quote 'import pathlib,sqlite3,sys; s=sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri()+"?mode=ro",uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()')" \
            "$(quote "$(native_path "$snapshot")")" "$(quote "$(native_path "$data/mind.db")")"
    fi
    if ((native)); then printf ' && AC_JOBS=4 bash scripts/ac-build.sh && bash scripts/ac-server.sh start'; else
        printf ' && %s scripts/ac-mind.py start --root %s --data-dir %s' "$(quote "$python")" "$(quote "$(native_path "$AC_ROOT")")" "$(quote "$(native_path "$data")")"
    fi
    printf '\n'
    if ((!snapshot_ready)); then printf 'note: planned snapshot was not created; omit its restore step\n'; fi
}

rollback() {
    printf 'FAIL: release failed after changes; attempting rollback to %s\n' "$pre" >&2
    if ((!services_touched)); then
        git cherry-pick --abort >/dev/null 2>&1 || true
        if git reset --hard "$pre" >/dev/null; then ok 'checkout rolled back; services untouched'; else
            printf 'FAIL: checkout rollback failed\n' >&2
        fi
        return
    fi
    # Do not restore files under a service whose stop could not be confirmed.
    if ! stop_services; then printf 'FAIL: stop failed; follow the rollback line after stopping services\n' >&2; return; fi
    git cherry-pick --abort >/dev/null 2>&1 || true
    if ! git reset --hard "$pre" >/dev/null; then printf 'FAIL: checkout rollback failed\n' >&2; return; fi
    if ((snapshot_ready)) && ! data_op restore "$(native_path "$snapshot")" "$(native_path "$data/mind.db")"; then
        printf 'FAIL: database rollback failed; services remain stopped\n' >&2; return
    fi
    if ((build_attempted)); then
        if ((!binary_ready)) || ! data_op restore-binaries "$(native_path "$binary_backup")" "$(native_path "$AC_BIN")"; then
            printf 'FAIL: binary rollback failed; services remain stopped\n' >&2; return
        fi
    fi
    if start_services; then ok 'checkout and database rolled back; old services restarted'; else
        printf 'FAIL: old services did not restart; follow the rollback line\n' >&2
    fi
}

finish() {
    local rc=$1
    trap - EXIT
    set +e
    if ((rc != 0 && changed && !completed)); then rollback; rc=2; fi
    [[ -z $cnf ]] || rm -f -- "$cnf"
    ((build_lock_owned == 0)) || rmdir -- "$build_lock"
    ((lock_owned == 0)) || rmdir -- "$release_lock"
    rollback_note
    exit "$rc"
}
trap 'finish $?' EXIT
trap 'exit 2' INT TERM

while (($#)); do
    case "$1" in
        --native) native=1; shift ;;
        --dry-run) dry=1; shift ;;
        --allow-raid)
            if (($# < 2)) || [[ ! $2 =~ ^[1-9][0-9]*$ ]]; then bad '--allow-raid requires a positive integer'; exit 1; fi
            allow+=("$2"); shift 2 ;;
        --*) bad "unknown option: $1"; exit 1 ;;
        *) commits+=("$1"); shift ;;
    esac
done
if ((${#commits[@]} == 0)); then bad 'usage: ac-release.sh [--native] [--allow-raid ID]... [--dry-run] COMMIT...'; exit 1; fi
cd "$AC_ROOT"
pre=$(git rev-parse --verify HEAD) || exit 1
branch=$(git symbolic-ref --short -q HEAD) || branch=detached
case "$branch" in
    codex/live-release-*|release/*) ok "live branch $branch" ;;
    *) bad "expected a release/* or codex/live-release-* branch, found $branch" ;;
esac
if [[ -z $(git status --porcelain --untracked-files=normal) ]]; then ok 'clean working tree'; else bad 'working tree is dirty'; fi
if git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null || git rev-parse -q --verify MERGE_HEAD >/dev/null; then bad 'git operation already in progress'; fi
release_lock=$(git rev-parse --git-path ac-release.lock)
if [[ -e $release_lock ]]; then bad 'another release holds the checkout lock'; fi
if ((native)); then
    if [[ -e $build_lock ]]; then bad "build lock exists: $build_lock"; else ok 'no native build lock'; fi
fi
for commit in "${commits[@]}"; do
    if [[ $commit == -* ]] || ! sha=$(git rev-parse --verify "$commit^{commit}" 2>/dev/null) || \
        ! git merge-base --is-ancestor "$sha" origin/main; then bad "commit is not on origin/main: $commit"; continue; fi
    resolved+=("$sha")
    ok "commit on origin/main: $sha"
    # --no-renames checks both sides of a rename; NUL records preserve odd filenames.
    parents=$(git rev-list --parents -n 1 "$sha")
    if [[ $parents != *' '* ]]; then bad "root commit has no release parent: $sha"; continue; fi
    paths_ok=1
    while IFS= read -r -d '' path; do
        case "$path" in
            mind/*|selfie/*|voice/*|CLAUDE.md|README.md|references.bib) ;;
            modules/*|config/*|patches/*) if ((!native)); then bad "disallowed path: $path (requires --native)"; paths_ok=0; fi ;;
            *) bad "disallowed path: $path"; paths_ok=0 ;;
        esac
    done < <(git diff-tree -r --no-renames --no-commit-id --name-only -z "$sha^1" "$sha")
    ((paths_ok == 0)) || ok "allowed paths: $sha"
done
for raid in "${allow[@]}"; do printf 'note: explicit raid exception %s\n' "$raid"; done

umask 077
cnf=$(mktemp "${TMPDIR:-/tmp}/ac-release.XXXXXX.cnf")
# Keep common.sh's credentials in a subprocess, never in the command line or output.
if (
    ac_db_creds CharacterDatabaseInfo
    printf '[client]\n'
    for key in host port user password; do
        case "$key" in host) value=$DB_HOST;; port) value=$DB_PORT;; user) value=$DB_USER;; password) value=$DB_PASS;; esac
        value=${value//\\/\\\\}; value=${value//\"/\\\"}
        printf '%s="%s"\n' "$key" "$value"
    done
) > "$cnf" 2>/dev/null && online=$("$mysql" --defaults-extra-file="$(native_path "$cnf")" -N -B acore_characters \
    -e 'SELECT online FROM characters WHERE guid = 1' 2>/dev/null) && [[ $online == 0 ]]; then ok 'owner offline'; else bad 'owner online or offline status unavailable'; fi
if data_op raids "$(native_path "$data/mind.db")" "${allow[@]}"; then ok 'no unexcepted open raids'; else bad 'open raids or raid state unavailable'; fi
((failed == 0)) || exit 1
stamp="$(date -u +%Y%m%d-%H%M%S)-$$"
snapshot="$data/snapshots/pre-release-$stamp.db"
binary_backup="$data/snapshots/pre-release-$stamp-binaries"
printf 'Plan: cherry-pick %s commit(s); stop services; snapshot %s; ' "${#resolved[@]}" "$snapshot"
((native == 0)) || printf 'save old binaries; build with AC_JOBS=4; '
printf 'migrate; start services; restart leeroy-console; health summary\n'
if ((dry)); then ok 'dry-run complete; no release changes'; exit 0; fi

# Atomic locks, acquired before checkout changes; never remove another run's lock.
if ! mkdir "$release_lock"; then bad 'checkout lock could not be acquired'; exit 1; fi
lock_owned=1
if ((native)); then
    mkdir -p "$(dirname "$build_lock")"
    if ! mkdir "$build_lock"; then bad 'native build lock could not be acquired'; exit 1; fi
    build_lock_owned=1
fi
changed=1
for sha in "${resolved[@]}"; do
    if git rev-parse -q --verify "$sha^2" >/dev/null; then git cherry-pick -m 1 "$sha"; else git cherry-pick "$sha"; fi
done
services_touched=1
stop_services
data_op snapshot "$(native_path "$data/mind.db")" "$(native_path "$snapshot")"
snapshot_ready=1
if ((native)); then
    data_op save-binaries "$(native_path "$AC_BIN")" "$(native_path "$binary_backup")"
    binary_ready=1 build_attempted=1
    # Do not start rollback while compilers are still writing the build tree.
    # Ignored signals are inherited by the build wrapper and its children.
    trap '' INT TERM
    AC_JOBS=4 bash "$AC_ROOT/scripts/ac-build.sh"
    trap 'exit 2' INT TERM
fi
(cd "$AC_ROOT/mind" && "$python" -m leeroy_mind db --data-dir "$(native_path "$data")" migrate)
start_services
if command -v schtasks >/dev/null 2>&1 && schtasks //Query //TN leeroy-console >/dev/null 2>&1; then
    schtasks //End //TN leeroy-console >/dev/null 2>&1 || true
    schtasks //Run //TN leeroy-console >/dev/null
    ok 'console task restarted'
else printf 'note: leeroy-console scheduled task is absent\n'; fi
# A health failure reports exit 2 and recovery instructions, but must not discard a
# successfully running release because an unrelated optional service is unhealthy.
completed=1
if ! bash "$AC_ROOT/scripts/ac-health.sh"; then printf 'FAIL: post-release health; inspect summary and rollback line\n' >&2; exit 2; fi
ok 'release complete'
