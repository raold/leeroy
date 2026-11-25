#!/usr/bin/env bash
# End-to-end converter/updater exercise, local repositories and stub processes only.
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
temp_root=$(cd "${TMPDIR:-/tmp}" && pwd -P)
scratch=$(mktemp -d "$temp_root/leeroy-subtree-test.XXXXXX")
case "$scratch" in "$temp_root"/leeroy-subtree-test.*) ;; *) exit 1 ;; esac
trap 'rm -rf -- "$scratch"' EXIT
export GIT_AUTHOR_NAME=tester GIT_AUTHOR_EMAIL=tester@example.invalid
export GIT_COMMITTER_NAME=tester GIT_COMMITTER_EMAIL=tester@example.invalid
export AC_ROOT="$scratch/install with spaces" TMPDIR="$scratch/tmp" AC_TASKLIST="$scratch/tasklist"
export AC_PROCESSES="$scratch/servers"
mkdir -p "$AC_ROOT/scripts" "$TMPDIR"
cp "$scripts/"{ac-update,common,config-keys,subtree-module}.sh "$AC_ROOT/scripts/"
printf '#!/usr/bin/env bash\nprintf '\''"explorer.exe","100","Console","1","900 K"\\n'\''\n' > "$AC_TASKLIST"
printf '#!/usr/bin/env bash\ncat "%s/server-rows"\n' "$scratch" > "$AC_PROCESSES"
printf 'end\n' > "$scratch/server-rows"
chmod +x "$AC_TASKLIST" "$AC_PROCESSES"
checks=0
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok() { checks=$((checks + 1)); }
has() { grep -Fq -- "$1" <<< "$output" || fail "missing '$1': $output"; ok; }
run() { set +e; output=$(bash "$AC_ROOT/scripts/ac-update.sh" "$@" 2>&1); code=$?; set -e; }
expect() { [ "$code" -eq "$1" ] || fail "exit $code, expected $1: $output"; ok; }
g() { git -c core.safecrlf=false -C "$1" "${@:2}"; }
head() { g "$1" rev-parse HEAD; }
revision() { g "$module" config --local leeroy.subtreeRevision; }
clean_temp() {
    [ -z "$(find "$TMPDIR" -mindepth 1 -maxdepth 1 -name 'leeroy-subtree.*')" ] || fail "temporary checkout leaked"; ok
    [ "$(g "$module" worktree list --porcelain | grep -c '^worktree ')" = 1 ] || fail "worktree registration leaked"; ok
}

core_up="$scratch/core upstream"
git init -q --initial-branch=main "$core_up"
printf 'core\n' > "$core_up/README.md"
g "$core_up" add -A; g "$core_up" commit -qm initial
git clone -q "$core_up" "$AC_ROOT/azerothcore-wotlk"
up="$scratch/module upstream"
prefix='server-side/module with spaces'
git init -q --initial-branch=main "$up"
mkdir -p "$up/$prefix/src" "$up/client-side"
printf 'int x = 1;\n' > "$up/$prefix/src/x.cpp"
printf 'outside\n' > "$up/client-side/map.dbc"
g "$up" add -A; g "$up" commit -qm initial
initial=$(head "$up")
module="$AC_ROOT/azerothcore-wotlk/modules/mod-nested"
mkdir -p "$module"
cp -R "$up/$prefix/". "$module/"
printf 'local\n' > "$module/local.txt"
# A process snapshot without its end sentinel must still refuse the swap.
printf 'incomplete\n' > "$scratch/server-rows"
if output=$(bash "$scripts/ac-module-to-git.sh" --go --subdir "$prefix" mod-nested "$up" "$initial" 2>&1); then
    fail 'unreadable process snapshot permitted conversion'
fi
has 'cannot read the server processes'
[ ! -e "$module/.git" ] || fail 'refused conversion changed the module'; ok
printf 'end\n' > "$scratch/server-rows"
output=$(bash "$scripts/ac-module-to-git.sh" --go --subdir "$prefix" mod-nested "$up" "$initial" 2>&1)
has 'ac-update.sh follows origin/main'
g "$module" branch -m local-work
# A similarly named tag must never shadow the configured remote-tracking branch.
g "$module" tag origin/main "$initial"
before=$(head "$module"); before_revision=$(revision)

