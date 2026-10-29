#!/usr/bin/env bash
# shellcheck disable=SC2034  # these variables are used by the scripts that source this file
# Shared paths and helpers for the AzerothCore maintenance scripts.
# Sourced by the ac-*.sh scripts; not meant to be run directly.

set -euo pipefail

AC_ROOT="${AC_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
AC_SRC="$AC_ROOT/azerothcore-wotlk"
AC_BUILD="$AC_ROOT/build"
AC_BIN="$AC_BUILD/bin/RelWithDebInfo"
AC_CONF="$AC_BIN/configs"
AC_MODCONF="$AC_CONF/modules"
AC_MODULES="$AC_SRC/modules"
AC_BACKUP="$AC_ROOT/db_backup"

# 16, not 32. Above ~16 concurrent cl.exe the large PCH exhausts the commit
# limit and MSVC fails with C3859 / C1076. See CLAUDE.md.
AC_JOBS="${AC_JOBS:-16}"

MYSQL_DIR="${MYSQL_DIR:-C:/Program Files/MySQL/MySQL Server 8.4/bin}"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Read DB credentials out of worldserver.conf so there is no second copy of the
# password on disk. Sets DB_HOST/DB_PORT/DB_USER/DB_PASS/DB_NAME.
# Never prints DB_PASS.
ac_db_creds() {
    local key="${1:-WorldDatabaseInfo}"
    local conf="$AC_CONF/worldserver.conf"
    [ -f "$conf" ] || die "worldserver.conf not found at $conf"
    local line
    line=$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$conf" | head -1 | sed 's/.*= *"//; s/"[[:space:]]*$//')
    [ -n "$line" ] || die "$key not found in $conf"
    IFS=';' read -r DB_HOST DB_PORT DB_USER DB_PASS DB_NAME <<< "$line"
}

# Every module directory that is a git repo.
ac_git_modules() {
    local d
    for d in "$AC_MODULES"/*/; do
        [ -d "$d/.git" ] && printf '%s\n' "${d%/}"
    done
}
