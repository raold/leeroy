#!/usr/bin/env bash
# Diff-triggered verification before a release: runs only the checks a branch's changes need.
#
# Usage: scripts/verify/run.sh [--plan] [--ci] [--only CHECK[,CHECK...]] [BASE]
#   BASE     the merge target (default origin/main); the changes are `git diff BASE...HEAD`
#   --plan   print the checks the changes select, run nothing
#   --only   run only these of the selected checks
#   --ci     portable mode (Linux CI): patches apply to upstream, migrations run on a fixture
#            database, msvc is skipped; nothing of the live install is read
#
# Checks, fastest first; each prints ok / FAIL / skip / warn lines, and the exit status is 1
# when any FAILs:
#   workflows    .github/workflows changed together with other files (warn; FAIL with
#                VERIFY_STRICT_WORKFLOWS=1): a workflow change belongs in its own PR
#   identifiers  modules/*/src C++: added lines naming a <windows.h> macro (near, far, ...)
#   numbering    new mind migrations against the merge target: no number taken, no hole
#   patches      patches/mod-playerbots or mod-dungeon-clear: the series entries the live module
#                checkouts lack, `git am` in temporary --shared clones (scripts/ac-release-patches.py
#                prepare, as the release does), then the pin-guard check on the result
#   contract     guild/bridge command code: the mind's `pytest -k contract`
#   msvc         modules/mod-leeroy/src C++: `cl /Zs` of the changed translation units with the
#                live build's include paths and defines (modules.vcxproj)
#   migrate      mind migrations: the branch's `db migrate` on an online-backup copy of the live
#                mind.db, then PRAGMA integrity_check; the copy is deleted
#
# Read-only toward the live install: it reads modules.vcxproj, clones module checkouts with
# --shared, and opens mind.db read-only. What it writes lives in a temporary directory.
#
# Environment (local mode; defaults follow this repository's main worktree, the install root):
#   VERIFY_LIVE_ROOT    install root
#   VERIFY_MIND_DB      the database to copy (default $LEEROY_MIND_DATA/mind.db, else
#                       $VERIFY_LIVE_ROOT/mind-data/mind.db)
#   VERIFY_PYTHON       python with the mind's dependencies (default: the live mind venv; --ci: python3)
#   VERIFY_JOBS         concurrent cl.exe (default 4)
#   VERIFY_UPSTREAM_PLAYERBOTS, VERIFY_UPSTREAM_DUNGEON_CLEAR   --ci clone sources

set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plan=0 ci=0 only="" base=""
while (($#)); do
    case "$1" in
        --plan) plan=1 ;;
        --ci) ci=1 ;;
        --only) only="${2:-}"; shift ;;
        --only=*) only="${1#--only=}" ;;
        -h|--help) sed -n '2,/^$/s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"; exit 0 ;;
        -*) printf 'unknown option %s\n' "$1" >&2; exit 2 ;;
        *) base="$1" ;;
    esac
    shift
done
base="${base:-origin/main}"

repo="${VERIFY_REPO:-$(git rev-parse --show-toplevel 2>/dev/null)}" || true
[[ -n $repo && -d $repo ]] || { printf 'not inside a git checkout\n' >&2; exit 2; }
cd "$repo" || exit 2
git rev-parse -q --verify "$base^{commit}" >/dev/null || { printf 'unknown base %s (fetch first?)\n' "$base" >&2; exit 2; }
mb="$(git merge-base "$base" HEAD)" || { printf 'no merge base between %s and HEAD\n' "$base" >&2; exit 2; }

native_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s\n' "$1"; fi; }

if ((ci)); then
    live=""
    py="${VERIFY_PYTHON:-python3}"
else
    common="$(git rev-parse --path-format=absolute --git-common-dir)"
    live="${VERIFY_LIVE_ROOT:-$(dirname "$common")}"
    py="${VERIFY_PYTHON:-}"
    if [[ -z $py ]]; then
        for py in "$live/mind/.venv/Scripts/python.exe" "$live/mind/.venv/bin/python" python3 python; do
            command -v "$py" >/dev/null 2>&1 && break
        done
    fi
fi

mapfile -d '' changed < <(git diff --name-only -z "$mb" HEAD)