# Dry-run computes a real split, but changes neither HEAD, metadata nor rollback record.
printf 'int y = 2;\n' > "$up/$prefix/src/y.cpp"
printf 'more outside\n' >> "$up/client-side/map.dbc"
g "$up" add -A; g "$up" commit -qm update
run --dry-run; expect 0; has 'subtree: 1 upstream commits, 1 local commits'
[ "$(head "$module")" = "$before" ] && [ "$(revision)" = "$before_revision" ] || fail "dry-run changed module"; ok
[ ! -e "$AC_ROOT/scripts/.last-update" ] || fail "dry-run wrote rollback state"; ok
[ ! -e "$AC_ROOT/scripts/.last-update.subtrees" ] || fail "dry-run wrote subtree rollback state"; ok
clean_temp

# Rebase local edits onto only the extracted folder, following origin/main even on local-work.
run; expect 0
[ -f "$module/src/y.cpp" ] && [ -f "$module/local.txt" ] || fail "upstream or local edit missing"; ok
[ ! -e "$module/client-side" ] && [ ! -e "$module/server-side" ] || fail "upstream root leaked into module"; ok
[ "$(g "$module" branch --show-current)" = local-work ] || fail "branch changed"; ok
grep -Fq "$module $before" "$AC_ROOT/scripts/.last-update" || fail "rollback HEAD missing"; ok
[ "${initial}" != "$(revision | cut -d' ' -f1)" ] || fail "source revision did not advance"; ok
clean_temp
after=$(head "$module")
# Rollback restores metadata as well as files, so the next update can still use the recorded base.
after_revision=$(revision)
cp "$AC_ROOT/scripts/.last-update.subtrees" "$scratch/original-subtree-rollback"
g "$module" config --local leeroy.subtreeRevision "$before_revision"
run; expect 0
[ "$(head "$module")" = "$after" ] && [ "$(revision)" = "$after_revision" ] \
    || fail "interrupted metadata checkpoint replayed applied upstream commits"; ok
# That retry overwrote the current rollback points. The operator kept the old pair.
g "$module" reset --hard --quiet "$before"
bash "$scratch/original-subtree-rollback"
[ "$(revision)" = "$before_revision" ] || fail "subtree rollback metadata was not restored"; ok
run; expect 0
[ "$(g "$module" rev-parse 'HEAD^{tree}')" = "$(g "$module" rev-parse "$after^{tree}")" ] \
    && [ "$(revision)" = "$after_revision" ] || fail "update after rollback changed the result"; ok
after=$(head "$module")
run; expect 0; has 'subtree: 0 upstream commits, 1 local commits'
[ "$(head "$module")" = "$after" ] || fail "repeat update rewrote local commit"; ok

# Outside-only upstream changes move the source checkpoint without rebasing the module.
printf 'docs\n' > "$up/README.md"; g "$up" add -A; g "$up" commit -qm docs
run; expect 0
[ "$(head "$module")" = "$after" ] || fail "outside-only change rewrote module"; ok
[ "$(revision | cut -d' ' -f1)" = "$(head "$up")" ] || fail "source checkpoint did not advance"; ok

# Interrupted checkpoint: upstream advances x=1 -> x=2 -> x=3 while metadata still
# records x=1. The applied x=2 commit is upstream history, never a local patch.
old_revision=$(revision)
printf 'int x = 2;\n' > "$up/$prefix/src/x.cpp"; g "$up" commit -qam x-two
run; expect 0
[ "$(cat "$module/src/x.cpp")" = 'int x = 2;' ] || fail "intermediate upstream change missing"; ok
intermediate=$(head "$module")
g "$module" config --local leeroy.subtreeRevision "$old_revision"
printf 'int x = 3;\n' > "$up/$prefix/src/x.cpp"; g "$up" commit -qam x-three
run --dry-run; expect 0; has 'subtree: 1 upstream commits, 1 local commits'
[ "$(head "$module")" = "$intermediate" ] && [ "$(revision)" = "$old_revision" ] \
    || fail "intermediate-base dry-run changed checkpoint"; ok
run; expect 0
[ "$(cat "$module/src/x.cpp")" = 'int x = 3;' ] && [ -f "$module/local.txt" ] \
    || fail "recovery lost upstream or local content"; ok
