#!/usr/bin/env bash
# Release merged main commits onto the installed release branch. Never fetches.
# Usage: ac-release.sh [--native] [--allow-scripts] [--allow-raid ID]... [--dry-run] COMMIT...
# AC_ROOT/LEEROY_MIND_DATA select the installation; AC_RELEASE_PYTHON and
# AC_RELEASE_MYSQL select executables (also used by the isolated self-test).
# Bash reads scripts incrementally. A release may replace this very file: run an
# immutable private copy, including the helpers needed after a checkout rollback.
freeze_release_runner() {
    local here root runtime name
    here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || exit 1
    if [[ ${AC_RELEASE_FROZEN_DIR:-} == "$here" && -f $here/.release-runtime ]]; then return; fi
    root=$(cd "${AC_ROOT:-$here/..}" && pwd) || exit 1
    umask 077
    runtime=$(mktemp -d "${TMPDIR:-/tmp}/ac-release.XXXXXXXX") || exit 1
    printf 'leeroy-release-runtime\n' > "$runtime/.release-runtime"
    for name in ac-release.sh common.sh ac-release-data.py ac-release-git.py ac-release-patches.py; do
        if ! cp -- "$here/$name" "$runtime/$name"; then
            printf 'FAIL: cannot freeze release helpers\nRollback: no checkout changes\n' >&2
            exit 1
        fi
    done
    export AC_ROOT="$root" AC_RELEASE_FROZEN_DIR="$runtime"
    exec bash "$runtime/ac-release.sh" "$@"
}
freeze_release_runner "$@"
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
# Even a dry-run git status must not refresh the index on disk.
export GIT_OPTIONAL_LOCKS=0

native=0 dry=0 failed=0 changed=0 lock_owned=0 build_lock_owned=0 build_attempted=0 allow_scripts=0
snapshot_ready=0 binary_ready=0 completed=0 services_touched=0
voice_changes=0 modules_applied=0 planned_changes=0 pending_patches=0 recovery_ready=0
allow=() commits=() resolved=()
pre=unavailable branch=unavailable cnf='' release_lock='' snapshot='' binary_backup=''
python="${AC_RELEASE_PYTHON:-$AC_ROOT/mind/.venv/Scripts/python.exe}"
mysql="${AC_RELEASE_MYSQL:-$MYSQL_DIR/mysql.exe}"
data="${LEEROY_MIND_DATA:-$AC_ROOT/mind-data}"
voice_data="${LEEROY_VOICE_DATA:-$AC_ROOT/voice-data}"
runtime="$AC_RELEASE_FROZEN_DIR"
helper="$runtime/ac-release-data.py"
git_helper="$runtime/ac-release-git.py"
patch_helper="$runtime/ac-release-patches.py"
plan="$runtime/plan.json"
module_journal="$runtime/modules.json"
recovery=''
control_root="$AC_ROOT"
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
    local code_root="$AC_ROOT"
    if [[ $1 == stop ]]; then code_root="$control_root"; fi
    PYTHONPATH="$(native_path "$code_root/mind")" "$python" "$code_root/scripts/ac-mind.py" "$1" \
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
        bash "$control_root/scripts/ac-server.sh" stop || return 1
        native_stopped
    else mind stop; fi
}
start_services() { if ((native)); then bash "$AC_ROOT/scripts/ac-server.sh" start; else mind start; fi; }
data_op() { "$python" "$helper" "$@"; }

