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

# --- game server processes ---------------------------------------------------
# A dojo (or any other copy of the core) runs its own authserver.exe and worldserver.exe,
# so this install's servers are the ones running $AC_BIN's executables: told apart by
# path, never by image name (2026-11-24: a dojo worldserver made `ac-server.sh start`
# skip the live one, and `stop` terminated every process of those names).
# The listing has one "pid<TAB>image<TAB>executable path" line per process named
# authserver.exe or worldserver.exe (the path empty when Windows does not give it), then
# a line "end", so that an empty or cut-off listing never reads as "none running".
# AC_PROCESSES names a command that prints a listing instead (tests).
AC_SERVERS_PS=$'$ErrorActionPreference = \'Stop\'; [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); Get-CimInstance Win32_Process -Filter "Name = \'authserver.exe\' OR Name = \'worldserver.exe\'" | ForEach-Object { "$($_.ProcessId)`t$($_.Name)`t$($_.ExecutablePath)" }; \'end\''

ac_server_processes() {
    if [ -n "${AC_PROCESSES:-}" ]; then "$AC_PROCESSES"; else
        powershell -NoProfile -NonInteractive -Command "$AC_SERVERS_PS" </dev/null
    fi
}

# A Windows path as a comparable key in REPLY: backslashes, no \\?\ prefix, lower case.
# shellcheck disable=SC1003  # the quoted \\?\ is the prefix itself, not an escaped quote
ac_path_key() { REPLY=${1//\//\\}; REPLY=${REPLY#'\\?\'}; REPLY=${REPLY,,}; }

# Reads one listing into AC_PROCS. 1 (and AC_PROCS empty) when it cannot be read, a line
# is malformed or the "end" line is missing.
ac_list_servers() {
    local row re=$'^[0-9]+\t[^\t]+\t'
    if ! AC_PROCS=$(ac_server_processes 2>/dev/null | tr -d '\r'); then AC_PROCS=''; return 1; fi
    case "$AC_PROCS" in
        end) AC_PROCS='' ;;
        *$'\n'end) AC_PROCS=${AC_PROCS%$'\n'end} ;;
        *) AC_PROCS=''; return 1 ;;
    esac
    while IFS= read -r row; do
        if [ -n "$row" ] && ! [[ $row =~ $re ]]; then AC_PROCS=''; return 1; fi
    done <<< "$AC_PROCS"
}

# For image $1 (authserver.exe or worldserver.exe) in AC_PROCS: AC_LIVE, this install's
# PIDs; AC_ELSEWHERE, how many run from another path; AC_UNREAD, how many whose path
# cannot be read (never this install's for certain, so never stopped either).
ac_scan_servers() {
    local want=${1,,} pid image path
    if [ -z "${AC_BIN_KEY:-}" ]; then
        if command -v cygpath >/dev/null 2>&1; then ac_path_key "$(cygpath -w "$AC_BIN")"; else ac_path_key "$AC_BIN"; fi
        AC_BIN_KEY=${REPLY%\\}
    fi
    AC_LIVE='' AC_ELSEWHERE=0 AC_UNREAD=0
    while IFS=$'\t' read -r pid image path; do
        [ -n "$pid" ] && [ "${image,,}" = "$want" ] || continue
        if [ -z "$path" ]; then AC_UNREAD=$((AC_UNREAD + 1)); continue; fi
        ac_path_key "$path"
        if [ "$REPLY" = "$AC_BIN_KEY\\$want" ]; then AC_LIVE="${AC_LIVE:+$AC_LIVE }$pid"
        else AC_ELSEWHERE=$((AC_ELSEWHERE + 1)); fi
    done <<< "$AC_PROCS"
    return 0
}
