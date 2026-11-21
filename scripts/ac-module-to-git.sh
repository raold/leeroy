#!/usr/bin/env bash
# Swap a module installed from a zip extraction for a git clone of its upstream at the
# commit the zip came from, so the folder has history and an update path.
#
# Usage: ac-module-to-git.sh [--go] [--subdir PATH] MODULE URL COMMIT
#   MODULE   the folder under azerothcore-wotlk/modules; its name is kept exactly
#   URL      the upstream repository
#   COMMIT   the upstream commit whose tree matches the folder (the dry run shows the match)
#   --subdir PATH  the module lives in PATH inside the upstream repository (the core reads a
#            module's conf/ and data/sql/ from the folder itself, so the clone cannot hold it
#            nested): the branch is `git subtree split` of PATH. Local metadata lets
#            ac-update.sh update the split without importing the rest of upstream
#   --go     act; without it the upstream is cloned into a temporary directory and compared
#
# Afterwards the folder holds exactly what the zip folder held, line endings aside: what the
# zip changed, added or lacks is one local commit on top of COMMIT, which ac-update.sh
# rebases like any local patch. The old folder is kept under $AC_ROOT/module-to-git/.
# Refuses while the native build lock is held, a server or a build is running (or the
# process list cannot be read), or the folder is a link, a git checkout already, or named
# like an unstripped GitHub zip (-master, -main).
#
# AC_TASKLIST names a command that prints the process list as tasklist's CSV (tests).
# Exit: 0 done or compared; 2 the swap itself failed (the message says where each folder
# is); any other failure changed nothing.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

usage() { sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

GO=0 SUBDIR='' args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --go) GO=1 ;;
        --subdir) [ $# -ge 2 ] || die "--subdir needs a path"; SUBDIR="$2"; shift ;;
        --subdir=*) SUBDIR="${1#--subdir=}" ;;
        -h|--help) usage; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) args+=("$1") ;;
    esac
    shift
done
[ "${#args[@]}" -eq 3 ] || { usage >&2; exit 1; }
MODULE="${args[0]}" URL="${args[1]}" COMMIT="${args[2]}"