oks=0 fails=0 skips=0 warns=0
report() {  # report STATUS CHECK MESSAGE
    local status="$1" check="$2"; shift 2
    printf '%-4s %s: %s\n' "$status" "$check" "$*"
    case "$status" in ok) ((oks++)) ;; FAIL) ((fails++)) ;; skip) ((skips++)) ;; warn) ((warns++)) ;; esac
    if ((ci)) && [[ -n ${GITHUB_ACTIONS:-} ]]; then
        case "$status" in FAIL) printf '::error title=verify %s::%s\n' "$check" "$*" ;;
                          warn) printf '::warning title=verify %s::%s\n' "$check" "$*" ;; esac
    fi
}
indent() { sed 's/^/       /'; }
# Python on Windows ends its lines with CRLF; its exit status survives the pipe (pipefail).
pyrun() { "$py" "$@" 2>&1 | tr -d '\r'; }

# --- selection -------------------------------------------------------------------------------
matching() {  # matching REGEX: the changed paths that match, one per line
    local path
    for path in "${changed[@]}"; do [[ $path =~ $1 ]] && printf '%s\n' "$path"; done
}
present() {  # the paths among stdin that exist in the checkout
    local path
    while IFS= read -r path; do [[ -f $path ]] && printf '%s\n' "$path"; done
}

cxx='\.(c|cc|cpp|cxx|h|hh|hpp|hxx|inl|ipp)$'
workflow_files="$(matching '^\.github/workflows/')"
other_files="$(for path in "${changed[@]}"; do [[ $path == .github/* ]] || printf '%s\n' "$path"; done)"
src_files="$(matching "^modules/[^/]+/src/.*$cxx")"
leeroy_src="$(matching "^modules/mod-leeroy/src/.*$cxx" | present)"
migration_files="$(matching '^mind/leeroy_mind/storage/(migrations/|db\.py$)')"
patch_files="$(matching '^patches/(mod-playerbots|mod-dungeon-clear)/')"
contract_files="$(matching '^mind/leeroy_mind/(bridge|protocol)\.py$|^mind/leeroy_mind/guild/|^modules/mod-leeroy/src/(LeeroyCommands\.|Guild/BotCommands\.|Lib/(LeeroyProtocol|LeeroyBridge|LeeroyGuildCommands|LeeroyBotCommandPolicy)\.)|contract')"

selected=()
[[ -n $workflow_files ]] && selected+=(workflows)
[[ -n $src_files ]] && selected+=(identifiers)
[[ -n $migration_files ]] && selected+=(numbering)
[[ -n $patch_files ]] && selected+=(patches)
[[ -n $contract_files ]] && selected+=(contract)
[[ -n $src_files ]] && selected+=(msvc)
[[ -n $migration_files ]] && selected+=(migrate)

if [[ -n $only ]]; then
    keep=()
    for check in "${selected[@]}"; do [[ ",$only," == *",$check,"* ]] && keep+=("$check"); done
    selected=("${keep[@]}")
fi

