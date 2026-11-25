#!/usr/bin/env bash
# Temporary repositories, folders and a stub process list only; never the installed core.
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
temp_root=$(cd "${TMPDIR:-/tmp}" && pwd -P)
scratch=$(mktemp -d "$temp_root/leeroy-module-to-git.XXXXXX")
case "$scratch" in "$temp_root"/leeroy-module-to-git.*) ;; *) exit 1 ;; esac
trap 'rm -rf -- "$scratch"' EXIT

export GIT_AUTHOR_NAME=tester GIT_AUTHOR_EMAIL=tester@example.invalid
export GIT_COMMITTER_NAME=tester GIT_COMMITTER_EMAIL=tester@example.invalid
export AC_ROOT="$scratch/install with spaces" TMPDIR="$scratch/tmp" AC_TASKLIST="$scratch/tasklist"
export AC_PROCESSES="$scratch/servers"
modules="$AC_ROOT/azerothcore-wotlk/modules"
mkdir -p "$modules" "$TMPDIR"
printf '#!/usr/bin/env bash\ncat "%s/procs"\n' "$scratch" > "$AC_TASKLIST"
printf '#!/usr/bin/env bash\ncat "%s/server-rows"\nprintf "end\\r\\n"\n' "$scratch" > "$AC_PROCESSES"
chmod +x "$AC_TASKLIST" "$AC_PROCESSES"
printf '"explorer.exe","100","Console","1","9,000 K"\r\n' > "$scratch/procs"
# A dojo's worldserver runs throughout: only this install's servers may block the swap.
live_bin="$AC_ROOT/build/bin/RelWithDebInfo"
if command -v cygpath >/dev/null 2>&1; then live_bin=$(cygpath -w "$live_bin"); fi
dojo_row=$'400\tworldserver.exe\tF:\\azerothcore\\dojo-runtime\\instances\\claude-raids\\bin\\worldserver.exe\r\n'
printf '%s' "$dojo_row" > "$scratch/server-rows"

checks=0
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok() { checks=$((checks + 1)); }
has() { grep -Fq -- "$1" <<< "$output" || fail "missing '$1' in: $output"; ok; }
run() { set +e; output=$(bash "$scripts/ac-module-to-git.sh" "$@" 2>&1); code=$?; set -e; }
expect() { [ "$code" -eq "$1" ] || fail "exit $code, wanted $1: $output"; ok; }
digest() { (cd "$1" && find . -path ./.git -prune -o -type f -print | LC_ALL=C sort | while IFS= read -r f; do
    printf '%s %s\n' "$f" "$(tr -d '\r' < "$f" | cksum)"; done); }
bytes() { (cd "$1" && find . -path ./.git -prune -o -type f -print | LC_ALL=C sort | xargs cksum); }
g() { git -c core.safecrlf=false -C "$1" "${@:2}"; }

# --- upstream with the module at its root ---------------------------------
up="$scratch/upstream plain"
git init --quiet --initial-branch=main "$up"
mkdir -p "$up/src" "$up/conf" "$up/data/sql/db-world/base"
printf 'int a() { return 1; }\n' > "$up/src/a.cpp"
printf 'Plain.Enable = 1\n' > "$up/conf/plain.conf.dist"
printf 'DELETE FROM t;\n' > "$up/data/sql/db-world/base/plain.sql"
printf 'readme\n' > "$up/README.md"
g "$up" add -A && g "$up" commit --quiet -m first
printf 'int a() { return 2; }\nint b() { return 3; }\n' > "$up/src/a.cpp"
g "$up" commit --quiet -am second
zip_commit=$(g "$up" rev-parse HEAD)
printf 'int c();\n' > "$up/src/c.cpp"
g "$up" add -A && g "$up" commit --quiet -m third
g "$up" checkout --quiet -b side "$zip_commit"
printf 'side\n' > "$up/side.txt"
g "$up" add -A && g "$up" commit --quiet -m side
side_commit=$(g "$up" rev-parse HEAD)
g "$up" checkout --quiet main

# The zip: the second commit's files, one line-ending change and three real edits.
zip="$modules/mod-plain"
mkdir -p "$zip"
g "$up" archive "$zip_commit" | tar -x -C "$zip"
sed 's/$/\r/' "$zip/src/a.cpp" > "$zip/src/a.tmp" && mv "$zip/src/a.tmp" "$zip/src/a.cpp"
printf 'Plain.Enable = 0\n' > "$zip/conf/plain.conf.dist"
printf '#define LOCAL 1\n' > "$zip/src/local.h"
rm "$zip/README.md"
mkdir -p "$zip/data/sql/db-world/updates"
before=$(bytes "$zip")

