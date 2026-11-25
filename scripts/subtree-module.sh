#!/usr/bin/env bash
# Helpers for modules whose repository keeps the module below its root.
# Local git config records the prefix, remote branch, original source and split.

ac_subtree_read() {
    local repo="$1" revision tree source_tree
    AC_SUBTREE_PREFIX=$(git -C "$repo" config --local --get leeroy.subtreePrefix || true)
    AC_SUBTREE_UPSTREAM=$(git -C "$repo" config --local --get leeroy.subtreeUpstream || true)
    revision=$(git -C "$repo" config --local --get leeroy.subtreeRevision || true)
    if [ -z "$AC_SUBTREE_PREFIX$AC_SUBTREE_UPSTREAM$revision" ]; then return 1; fi
    [ -n "$AC_SUBTREE_PREFIX" ] && [ -n "$AC_SUBTREE_UPSTREAM" ] && [ -n "$revision" ] \
        || die "$(basename "$repo"): incomplete subtree metadata; nothing can update this module safely"
    case "/$AC_SUBTREE_PREFIX/" in
        *'/../'*|*'/./'*|*'//'*|*'\'*|*':'*|*$'\n'*|*$'\r'*) die "invalid subtree prefix" ;;
    esac
    [[ "$AC_SUBTREE_UPSTREAM" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/.+$ ]] \
        && git check-ref-format "refs/remotes/$AC_SUBTREE_UPSTREAM" \
        || die "invalid subtree upstream"
    [[ "$revision" =~ ^([0-9a-f]{40}|[0-9a-f]{64})\ ([0-9a-f]{40}|[0-9a-f]{64})$ ]] \
        || die "invalid subtree revision"
    AC_SUBTREE_SOURCE=${revision%% *}
    AC_SUBTREE_BASE=${revision#* }
    tree=$(git -C "$repo" rev-parse --verify "$AC_SUBTREE_BASE^{tree}") || die "missing subtree base"
    source_tree=$(git -C "$repo" rev-parse --verify "$AC_SUBTREE_SOURCE:$AC_SUBTREE_PREFIX") \
        || die "missing subtree source"
    [ "$tree" = "$source_tree" ] || die "subtree base does not match its source folder"
    git -C "$repo" merge-base --is-ancestor "$AC_SUBTREE_BASE" HEAD \
        || die "subtree base is not an ancestor of this checkout; repair metadata before updating"
}

ac_subtree_split() (
    # Subshell keeps cleanup traps out of the caller. Nothing is checked out into
    # the installed module: old Git needs the prefix on disk in a temporary worktree.
    set -euo pipefail
    local repo="$1" prefix="$2" source="$3" root folder work
    root=$(cd "${TMPDIR:-/tmp}" && pwd -P)
    folder=$(mktemp -d "$root/leeroy-subtree.XXXXXX")
    case "$folder" in "$root"/leeroy-subtree.*) ;; *) die "unexpected subtree scratch path" ;; esac
    work="$folder/work"
    cleanup() {
        if [ -e "$work/.git" ]; then git -C "$repo" worktree remove --force "$work" >&2 || return; fi
        rmdir "$folder" 2>/dev/null || true
    }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    GIT_LFS_SKIP_SMUDGE=1 git -C "$repo" worktree add --quiet --detach "$work" "$source" >&2
    (cd "$work" && git subtree split -q --prefix="$prefix" HEAD)
)
