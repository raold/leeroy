#!/usr/bin/env bash
# Fast-forward the core and every git-backed module, then report what changed.
# Refuses to touch a repo with local modifications.
#
# Usage: ac-update.sh [--dry-run]

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/config-keys.sh"
source "$(dirname "${BASH_SOURCE[0]}")/subtree-module.sh"

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

STATE="$(dirname "${BASH_SOURCE[0]}")/.last-update"
REPOS=("$AC_SRC")
while read -r m; do REPOS+=("$m"); done < <(ac_git_modules)

# --- guard: nothing dirty -----------------------------------------------
dirty=0
for r in "${REPOS[@]}"; do
    if [ -n "$(git -C "$r" status --porcelain --untracked-files=no)" ]; then
        warn "$(basename "$r") has uncommitted changes:"
        git -C "$r" status --short --untracked-files=no | sed 's/^/      /'
        dirty=1
    fi
    # Validate every recorded split before updating any checkout.
    if ac_subtree_read "$r"; then :; fi
done
[ "$dirty" -eq 0 ] || die "refusing to update with local modifications. Commit, stash, or revert them first."

# --- record rollback points ---------------------------------------------
if [ "$DRY" -eq 0 ]; then
    : > "$STATE"
    : > "$STATE.subtrees"
    for r in "${REPOS[@]}"; do
        printf '%s %s\n' "$r" "$(git -C "$r" rev-parse HEAD)" >> "$STATE"
        if ac_subtree_read "$r"; then
            printf 'git -C %q config --local leeroy.subtreeRevision %q\n' \
                "$r" "$AC_SUBTREE_SOURCE $AC_SUBTREE_BASE" >> "$STATE.subtrees"
        fi
    done
    log "rollback points written to $STATE"
    [ ! -s "$STATE.subtrees" ] || log "subtree rollback metadata written to $STATE.subtrees (restore with the saved HEADs)"
fi

# --- fetch + fast-forward ------------------------------------------------
changed=0
for r in "${REPOS[@]}"; do
    name="$(basename "$r")"
    branch="$(git -C "$r" branch --show-current)"
    [ -n "$branch" ] || { warn "$name: detached HEAD, skipping"; continue; }

    if ac_subtree_read "$r"; then
        remote=${AC_SUBTREE_UPSTREAM%%/*}
        remote_branch=${AC_SUBTREE_UPSTREAM#*/}
        git -C "$r" fetch --quiet --no-tags "$remote" \
            "refs/heads/$remote_branch:refs/remotes/$AC_SUBTREE_UPSTREAM"
        source=$(git -C "$r" rev-parse --verify "refs/remotes/$AC_SUBTREE_UPSTREAM^{commit}")
        git -C "$r" merge-base --is-ancestor "$AC_SUBTREE_SOURCE" "$source" \
            || die "$name: upstream history changed; review the subtree manually"
        [ "$(git -C "$r" cat-file -t "$source:$AC_SUBTREE_PREFIX" 2>/dev/null)" = tree ] \
            || die "$name: upstream no longer contains the subtree folder; checkout unchanged"
        target=$(ac_subtree_split "$r" "$AC_SUBTREE_PREFIX" "$source") \
            || die "$name: subtree split failed; module checkout unchanged"
        git -C "$r" merge-base --is-ancestor "$AC_SUBTREE_BASE" "$target" \
            || die "$name: new split does not extend the recorded base; review manually"
        ahead=$(git -C "$r" rev-list --count "$AC_SUBTREE_BASE..HEAD")
        behind=$(git -C "$r" rev-list --count "$AC_SUBTREE_BASE..$target")
        # The checkout can advance before the metadata write fails or the process
        # stops. Recognize the actual new base on retry; do not replay its commits
        # as though they were local patches.
        if git -C "$r" merge-base --is-ancestor "$target" HEAD; then
            behind=0
            ahead=$(git -C "$r" rev-list --count "$target..HEAD")
        fi
        printf '    %-28s subtree: %s upstream commits, %s local commits\n' "$name" "$behind" "$ahead"
        if [ "$behind" -gt 0 ]; then
            changed=1
            if [ "$DRY" -eq 0 ]; then
                if [ "$ahead" -gt 0 ]; then
                    git -C "$r" rebase --quiet --rebase-merges --onto "$target" "$AC_SUBTREE_BASE" \
                        || { git -C "$r" rebase --abort; die "$name: subtree conflict; original checkout restored. Rebase manually."; }
                else
                    git -C "$r" merge --ff-only --quiet "$target" || die "$name: subtree fast-forward failed"
                fi
            fi
        fi
        if [ "$DRY" -eq 0 ]; then
            if ! git -C "$r" config --local leeroy.subtreeRevision "$source $target"; then
                printf '    record the completed update: git -C %q config --local leeroy.subtreeRevision %q\n' \
                    "$r" "$source $target" >&2
                die "$name: checkout updated but subtree metadata could not be saved; repair it before the next update"
            fi
        fi
        continue
    fi

    upstream="$(git -C "$r" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    if [ -z "$upstream" ]; then warn "$name: branch '$branch' has no upstream, skipping"; continue; fi

    git -C "$r" fetch --quiet "${upstream%%/*}" "$branch"
    behind="$(git -C "$r" rev-list --count "HEAD..$upstream")"
    ahead="$(git -C "$r" rev-list --count "$upstream..HEAD")"
    local_note=""
    [ "$ahead" -gt 0 ] && local_note=", carrying $ahead local commit(s)"

    if [ "$behind" -eq 0 ]; then
        printf '    %-28s up to date%s\n' "$name" "$local_note"
        continue
    fi

    changed=1
    printf '    %-28s %s commits behind %s%s\n' "$name" "$behind" "$upstream" "$local_note"
    if [ "$DRY" -eq 0 ]; then
        if [ "$ahead" -gt 0 ]; then
            # Local patches (e.g. the playerbots raid browser listing) are replayed on top of upstream.
            git -C "$r" rebase --quiet "$upstream" \
                || { git -C "$r" rebase --abort; die "$name: local commits conflict with $upstream. Rebase manually."; }
        else
            git -C "$r" merge --ff-only "$upstream" --quiet \
                || die "$name: fast-forward failed (diverged?). Resolve manually."
        fi
    fi
done

# Report the checked-out templates even when nothing needs pulling. Dry run
# still fetches metadata above, but never merges/rebases or writes rollback points.
ac_report_all_config_keys "${REPOS[@]}"
if [ "$changed" -eq 0 ]; then log "everything already current"; exit 0; fi
[ "$DRY" -eq 1 ] && { log "dry run, nothing changed"; exit 0; }

printf '\n'
log "source updated. Now run:  ./ac-backup.sh pre_update && ./ac-build.sh"
log "the DB auto-updater applies new SQL on the next worldserver start."