printf 'verify %s...HEAD: %d changed path(s); checks: %s\n' "$base" "${#changed[@]}" "${selected[*]:-none}"
if ((plan)) || ((${#selected[@]} == 0)); then exit 0; fi

scratch="$(mktemp -d "${TMPDIR:-/tmp}/leeroy-verify.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT

# --- checks ----------------------------------------------------------------------------------
check_workflows() {
    if [[ -z $other_files ]]; then
        report ok workflows "only workflow files changed"
        return
    fi
    local message
    message="$(wc -l <<<"$workflow_files" | tr -d ' ') workflow file(s) changed together with $(wc -l <<<"$other_files" | tr -d ' ') other path(s): a change to the checks belongs in its own PR"
    if [[ ${VERIFY_STRICT_WORKFLOWS:-0} == 1 ]]; then report FAIL workflows "$message"; else report warn workflows "$message"; fi
}

check_identifiers() {
    local out
    if out="$(pyrun "$(native_path "$here/winmacros.py")" --repo "$(native_path "$repo")" --base "$mb")"; then
        report ok identifiers "$(tail -n 1 <<<"$out")"
    else
        report FAIL identifiers "$(tail -n 1 <<<"$out")"
        head -n -1 <<<"$out" | indent
    fi
}

check_numbering() {
    local out
    if out="$(pyrun "$(native_path "$here/migrate_check.py")" numbering --repo "$(native_path "$repo")" --base "$base")"; then
        report ok numbering "$out"
    else
        report FAIL numbering "$out"
    fi
}

# The pin guard from CLAUDE.md: IsPinned before the death block, both before the group leave.
pin_guard() {
    awk '/^bool RandomPlayerbotMgr::ProcessBot\(Player\* bot\)/{p=1}
         p && !g && /IsPinned\(botId\)/{g=NR}  p && !d && /bot->isDead\(\)/{d=NR}
         p && /leave group if leader is rndbot/{l=NR; exit}
         END{print (g && d && l && g < d && d < l) ? "pin guard OK" : "pin guard MISPLACED"}' "$1"
}

clone_at() {  # clone_at SOURCE DEST [COMMIT]: a throwaway --shared clone with a plain identity
    git clone --quiet --shared --no-checkout "$1" "$2" &&
        git -C "$2" config user.name "verify" && git -C "$2" config user.email "verify@example.invalid" &&
        git -C "$2" config core.autocrlf false && git -C "$2" config commit.gpgSign false &&
        git -C "$2" checkout --quiet --detach "${3:-HEAD}"
}

check_patches_live() {
    local modules="$live/azerothcore-wotlk/modules" journal="$scratch/patches/journal.json" out names head guard
    if [[ ! -d $modules/mod-playerbots/.git && ! -f $modules/mod-playerbots/.git ]]; then
        report skip patches "no live module checkouts under $modules"
        return
    fi
    mkdir -p "$scratch/patches"
    if ! out="$(pyrun "$(native_path "$repo/scripts/ac-release-patches.py")" prepare "$(native_path "$modules")" \
            "$(native_path "$repo/patches")" "$(native_path "$journal")")"; then
        report FAIL patches "release rehearsal refused: ${out#FAIL: release patches: }"
        return
    fi
    # The pin guard on the patched result: re-apply the entries the live checkout lacks.
    names="$(pyrun -c 'import json, sys
for m in json.load(open(sys.argv[1], encoding="utf-8"))["modules"]:
    if m["name"] == "mod-playerbots":
        print(m["head"]); print("\n".join(p["name"] for p in m["patches"]))' "$(native_path "$journal")")"
    head="$(head -n 1 <<<"$names")"
    local file="$modules/mod-playerbots/src/Bot/RandomPlayerbotMgr.cpp"
    if [[ -n $head && $(wc -l <<<"$names") -gt 1 ]]; then
        local clone="$scratch/patches/mod-playerbots" name
        clone_at "$modules/mod-playerbots" "$clone" "$head" || { report FAIL patches "could not clone mod-playerbots"; return; }
        while IFS= read -r name; do
            [[ -z $name ]] && continue
            git -C "$clone" am --quiet --no-3way "$repo/patches/mod-playerbots/$name" >/dev/null 2>&1 ||
                { report FAIL patches "mod-playerbots: $name does not apply"; return; }
        done < <(tail -n +2 <<<"$names")
        file="$clone/src/Bot/RandomPlayerbotMgr.cpp"
    fi
    guard="$(pin_guard "$file")"
    local missing
    missing="$(grep -oE '"missing": [0-9]+' <<<"$out" | grep -oE '[0-9]+$')"
    if [[ $guard == "pin guard OK" ]]; then
        report ok patches "${missing:-0} series entr$( ((${missing:-0} == 1)) && echo y || echo ies) the live checkouts lack apply in a rehearsal clone; $guard"
    else
        report FAIL patches "$guard after the series"
    fi
}

check_patches_upstream() {
    local module url branch series clone n failed=0
    for module in mod-playerbots mod-dungeon-clear; do
        series="$repo/patches/$module"
        compgen -G "$series/*.patch" >/dev/null || continue
        case "$module" in
            mod-playerbots) url="${VERIFY_UPSTREAM_PLAYERBOTS:-https://github.com/mod-playerbots/mod-playerbots.git}" ;;
            mod-dungeon-clear) url="${VERIFY_UPSTREAM_DUNGEON_CLEAR:-https://github.com/jrad7/mod-dungeon-clear.git}" ;;
        esac
        branch=master
        clone="$scratch/upstream/$module"
        if ! git clone --quiet --filter=blob:none --branch "$branch" "$url" "$clone" 2>/dev/null; then
            report skip patches "$module: upstream clone failed"
            continue
        fi
        n="$(find "$series" -maxdepth 1 -name '*.patch' | wc -l | tr -d ' ')"
        if ! git -C "$clone" -c user.name=verify -c user.email=verify@example.invalid am --quiet --3way "$series"/*.patch >"$scratch/am.log" 2>&1; then
            report FAIL patches "$module: the series ($n) no longer applies to upstream $branch"
            grep -E '^(error|Patch failed|CONFLICT)' "$scratch/am.log" | head -n 5 | indent
            failed=1
            continue
        fi
        if [[ $module == mod-playerbots ]]; then
            local guard
            guard="$(pin_guard "$clone/src/Bot/RandomPlayerbotMgr.cpp")"
            if [[ $guard != "pin guard OK" ]]; then report FAIL patches "$module: $guard"; failed=1; continue; fi
            report ok patches "$module: $n patch(es) apply to upstream $branch; $guard"
        else
            report ok patches "$module: $n patch(es) apply to upstream $branch"
        fi
    done
    return "$failed"
}

check_contract() {
    if ! "$py" -c 'import pytest' >/dev/null 2>&1; then
        report skip contract "no pytest in $py"
        return
    fi
    local out rc
    out="$(cd "$repo/mind" && PYTHONDONTWRITEBYTECODE=1 pyrun -m pytest -q -k contract -p no:cacheprovider tests)"
    rc=$?
    case "$rc" in
        0) report ok contract "$(tail -n 1 <<<"$out")" ;;
        5) report skip contract "no test matches -k contract" ;;
        *) report FAIL contract "$(tail -n 1 <<<"$out")"
           grep -E '^(FAILED|ERROR) ' <<<"$out" | head -n 20 | indent ;;
    esac
}

check_msvc() {
    local dojo
    dojo="$(matching "^modules/mod-dojo/src/.*$cxx")"
    [[ -n $dojo ]] && report skip msvc "mod-dojo is built only in the dojo's own core, not the live build"
    if [[ -z $leeroy_src ]]; then
        [[ -z $dojo ]] && report skip msvc "no mod-leeroy source left to compile"
        return
    fi
    if ((ci)); then report skip msvc "needs Windows and the live build tree"; return; fi
    local vcxproj="$live/build/modules/modules.vcxproj"
    if [[ ! -f $vcxproj ]]; then report skip msvc "no live build tree at $live/build"; return; fi
    [[ -n $(git status --porcelain --untracked-files=no -- modules/mod-leeroy) ]] &&
        report warn msvc "modules/mod-leeroy has uncommitted changes: the working copies are compiled"
    local -a files
    mapfile -t files <<<"$leeroy_src"
    local out rc
    out="$(pyrun "$(native_path "$here/msvc_syntax.py")" --vcxproj "$(native_path "$vcxproj")" \
        --live-module "$(native_path "$live/azerothcore-wotlk/modules/mod-leeroy")" \
        --module "$(native_path "$repo/modules/mod-leeroy")" --jobs "${VERIFY_JOBS:-4}" "${files[@]}" 2>&1)"
    rc=$?
    local tus
    tus="$(grep -cE '^(ok  |FAIL) msvc ' <<<"$out")"
    if ((rc == 0)); then
        report ok msvc "$tus translation unit(s) pass cl /Zs"
        grep -E '^note ' <<<"$out" | indent
    else
        report FAIL msvc "$(grep -c '^FAIL' <<<"$out") of $tus translation unit(s) fail cl /Zs"
        grep -vE '^ok  ' <<<"$out" | indent
    fi
}

check_migrate() {
    local out
    if ((ci)); then
        mkdir -p "$scratch/base"
        git archive "$base" mind | tar -x -C "$scratch/base" || { report FAIL migrate "could not extract $base's mind"; return; }
        if out="$(pyrun "$(native_path "$here/migrate_check.py")" fixture --base-mind "$(native_path "$scratch/base/mind")" \
                --mind "$(native_path "$repo/mind")" --scratch "$(native_path "$scratch")" 2>&1)"; then
            report ok migrate "$out"
        else
            report FAIL migrate "$out"
        fi
        return
    fi
    local db="${VERIFY_MIND_DB:-${LEEROY_MIND_DATA:-$live/mind-data}/mind.db}"
    if [[ ! -f $db ]]; then report skip migrate "no mind.db at $db"; return; fi
    if out="$(pyrun "$(native_path "$here/migrate_check.py")" live --source "$(native_path "$db")" \
            --mind "$(native_path "$repo/mind")" --scratch "$(native_path "$scratch")" 2>&1)"; then
        report ok migrate "$out"
    else
        report FAIL migrate "$out"
    fi
}

for check in "${selected[@]}"; do
    case "$check" in
        patches) if ((ci)); then check_patches_upstream; else check_patches_live; fi ;;
        *) "check_$check" ;;
    esac
done

printf 'verify: %d ok, %d FAIL, %d warn, %d skip\n' "$oks" "$fails" "$warns" "$skips"
((fails == 0))