rollback_note() {
    printf 'Rollback (branch %s; allowed raid IDs:' "$branch"
    if ((${#allow[@]})); then printf ' %s' "${allow[@]}"; else printf ' none'; fi
    printf '; reset target %s):\n' "$pre"
    if [[ $pre == unavailable ]]; then printf '  no checkout change; reset target unavailable\n'; return; fi
    if ((!recovery_ready)); then printf '  no release changes; no rollback required\n'; return; fi
    printf '  cd %s && ( export AC_ROOT=%s LEEROY_MIND_DATA=%s && ' "$(quote "$AC_ROOT")" "$(quote "$AC_ROOT")" "$(quote "$data")"
    printf 'release_recovery_lock=$(git rev-parse --git-path ac-release.lock) && mkdir "$release_recovery_lock" && trap %s EXIT && ' \
        "$(quote 'rmdir -- "$release_recovery_lock"')"
    if ((native || allow_scripts)); then
        printf 'release_recovery_build_lock=%s && mkdir "$release_recovery_build_lock" && trap %s EXIT && ' \
            "$(quote "$build_lock")" "$(quote 'rmdir -- "$release_recovery_build_lock"; rmdir -- "$release_recovery_lock"')"
    fi
    if ((native)); then
        printf 'AC_ROOT=%s LEEROY_MIND_DATA=%s bash %s stop && (set -o pipefail; tasklist //FO CSV //NH | %s -c %s)' \
            "$(quote "$AC_ROOT")" "$(quote "$data")" "$(quote "$control_root/scripts/ac-server.sh")" \
            "$(quote "$python")" "$(quote "$native_check")"
    else
        printf '%s %s stop --root %s --data-dir %s' "$(quote "$python")" "$(quote "$(native_path "$control_root/scripts/ac-mind.py")")" "$(quote "$(native_path "$AC_ROOT")")" "$(quote "$(native_path "$data")")"
    fi
    if ((recovery_ready && native)); then
        printf ' && %s %s rollback %s' "$(quote "$python")" "$(quote "$(native_path "$patch_helper")")" \
            "$(quote "$(native_path "$module_journal")")"
    fi
    printf ' && %s %s rollback %s %s' "$(quote "$python")" "$(quote "$(native_path "$git_helper")")" \
        "$(quote "$(native_path "$AC_ROOT")")" "$(quote "$(native_path "$plan")")"
    # Inline stdlib code survives the reset even if an older checkout lacks this helper.
    if ((snapshot_ready)); then
        printf ' && %s -c %s %s %s' "$(quote "$python")" \
            "$(quote 'import pathlib,sqlite3,sys; s=sqlite3.connect(pathlib.Path(sys.argv[1]).resolve().as_uri()+"?mode=ro",uri=True); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()')" \
            "$(quote "$(native_path "$snapshot")")" "$(quote "$(native_path "$data/mind.db")")"
    fi
    if ((native)); then printf " && trap '' INT TERM && AC_JOBS=4 bash scripts/ac-build.sh && bash scripts/ac-server.sh start"; else
        printf ' && %s scripts/ac-mind.py start --root %s --data-dir %s' "$(quote "$python")" "$(quote "$(native_path "$AC_ROOT")")" "$(quote "$(native_path "$data")")"
    fi
    printf ' && (if command -v schtasks >/dev/null 2>&1 && schtasks //Query //TN leeroy-console >/dev/null 2>&1; then schtasks //End //TN leeroy-console >/dev/null 2>&1 || true; schtasks //Run //TN leeroy-console >/dev/null; fi)'
    printf ' )\n'
    if ((!snapshot_ready)); then printf 'note: no database snapshot was created; restore is omitted\n'; fi
}

rollback() {
    printf 'FAIL: release failed after changes; attempting rollback to %s\n' "$pre" >&2
    if ((!services_touched)); then
        if "$python" "$git_helper" rollback "$(native_path "$AC_ROOT")" "$(native_path "$plan")"; then ok 'checkout rolled back; services untouched'; else
            printf 'FAIL: checkout rollback failed\n' >&2
        fi
        return
    fi
    # Do not restore files under a service whose stop could not be confirmed.
    if ! stop_services; then printf 'FAIL: stop failed; follow the rollback line after stopping services\n' >&2; return; fi
    if ((modules_applied)) && ! "$python" "$patch_helper" rollback "$(native_path "$module_journal")"; then
        printf 'FAIL: module rollback refused; services remain stopped; inspect the recovery journal\n' >&2; return
    fi
    if ! "$python" "$git_helper" rollback "$(native_path "$AC_ROOT")" "$(native_path "$plan")"; then printf 'FAIL: checkout rollback refused; services remain stopped\n' >&2; return; fi
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
    "$python" "$runtime/ac-release-git.py" cleanup "$(native_path "$runtime")" || true
    exit "$rc"
}
trap 'finish $?' EXIT
trap 'exit 2' INT TERM

while (($#)); do
    case "$1" in
        --native) native=1; shift ;;
        --allow-scripts) allow_scripts=1; shift ;;
        --dry-run) dry=1; shift ;;
        --allow-raid)
            if (($# < 2)) || [[ ! $2 =~ ^[1-9][0-9]*$ ]]; then bad '--allow-raid requires a positive integer'; exit 1; fi
            allow+=("$2"); shift 2 ;;
        --*) bad "unknown option: $1"; exit 1 ;;
        *) commits+=("$1"); shift ;;
    esac
done
if ((${#commits[@]} == 0)); then bad 'usage: ac-release.sh [--native] [--allow-scripts] [--allow-raid ID]... [--dry-run] COMMIT...'; exit 1; fi
cd "$AC_ROOT"
# Serialize the state snapshot and every preflight, not merely the mutation phase.
# Otherwise a delayed preflight could reset a release completed while it was waiting.
release_lock=$(git rev-parse --git-path ac-release.lock) || exit 1
if ((!dry)); then
    if ! mkdir "$release_lock" 2>/dev/null; then bad 'another release holds the checkout lock'; exit 1; fi
    lock_owned=1
fi
pre=$(git rev-parse --verify HEAD) || exit 1
branch=$(git symbolic-ref --short -q HEAD) || branch=detached
case "$branch" in
    codex/live-release-*|release/*) ok "live branch $branch" ;;
    *) bad "expected a release/* or codex/live-release-* branch, found $branch" ;;
esac
if [[ -z $(git status --porcelain --untracked-files=normal) ]]; then ok 'clean working tree'; else bad 'working tree is dirty'; fi
if git rev-parse -q --verify CHERRY_PICK_HEAD >/dev/null || git rev-parse -q --verify MERGE_HEAD >/dev/null; then bad 'git operation already in progress'; fi
for operation in rebase-apply rebase-merge sequencer REVERT_HEAD; do
    if [[ -e $(git rev-parse --git-path "$operation") ]]; then bad 'git operation already in progress'; fi
done
if ((!lock_owned)) && [[ -e $release_lock ]]; then bad 'another release holds the checkout lock'; fi
if ((native || allow_scripts)); then
    if [[ -e $build_lock ]]; then bad "build lock exists: $build_lock"; else ok 'no native build lock'; fi
fi
main_tip=$(git rev-parse --verify 'origin/main^{commit}') || exit 1
for commit in "${commits[@]}"; do
    if [[ $commit == -* ]] || ! sha=$(git rev-parse --verify "$commit^{commit}" 2>/dev/null) || \
        ! git merge-base --is-ancestor "$sha" "$main_tip"; then bad "commit is not on origin/main: $commit"; continue; fi
    resolved+=("$sha")
    ok "commit on origin/main: $sha"
    # --no-renames checks both sides of a rename; NUL records preserve odd filenames.
    parents=$(git rev-list --parents -n 1 "$sha")
    if [[ $parents != *' '* ]]; then bad "root commit has no release parent: $sha"; continue; fi
    paths_ok=1
    while IFS= read -r -d '' path; do
        case "$path" in
            voice/*) voice_changes=1 ;;
            mind/*|selfie/*|.github/*|CLAUDE.md|README.md|references.bib|.gitattributes|.gitleaksignore) ;;
            scripts/*) if ((!allow_scripts)); then bad "disallowed path: $path (requires --allow-scripts)"; paths_ok=0; fi ;;
            modules/*|config/*|patches/*) if ((!native)); then bad "disallowed path: $path (requires --native)"; paths_ok=0; fi ;;
            *) bad "disallowed path: $path"; paths_ok=0 ;;
        esac
    done < <(git diff-tree -r --no-renames --no-commit-id --name-only -z "$sha^1" "$sha")
    ((paths_ok == 0)) || ok "allowed paths: $sha"
done
((failed == 0)) || exit 1
if ! "$python" "$git_helper" plan "$(native_path "$AC_ROOT")" "$main_tip" "$(native_path "$plan")" "$native" "${resolved[@]}"; then
    bad 'release plan refused; no checkout or service changes'; exit 1
fi
read -r planned_changes pending_patches voice_changes < <("$python" "$git_helper" stats "$(native_path "$plan")")
if ((planned_changes == 0 && pending_patches == 0)); then
    ok 'all commits already applied; no release changes'; exit 0
fi
if ((voice_changes)); then
    if data_op voice-stopped "$(native_path "$voice_data")"; then ok 'voice service stopped for voice changes';
    else bad 'voice changes require the voice service to be stopped first'; fi
fi
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
printf 'Plan: stop services; snapshot %s; cherry-pick %s commit(s); ' "$snapshot" "$planned_changes"
((native == 0)) || printf 'save old binaries; apply %s missing module patch(es); verify pin guard; build with AC_JOBS=4; ' "$pending_patches"
printf 'migrate; start services; restart leeroy-console; health summary\n'
if ((dry)); then ok 'dry-run complete; no release changes'; exit 0; fi

# Scripts can replace the running build wrapper even without --native.
# Hold the build lock throughout either kind of release and its recovery.
if ((native || allow_scripts)); then
    mkdir -p "$(dirname "$build_lock")"
    if ! mkdir "$build_lock"; then bad 'native build lock could not be acquired'; exit 1; fi
    build_lock_owned=1
fi
recovery="$data/snapshots/pre-release-$stamp-recovery"
mkdir -p "$recovery"
for name in ac-release.sh common.sh ac-release-data.py ac-release-git.py ac-release-patches.py plan.json; do
    cp -- "$runtime/$name" "$recovery/$name"
done
if ((native)); then cp -- "$module_journal" "$recovery/modules.json"; fi
"$python" "$git_helper" freeze-controls "$(native_path "$AC_ROOT")" "$(native_path "$recovery/controls")"
helper="$recovery/ac-release-data.py"
git_helper="$recovery/ac-release-git.py"
patch_helper="$recovery/ac-release-patches.py"
plan="$recovery/plan.json"
module_journal="$recovery/modules.json"
control_root="$recovery/controls"
recovery_ready=1
changed=1
services_touched=1
stop_services
data_op snapshot "$(native_path "$data/mind.db")" "$(native_path "$snapshot")"
snapshot_ready=1
"$python" "$git_helper" apply "$(native_path "$AC_ROOT")" "$(native_path "$plan")"
if ((native)); then
    data_op save-binaries "$(native_path "$AC_BIN")" "$(native_path "$binary_backup")"
    binary_ready=1
    modules_applied=1
    "$python" "$patch_helper" apply "$(native_path "$module_journal")"
    build_attempted=1
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
    if schtasks //Run //TN leeroy-console >/dev/null 2>&1; then ok 'console task restarted'
    else printf 'note: leeroy-console did not start; the release stands, start it by hand
'; fi
else printf 'note: leeroy-console scheduled task is absent\n'; fi
# A health failure reports exit 2 and recovery instructions, but must not discard a
# successfully running release because an unrelated optional service is unhealthy.
completed=1
if ! bash "$AC_ROOT/scripts/ac-health.sh"; then printf 'FAIL: post-release health; inspect summary and rollback line\n' >&2; exit 2; fi
ok 'release complete'
