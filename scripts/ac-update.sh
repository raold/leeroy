#!/usr/bin/env bash
# Fast-forward the core and every git-backed module, then report what changed.
# Refuses to touch a repo with local modifications.
#
# Usage: ac-update.sh [--dry-run]

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

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
done
[ "$dirty" -eq 0 ] || die "refusing to update with local modifications. Commit, stash, or revert them first."

# --- record rollback points ---------------------------------------------
if [ "$DRY" -eq 0 ]; then
    : > "$STATE"
    for r in "${REPOS[@]}"; do
        printf '%s %s\n' "$r" "$(git -C "$r" rev-parse HEAD)" >> "$STATE"
    done
    log "rollback points written to $STATE"
fi

# --- fetch + fast-forward ------------------------------------------------
changed=0
for r in "${REPOS[@]}"; do
    name="$(basename "$r")"
    branch="$(git -C "$r" branch --show-current)"
    [ -n "$branch" ] || { warn "$name: detached HEAD, skipping"; continue; }

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

if [ "$changed" -eq 0 ]; then log "everything already current"; exit 0; fi
[ "$DRY" -eq 1 ] && { log "dry run, nothing changed"; exit 0; }

# --- new config keys -----------------------------------------------------
# A pull often adds .conf.dist keys your live .conf does not have. Missing keys
# fall back to compiled-in defaults, but you should know. Module templates live
# in <repo>/conf/; the core's are under src/server/apps/ and map to configs/.
new_keys() {
    local dist="$1" livedir="$2" live
    live="$livedir/$(basename "$dist" .dist)"
    [ -f "$live" ] || { warn "$(basename "$dist" .dist) missing from $livedir -- copy it from $dist"; return; }
    grep -oE '^[A-Za-z][A-Za-z0-9_.]*[[:space:]]*=' "$dist" | sed 's/[[:space:]]*=//' | sort -u |
    while read -r k; do
        grep -qE "^[#[:space:]]*${k}[[:space:]]*=" "$live" || printf '    new key: %-50s (%s)\n' "$k" "$(basename "$live")"
    done
}

log "checking for new config keys"
for dist in "$AC_SRC"/src/server/apps/{worldserver,authserver}/*.conf.dist; do
    [ -e "$dist" ] && new_keys "$dist" "$AC_CONF"
done
for r in "${REPOS[@]}"; do
    for dist in "$r"/conf/*.conf.dist; do
        [ -e "$dist" ] && new_keys "$dist" "$AC_MODCONF"
    done
done

printf '\n'
log "source updated. Now run:  ./ac-backup.sh pre_update && ./ac-build.sh"
log "the DB auto-updater applies new SQL on the next worldserver start."
