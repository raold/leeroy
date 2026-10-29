#!/usr/bin/env bash
# Copy the live configs and the local mod-playerbots commits into this repo so
# they are versioned. The live files stay the source of truth: this only
# snapshots them. Secrets are masked on the way in, and the copies are then
# checked against the real values before anything can be committed.
#
# Usage: ac-snapshot.sh

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

OUT_CONF="$AC_ROOT/config"
OUT_PATCH="$AC_ROOT/patches/mod-playerbots"
PLAYERBOTS="$AC_MODULES/mod-playerbots"

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

# --- playerbots patches --------------------------------------------------
upstream="$(git -C "$PLAYERBOTS" rev-parse --abbrev-ref --symbolic-full-name '@{u}')"
rm -rf "$OUT_PATCH"
mkdir -p "$OUT_PATCH"
git -C "$PLAYERBOTS" format-patch --quiet --no-signature -o "$OUT_PATCH" "$upstream..HEAD"
log "patches: $(find "$OUT_PATCH" -name '*.patch' | wc -l) commits on top of $upstream -> patches/mod-playerbots/"

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
        warn "a secret from the live configs appears in ${hit#"$AC_ROOT"/}"
        leaks=1
    done < <(grep -rlF -- "$value" "$OUT_CONF" "$OUT_PATCH" || true)
done < <(grep -hE "^[[:space:]]*${SECRET_KEYS}[[:space:]]*=" "$AC_CONF"/*.conf "$AC_MODCONF"/*.conf)
[ "$leaks" -eq 0 ] || die "secrets leaked into the snapshot; it was not cleaned up, fix the mask before committing"
log "leak check passed"