# --- dry run: compares, changes nothing ------------------------------------
run mod-plain "$up" "${zip_commit:0:8}"
expect 0
has 'changed  1'
has 'added    1'
has 'missing  1'
has 'changed conf/plain.conf.dist'
has 'added src/local.h'
has 'missing README.md'
has 'dry run: nothing changed'
[ "$(bytes "$zip")" = "$before" ] || fail "dry run changed the zip folder"; ok
[ ! -e "$AC_ROOT/module-to-git" ] || fail "dry run wrote into the install"; ok
[ -z "$(ls -A "$TMPDIR")" ] || fail "dry run left its clone behind"; ok

# --- refusals: nothing changes ---------------------------------------------
mkdir -p "$AC_ROOT/.superpowers/live-build.lock"
run --go mod-plain "$up" "$zip_commit"; expect 1; has 'native build lock is held'
run mod-plain "$up" "$zip_commit"; expect 1; has '--go would refuse'
rmdir "$AC_ROOT/.superpowers/live-build.lock"
printf '"worldserver.exe","200","Console","1","9,000 K"\r\n"cl.exe","300","Console","1","1 K"\r\n' >> "$scratch/procs"
run --go mod-plain "$up" "$zip_commit"; expect 1; has 'still running: cl.exe'
printf '"explorer.exe","100","Console","1","9,000 K"\r\n' > "$scratch/procs"
printf '200\tWORLDSERVER.EXE\t%s\\WorldServer.exe\r\n' "$live_bin" >> "$scratch/server-rows"
run --go mod-plain "$up" "$zip_commit"; expect 1; has 'worldserver.exe still running (pid 200)'
printf '%s300\tauthserver.exe\t\r\n' "$dojo_row" > "$scratch/server-rows"
run --go mod-plain "$up" "$zip_commit"; expect 1; has '1 authserver.exe whose path cannot be read still running'
printf '%s' "$dojo_row" > "$scratch/server-rows"
AC_TASKLIST=false run --go mod-plain "$up" "$zip_commit"; expect 1; has 'cannot read the process list'
printf '#!/usr/bin/env bash\n' > "$scratch/silent"; chmod +x "$scratch/silent"
AC_TASKLIST="$scratch/silent" run --go mod-plain "$up" "$zip_commit"; expect 1; has 'cannot read the process list'
AC_PROCESSES=false run --go mod-plain "$up" "$zip_commit"; expect 1; has 'cannot read the server processes'
AC_PROCESSES="$scratch/silent" run --go mod-plain "$up" "$zip_commit"; expect 1; has 'cannot read the server processes'
run --go mod-plain "$up" 0123456789abcdef; expect 1; has 'no commit 0123456789abcdef upstream'
run --go mod-plain "$up" "$side_commit"; expect 1; has 'is not on origin/main'
run --go --subdir ../x mod-plain "$up" "$zip_commit"; expect 1; has 'not a plain relative path'
run --go mod-missing "$up" "$zip_commit"; expect 1; has 'no module folder'
mkdir -p "$modules/mod-zipped-master"
run --go mod-zipped-master "$up" "$zip_commit"; expect 1; has 'GitHub zip suffix'
ln -s "$zip" "$modules/mod-link" 2>/dev/null || true
if [ -L "$modules/mod-link" ]; then   # Git Bash without native symlinks copies instead
    run --go mod-link "$up" "$zip_commit"; expect 1; has 'link or junction'
fi
rm -rf "$modules/mod-link"
[ "$(bytes "$zip")" = "$before" ] || fail "a refusal changed the zip folder"; ok
[ -z "$(find "$AC_ROOT/module-to-git" -mindepth 1 2>/dev/null)" ] || fail "a refusal left a clone behind"; ok