base=$(revision | cut -d' ' -f2)
[ "$(g "$module" rev-list --count "$base..HEAD")" = 1 ] \
    || fail "recovery replayed upstream commits as local edits"; ok

# Recreating merge topology can silently drop merge-only edits. Refuse both dry
# and real updates without changing the local merge, its files or metadata.
linear_head=$(head "$module")
linear_revision=$(revision)
g "$module" checkout -qb local-side
printf 'side\n' > "$module/side.txt"; g "$module" add -A; g "$module" commit -qm side
g "$module" checkout -q local-work
printf 'top\n' > "$module/top.txt"; g "$module" add -A; g "$module" commit -qm top
g "$module" merge --no-ff -qm merge-side local-side
printf 'merge-only\n' > "$module/merge-fix.txt"
g "$module" add merge-fix.txt; g "$module" commit --amend --no-edit -q
merged_head=$(head "$module")
merged_tree=$(g "$module" rev-parse 'HEAD^{tree}')
printf 'int z = 3;\n' > "$up/$prefix/src/z.cpp"; g "$up" add -A; g "$up" commit -qm z
run --dry-run; expect 1; has 'local subtree merges require manual updating'
run; expect 1; has 'local subtree merges require manual updating'
[ "$(head "$module")" = "$merged_head" ] && [ "$(g "$module" rev-parse 'HEAD^{tree}')" = "$merged_tree" ] \
    && [ "$(revision)" = "$linear_revision" ] || fail "merge refusal changed checkpoint"; ok
[ "$(cat "$module/merge-fix.txt")" = merge-only ] && [ -f "$module/side.txt" ] && [ -f "$module/top.txt" ] \
    || fail "merge refusal lost local content"; ok
[ -z "$(g "$module" status --porcelain)" ] || fail "merge refusal left dirty files"; ok
clean_temp
# Return this synthetic fixture to its saved linear branch for later scenarios.
g "$module" reset --hard --quiet "$linear_head"
run; expect 0
[ -f "$module/src/z.cpp" ] || fail "linear update did not proceed"; ok

# Conflicts abort back to the exact local HEAD; metadata and rollback evidence remain useful.
printf 'int x = 30;\n' > "$module/src/x.cpp"; g "$module" commit -qam local-x
before=$(head "$module"); before_revision=$(revision)
printf 'int x = 40;\n' > "$up/$prefix/src/x.cpp"; g "$up" commit -qam upstream-x
run; expect 1; has 'subtree conflict; original checkout restored'
[ "$(head "$module")" = "$before" ] && [ "$(revision)" = "$before_revision" ] || fail "failed rebase changed checkpoint"; ok
[ -z "$(g "$module" status --porcelain)" ] || fail "failed rebase left dirty files"; ok
clean_temp

# Dirty files and malformed metadata refuse before any checkout advances.
printf 'dirty\n' >> "$module/local.txt"
run; expect 1; has 'refusing to update with local modifications'
g "$module" restore local.txt
g "$module" config leeroy.subtreePrefix ../outside
run; expect 1; has 'invalid subtree prefix'
g "$module" config leeroy.subtreePrefix "$prefix"
g "$module" config --unset leeroy.subtreeRevision
run; expect 1; has 'incomplete subtree metadata'
g "$module" config leeroy.subtreeRevision "$before_revision"
[ "$(head "$module")" = "$before" ] || fail "refusal changed module"; ok

# An upstream deletion cannot record a nonexistent prefix as the new checkpoint.
g "$up" rm -qr -- "$prefix"; g "$up" commit -qm remove-module
run; expect 1; has 'upstream no longer contains the subtree folder'
[ "$(head "$module")" = "$before" ] && [ "$(revision)" = "$before_revision" ] || fail "removed folder changed module"; ok

# Rewritten remote history cannot silently replace the split's ancestry.
g "$up" checkout -q --orphan replacement
g "$up" add -A; g "$up" commit -qm rewritten
g "$up" branch -M main
run; expect 1
[ "$(head "$module")" = "$before" ] && [ "$(revision)" = "$before_revision" ] || fail "remote rewrite changed module"; ok
clean_temp
printf 'ac-update-subtree: %s checks passed\n' "$checks"
