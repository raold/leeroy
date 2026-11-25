#!/usr/bin/env bash
# Copy the live configs and the local commits of mod-playerbots,
# mod-ollama-chat, mod-dungeon-clear and mod-individual-progression into this repo so they are versioned. The live files stay the
# source of truth: this only snapshots them. Secrets are masked on the way in, and the copies are then
# checked against the real values before anything can be committed.
#
# Usage: ac-snapshot.sh
#
# AC_SNAPSHOT_OUT names the checkout that receives config/ and patches/ (default
# AC_ROOT). With AC_ROOT=/f/azerothcore and AC_SNAPSHOT_OUT set to a worktree,
# the live install is only read and the snapshot lands in the worktree.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

OUT="${AC_SNAPSHOT_OUT:-$AC_ROOT}"
[ -e "$OUT/.git" ] || die "AC_SNAPSHOT_OUT is not a git checkout: $OUT"
OUT="$(cd "$OUT" && pwd)"   # format-patch -o under git -C would resolve a relative path in the module
OUT_CONF="$OUT/config"

# Credentials, tokens and keys. Empty and numeric values are flags or unset, so
# they are kept; anything else becomes "<redacted>".
SECRET_KEYS='[A-Za-z0-9_.]*(DatabaseInfo|Secret|Token|ApiKey|Password)'

mask() {
    sed -E "/^[[:space:]]*${SECRET_KEYS}[[:space:]]*=/{
        /=[[:space:]]*(\"\")?[[:space:]]*\$/b
        /=[[:space:]]*[0-9]+[[:space:]]*\$/b
        s/=.*/= \"<redacted>\"/
    }" "$1"
}

# --- configs -------------------------------------------------------------
rm -rf "$OUT_CONF"
mkdir -p "$OUT_CONF/modules"
for f in "$AC_CONF"/*.conf "$AC_MODCONF"/*.conf; do
    rel="${f#"$AC_CONF"/}"
    mask "$f" > "$OUT_CONF/$rel"
done
log "configs: $(find "$OUT_CONF" -name '*.conf' | wc -l) files -> config/"

# --- module patches ------------------------------------------------------
# Local commits of a module repo (everything ahead of its upstream branch),
# written to patches/<module>/. Only that directory is ever cleared.
export_patches() {
    local module="$1" repo="$AC_MODULES/$1" out="$OUT/patches/$1" upstream
    upstream="$(git -C "$repo" rev-parse --abbrev-ref --symbolic-full-name '@{u}')"
    rm -rf "$out"
    mkdir -p "$out"
    git -C "$repo" format-patch --quiet --no-signature -o "$out" "$upstream..HEAD"
    log "patches: $(find "$out" -name '*.patch' | wc -l) commits on top of $upstream -> patches/$module/"
}
export_patches mod-playerbots
export_patches mod-ollama-chat
# mod-dungeon-clear's local commits (Trial of the Crusader, the Ulduar Leviathan pass). A live checkout
# without them exports none and clears patches/mod-dungeon-clear: apply the series (git am) first.
export_patches mod-dungeon-clear
# mod-individual-progression's local commit (level 80 and above may enter WotLK Onyxia). Same rule: a live
# checkout without it exports none and clears patches/mod-individual-progression.
export_patches mod-individual-progression

# --- leak check ----------------------------------------------------------
# Every secret value in the live configs must be absent from the copies: the
# password field of each DatabaseInfo, and the whole value of the other keys.
# Values are compared in memory and never printed.
leaks=0
while IFS= read -r line; do
    value="$(sed -E 's/^[^=]*=[[:space:]]*"?//; s/"?[[:space:]]*$//' <<< "$line")"
    case "${line%%=*}" in *DatabaseInfo*) IFS=';' read -r _ _ _ value _ <<< "$value" ;; esac
    [ "${#value}" -ge 4 ] && [[ ! "$value" =~ ^[0-9]+$ ]] || continue
    # A value the .dist templates already contain (a stock default) is public, and
    # their comment text would otherwise trip this check.
    grep -qF -- "$value" "$AC_CONF"/*.conf.dist "$AC_MODCONF"/*.conf.dist && continue
    while IFS= read -r hit; do
        warn "a secret from the live configs appears in ${hit#"$OUT"/}"
        leaks=1
    done < <(grep -rlF -- "$value" "$OUT_CONF" "$OUT/patches" || true)
done < <(grep -hE "^[[:space:]]*${SECRET_KEYS}[[:space:]]*=" "$AC_CONF"/*.conf "$AC_MODCONF"/*.conf)
[ "$leaks" -eq 0 ] || die "secrets leaked into the snapshot; it was not cleaned up, fix the mask before committing"
log "leak check passed"