# --- the swap ----------------------------------------------------------------
wanted=$(digest "$zip")
run --go mod-plain "$up" "$zip_commit"
expect 0
has 'mod-plain is now a git checkout: main'
has 'tracking origin/main'
[ -d "$zip/.git" ] || fail "no git checkout"; ok
[ "$(digest "$zip")" = "$wanted" ] || fail "the folder no longer holds the zip's content"; ok
[ -d "$zip/data/sql/db-world/updates" ] || fail "an empty folder of the zip was lost"; ok
[ -z "$(g "$zip" status --porcelain)" ] || fail "the checkout is not clean"; ok
[ "$(g "$zip" rev-parse HEAD~1)" = "$zip_commit" ] || fail "the local edits are not one commit on the zip's"; ok
[ "$(g "$zip" log -1 --format=%s)" = 'Keep the local edits of the mod-plain zip extraction' ] || fail "commit subject"; ok
[ "$(g "$zip" diff --name-only HEAD~1 HEAD | LC_ALL=C sort | tr '\n' ' ')" = 'README.md conf/plain.conf.dist src/local.h ' ] \
    || fail "the local commit holds more than the real edits"; ok
[ "$(g "$zip" rev-parse --abbrev-ref '@{u}')" = origin/main ] || fail "no upstream"; ok
[ "$(g "$zip" rev-list --count HEAD..origin/main)" = 1 ] || fail "upstream's newer commit is not ahead"; ok
g "$zip" rebase --quiet origin/main
[ -f "$zip/src/c.cpp" ] && [ -f "$zip/src/local.h" ] || fail "the update cannot carry the local edits"; ok
kept=("$AC_ROOT"/module-to-git/mod-plain.zip-*)
[ "${#kept[@]}" -eq 1 ] && [ "$(bytes "${kept[0]}")" = "$before" ] || fail "the zip folder was not kept as it was"; ok
[ -z "$(find "$AC_ROOT/module-to-git" -maxdepth 1 -name '.*.clone')" ] || fail "a staging clone was left"; ok
run --go mod-plain "$up" "$zip_commit"; expect 1; has 'already a git checkout'

# --- a module kept in a subfolder of its upstream -----------------------------
up2="$scratch/upstream nested"
git init --quiet --initial-branch=master "$up2"
mkdir -p "$up2/server-side/mod-sub/src" "$up2/server-side/mod-sub/conf" "$up2/client-side"
printf 'void x();\n' > "$up2/server-side/mod-sub/src/x.cpp"
printf 'Sub.Enable = 1\n' > "$up2/server-side/mod-sub/conf/sub.conf.dist"
printf 'big\n' > "$up2/client-side/Map.dbc"
printf 'readme\n' > "$up2/README.md"
g "$up2" add -A && g "$up2" commit --quiet -m first
printf 'docs\n' >> "$up2/README.md"
g "$up2" commit --quiet -am docs
sub_commit=$(g "$up2" rev-parse HEAD)
zip2="$modules/mod-sub"
mkdir -p "$zip2"
mkdir -p "$scratch/x"
g "$up2" archive "$sub_commit" server-side/mod-sub | tar -x -C "$scratch/x"
mv "$scratch/x/server-side/mod-sub/"* "$zip2/"
wanted2=$(bytes "$zip2")
run mod-sub "$up2" "$sub_commit"; expect 0; has 'added    2'; has 'missing  4'   # the whole repository
run --go --subdir server-side/mod-sub/ mod-sub "$up2" "$sub_commit"
expect 0
has 'no upstream (ac-update.sh skips it)'
[ "$(bytes "$zip2")" = "$wanted2" ] || fail "the nested module's folder changed"; ok
[ "$(g "$zip2" rev-parse 'HEAD^{tree}')" = "$(g "$up2" rev-parse "$sub_commit:server-side/mod-sub")" ] \
    || fail "the split is not the subfolder's tree"; ok
[ ! -e "$zip2/client-side" ] && [ ! -e "$zip2/README.md" ] || fail "the rest of the repository leaked in"; ok
if g "$zip2" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then fail "the split tracks an upstream"; fi; ok
[ -z "$(g "$zip2" status --porcelain)" ] || fail "the nested checkout is not clean"; ok
# The printed recipe updates it.
printf 'void y();\n' > "$up2/server-side/mod-sub/src/y.cpp"
g "$up2" add -A && g "$up2" commit --quiet -m y
recipe=$(grep -F 'git fetch origin' <<< "$output" | sed 's/^ *//')
(eval "$recipe") >/dev/null 2>&1 || fail "the update recipe failed: $recipe"
[ -f "$zip2/src/y.cpp" ] || fail "the recipe did not bring upstream's change"; ok

printf 'ac-module-to-git self-test: %d checks passed\n' "$checks"
