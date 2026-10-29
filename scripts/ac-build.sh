#!/usr/bin/env bash
# Reconfigure CMake and build worldserver + authserver.
#
# Usage: ac-build.sh [--no-configure]
#   AC_JOBS=N   override parallelism (default 16)

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

DO_CONFIGURE=1
[ "${1:-}" = "--no-configure" ] && DO_CONFIGURE=0

# ---------------------------------------------------------------------------
# Preflight: module folder names.
#
# AzerothCore derives two things from a module's DIRECTORY name:
#   1. modules/CMakeLists.txt does
#        include(".../modules/<dir>/<dir>.cmake" OPTIONAL)
#      so a .cmake not named exactly after its folder is skipped SILENTLY.
#   2. the generated script loader calls Add<dir with _>Scripts(), which must
#      match the Add...Scripts() the module's own source defines, or the build
#      dies at link time with LNK2019.
#
# A folder left named "mod-foo-master" or "mod-foo-main" (how GitHub zips
# extract) breaks both. This check catches it before a 40-minute build does.
# ---------------------------------------------------------------------------
preflight_modules() {
    local bad=0 d name cmake_file sym expected
    for d in "$AC_MODULES"/*/; do
        d="${d%/}"; name="$(basename "$d")"
        [ "$name" = "CMakeFiles" ] && continue

        cmake_file="$(ls "$d"/*.cmake 2>/dev/null | head -1 || true)"
        if [ -n "$cmake_file" ] && [ ! -f "$d/$name.cmake" ]; then
            warn "$name: ships $(basename "$cmake_file") but CMake looks for $name.cmake -- it will be SKIPPED SILENTLY"
            bad=1
        fi

        sym="$(grep -rhoE 'void Add[A-Za-z0-9_]+Scripts\(\)' "$d/src" 2>/dev/null \
               | sed 's/void //; s/()//' | sort -u | tail -1 || true)"
        if [ -n "$sym" ]; then
            expected="Add$(printf '%s' "$name" | tr '-' '_')Scripts"
            if [ "$sym" != "$expected" ]; then
                warn "$name: defines $sym but loader will call $expected -- link will fail (LNK2019)"
                bad=1
            fi
        fi
    done
    if [ "$bad" -ne 0 ]; then
        die "module folder names are wrong. Rename the folder to the module's canonical name (drop any -master/-main suffix) and re-run."
    fi
    log "module preflight OK ($(ls -d "$AC_MODULES"/*/ 2>/dev/null | wc -l) modules)"
}

preflight_modules

[ -d "$AC_BUILD" ] || die "build dir not found: $AC_BUILD"

if [ "$DO_CONFIGURE" -eq 1 ]; then
    log "cmake configure"
    ( cd "$AC_BUILD" && cmake . ) || die "cmake configure failed"
fi

LOGF="$AC_BUILD/build-$(date +%Y%m%d_%H%M%S).log"
log "building with -m:$AC_JOBS -> $LOGF"
set +e
# -m alone only limits concurrent projects; each one still runs /MP with a cl.exe
# per core (33 were seen at -m:16). MultiToolTask with EnforceProcessCountAcrossBuilds
# makes CL_MPCount a cap on cl.exe across the whole build.
( cd "$AC_BUILD" && cmake --build . --config RelWithDebInfo -- "-m:$AC_JOBS" \
    -p:UseMultiToolTask=true -p:EnforceProcessCountAcrossBuilds=true "-p:CL_MPCount=$AC_JOBS" \
    -v:minimal ) > "$LOGF" 2>&1
rc=$?
set -e

errs=$(grep -cE 'error [A-Z]+[0-9]+' "$LOGF" || true)

if [ "$rc" -eq 0 ] && [ "$errs" -eq 0 ]; then
    log "build OK"
    ls -l "$AC_BIN/worldserver.exe" "$AC_BIN/authserver.exe"
    exit 0
fi

printf '\n'
warn "build FAILED (exit $rc, $errs errors). Log: $LOGF"

if grep -qE 'C3859|C1076' "$LOGF"; then
    printf '\n'
    warn "C3859/C1076 = MSVC ran out of memory for the precompiled header."
    warn "This is parallelism, not your code. Re-run with a lower AC_JOBS:"
    warn "    AC_JOBS=8 $0 --no-configure"
    exit "$rc"
fi

printf '\n--- error summary ---\n'
grep -oE 'error [A-Z]+[0-9]+: .*' "$LOGF" | sed 's/\[[A-Z]:.*//' | sort | uniq -c | sort -rn | head -20
printf '\n--- failing files ---\n'
grep -oE "\(compiling source file '[^']+'\)" "$LOGF" | sed "s/.*source file '//; s/'.*//" | sort -u | head -20
exit "$rc"