[[ "$MODULE" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "not a module folder name: $MODULE"
SUBDIR="${SUBDIR#/}"; SUBDIR="${SUBDIR%/}"
if [ -n "$SUBDIR" ]; then
    case "/$SUBDIR/" in */../*|*/./*|*//*) die "not a plain relative path: $SUBDIR" ;; esac
fi

DIR="$AC_MODULES/$MODULE"
[ -d "$DIR" ] || die "no module folder: $DIR"
[ ! -L "$DIR" ] && [ "$(cd "$DIR" && pwd -P)" = "$(cd "$AC_MODULES" && pwd -P)/$MODULE" ] \
    || die "$MODULE is a link or junction, not a zip folder; nothing to do"

# --- preflight -------------------------------------------------------------
refusals=()
case "$MODULE" in *-master|*-main) refusals+=("folder name ends in a GitHub zip suffix: strip it first") ;; esac
[ ! -e "$DIR/.git" ] || refusals+=("already a git checkout")
build_lock="$AC_ROOT/.superpowers/live-build.lock"
[ ! -e "$build_lock" ] || refusals+=("the native build lock is held: $build_lock")
process_list() {
    if [ -n "${AC_TASKLIST:-}" ]; then "$AC_TASKLIST"; else tasklist //FO CSV //NH; fi
}
if ! listing="$(process_list 2>/dev/null)" || ! grep -q '^"' <<< "$listing"; then
    refusals+=("cannot read the process list, so the servers and builds are not proved stopped")
else
    busy="$(tr -d '\r' <<< "$listing" | awk -F'"' 'tolower($2) ~ /^(worldserver|authserver|cmake|msbuild|cl)\.exe$/ {print $2}' \
        | sort -u | tr '\n' ' ')"
    [ -z "$busy" ] || refusals+=("still running: ${busy% }")
fi
for r in "${refusals[@]}"; do warn "$MODULE: $r"; done
[ "$GO" -eq 0 ] || [ "${#refusals[@]}" -eq 0 ] || die "refused; nothing changed"

# --- clone -----------------------------------------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
if [ "$GO" -eq 1 ]; then
    # Same volume as the modules folder, so the swap is two renames.
    HOLD="$AC_ROOT/module-to-git"
    mkdir -p "$HOLD"
    STAGE="$HOLD/.$MODULE-$STAMP.clone"
else
    STAGE="$(mktemp -d "${TMPDIR:-/tmp}/ac-module-to-git.XXXXXX")/$MODULE"
fi
swapped=0
cleanup() { [ "$swapped" -eq 1 ] || rm -rf -- "$STAGE"; [ "$GO" -eq 1 ] || rm -rf -- "$(dirname "$STAGE")"; }
trap cleanup EXIT

log "cloning $URL"
GIT_LFS_SKIP_SMUDGE=1 git clone --quiet --no-checkout -- "$URL" "$STAGE" || die "clone failed"
sha="$(git -C "$STAGE" rev-parse --verify --quiet "$COMMIT^{commit}")" || die "no commit $COMMIT upstream"
remote_head="$(git -C "$STAGE" symbolic-ref --quiet --short refs/remotes/origin/HEAD)" \
    || die "upstream has no default branch"
branch="${remote_head#origin/}"
git -C "$STAGE" merge-base --is-ancestor "$sha" "$remote_head" \
    || die "$sha is not on $remote_head, so the clone could never fast-forward"
if [ -n "$SUBDIR" ]; then
    [ "$(git -C "$STAGE" cat-file -t "$sha:$SUBDIR" 2>/dev/null)" = tree ] || die "no folder $SUBDIR at $sha"
    # Older Git (2.43, Ubuntu 24.04) splits only a prefix that is on disk, even given a commit.
    GIT_LFS_SKIP_SMUDGE=1 git -C "$STAGE" checkout --quiet --detach "$sha"
    tip="$(cd "$STAGE" && git subtree split -q --prefix="$SUBDIR" "$sha")" || die "subtree split failed"
    GIT_LFS_SKIP_SMUDGE=1 git -C "$STAGE" checkout --quiet -B "$branch" "$tip"
    git -C "$STAGE" branch --quiet --unset-upstream "$branch" 2>/dev/null || true
    git -C "$STAGE" config --local leeroy.subtreePrefix "$SUBDIR"
    git -C "$STAGE" config --local leeroy.subtreeUpstream "$remote_head"
    git -C "$STAGE" config --local leeroy.subtreeRevision "$sha $tip"
else
    tip="$sha"
    GIT_LFS_SKIP_SMUDGE=1 git -C "$STAGE" checkout --quiet -B "$branch" "$sha"
    git -C "$STAGE" branch --quiet --set-upstream-to="$remote_head" "$branch"
fi

# --- compare ---------------------------------------------------------------
# One line per file: <state> <path>, state same | eol | changed | added | missing.
compare() {
    local zip="$1" clone="$2" f
    { (cd "$zip" && find . -path ./.git -prune -o -type f -print); \
      (cd "$clone" && find . -path ./.git -prune -o -type f -print); } | sed 's#^\./##' | sort -u |
    while IFS= read -r f; do
        if [ ! -e "$clone/$f" ]; then echo "added $f"
        elif [ ! -e "$zip/$f" ]; then echo "missing $f"
        elif cmp -s -- "$zip/$f" "$clone/$f"; then echo "same $f"
        elif diff -q --strip-trailing-cr -- "$zip/$f" "$clone/$f" >/dev/null; then echo "eol $f"
        else echo "changed $f"
        fi
    done
}
report() {
    local states="$1" s n
    for s in same eol changed added missing; do
        n="$(grep -c "^$s " <<< "$states" || true)"
        printf '    %-8s %s\n' "$s" "$n"
    done
    grep -E '^(changed|added|missing) ' <<< "$states" | sed 's/^/      /' || true
}
where="${SUBDIR:+ ($SUBDIR)}"
log "$MODULE vs $URL at ${sha:0:12}$where (eol: differs only in line endings)"
states="$(compare "$DIR" "$STAGE")"
report "$states"
edits="$(grep -cE '^(changed|added|missing) ' <<< "$states" || true)"

if [ "$GO" -eq 0 ]; then
    if [ "$edits" -gt 0 ]; then log "--go commits those $edits files as the zip's local edits"; fi
    [ "${#refusals[@]}" -eq 0 ] || die "--go would refuse"
    log "dry run: nothing changed"
    exit 0
fi

# --- local edits -----------------------------------------------------------
# A file that differs only in line endings takes the zip's bytes too, so the folder stays
# byte for byte what the build and the SQL updater's hashes knew, unless git would see it as
# changed (no autocrlf): then it keeps the checkout's, and no line-ending commit is made.
eol=()
while read -r state f; do
    case "$state" in
        changed|added|eol) mkdir -p "$(dirname "$STAGE/$f")"; cp -p -- "$DIR/$f" "$STAGE/$f" ;;&
        eol) eol+=("$f") ;;
        missing) git -C "$STAGE" rm --quiet -- "$f" ;;
    esac
done <<< "$states"
(cd "$DIR" && find . -path ./.git -prune -o -type d -empty -print) | while IFS= read -r d; do mkdir -p "$STAGE/$d"; done
name="$(git -C "$AC_ROOT" config --local --get user.name 2>/dev/null || true)"
email="$(git -C "$AC_ROOT" config --local --get user.email 2>/dev/null || true)"
if [ -n "$name" ] && [ -n "$email" ]; then
    git -C "$STAGE" config user.name "$name"
    git -C "$STAGE" config user.email "$email"
fi
git -C "$STAGE" -c core.safecrlf=false add -A
for f in "${eol[@]}"; do
    git -C "$STAGE" diff --cached --quiet -- "$f" || git -C "$STAGE" checkout --quiet HEAD -- "$f"
done
if ! git -C "$STAGE" diff --cached --quiet; then
    git -C "$STAGE" commit --quiet -m "Keep the local edits of the $MODULE zip extraction" \
        -m "The zip folder differed from ${sha:0:12}$where in these files:" \
        -m "$(grep -E '^(changed|added|missing) ' <<< "$states")"
fi
after="$(compare "$DIR" "$STAGE")"
if grep -qE '^(changed|added|missing) ' <<< "$after"; then
    grep -E '^(changed|added|missing) ' <<< "$after" | sed 's/^/    /' >&2
    die "the clone does not match the zip folder after the local edits; nothing changed"
fi
ignored="$(git -C "$STAGE" status --porcelain --ignored | grep -c '^!!' || true)"
[ "$ignored" -eq 0 ] || warn "$ignored path(s) of the zip folder are ignored by upstream's .gitignore; kept as files"

# --- swap ------------------------------------------------------------------
OLD="$HOLD/$MODULE.zip-$STAMP"
mv -- "$DIR" "$OLD" || die "could not move $DIR aside (open in another program?); nothing changed"
if ! mv -- "$STAGE" "$DIR"; then
    if mv -- "$OLD" "$DIR"; then warn "could not move the clone into place; the zip folder is back"
    else warn "could not move the clone into place, nor the zip folder back from $OLD"; fi
    exit 2
fi
swapped=1
log "$MODULE is now a git checkout: $branch at $(git -C "$DIR" rev-parse --short HEAD)"
if [ -n "$SUBDIR" ]; then
    printf '    %s; ac-update.sh follows %s with this prefix only.\n' "split of $SUBDIR from ${sha:0:12}" "$remote_head"
    printf '    update: bash %q\n' "$AC_ROOT/scripts/ac-update.sh"
else
    printf '    tracking %s; ac-update.sh fast-forwards it\n' "$remote_head"
fi
printf '    zip folder kept at %s\n' "$OLD"
printf '    rollback: mv %q %q && mv %q %q\n' "$DIR" "$HOLD/$MODULE.git-$STAMP" "$OLD" "$DIR"
printf '    re-run cmake (ac-build.sh) so the build sees the swap\n'
