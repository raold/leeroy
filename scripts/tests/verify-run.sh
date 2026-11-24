#!/usr/bin/env bash
# The verify runner against temporary repositories, a stub mind and stub module checkouts only;
# never the installed core, its build tree or a live database.
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
runner="$scripts/verify/run.sh"
temp_root=$(cd "${TMPDIR:-/tmp}" && pwd -P)
scratch=$(mktemp -d "$temp_root/leeroy-verify-test.XXXXXX")
case "$scratch" in "$temp_root"/leeroy-verify-test.*) ;; *) exit 1 ;; esac
trap 'rm -rf -- "$scratch"' EXIT

export GIT_AUTHOR_NAME=tester GIT_AUTHOR_EMAIL=tester@example.invalid
export GIT_COMMITTER_NAME=tester GIT_COMMITTER_EMAIL=tester@example.invalid
export TMPDIR="$scratch/tmp"
mkdir -p "$TMPDIR"
unset VERIFY_LIVE_ROOT VERIFY_MIND_DB VERIFY_STRICT_WORKFLOWS LEEROY_MIND_DATA GITHUB_ACTIONS

python=""
for candidate in "${VERIFY_TEST_PYTHON:-}" python3 python; do
    [[ -n $candidate ]] && "$candidate" -c 'import sqlite3' >/dev/null 2>&1 && { python="$candidate"; break; }
done
[[ -n $python ]] || { printf 'FAIL: no python with sqlite3\n' >&2; exit 1; }
export VERIFY_PYTHON="$python"

checks=0
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok() { checks=$((checks + 1)); }
has() { grep -Fq -- "$1" <<< "$output" || fail "missing '$1' in: $output"; ok; }
lacks() { ! grep -Fq -- "$1" <<< "$output" || fail "unexpected '$1' in: $output"; ok; }
run() { set +e; output=$(cd "$leeroy" && bash "$runner" "$@" 2>&1); code=$?; set -e; }
expect() { [ "$code" -eq "$1" ] || fail "exit $code, wanted $1: $output"; ok; }
g() { git -c core.autocrlf=false -c core.safecrlf=false -C "$1" "${@:2}"; }
commit() { g "$1" add -A && g "$1" commit --quiet -m "$2"; }
leftovers() { [ -z "$(ls -A "$TMPDIR")" ] || fail "left behind in TMPDIR: $(ls -A "$TMPDIR")"; ok; }

# --- the leeroy repository: main with a stub mind, a module and a patch series --------------------
leeroy="$scratch/leeroy"
git init --quiet --initial-branch=main "$leeroy"
mkdir -p "$leeroy/scripts" "$leeroy/modules/mod-leeroy/src/Lib" "$leeroy/modules/mod-leeroy/src/Guild" \
    "$leeroy/modules/mod-leeroy/tests" "$leeroy/mind/leeroy_mind/storage/migrations" "$leeroy/mind/leeroy_mind/guild" \
    "$leeroy/mind/tests" "$leeroy/.github/workflows" "$leeroy/patches/mod-playerbots"
cp "$scripts/ac-release-patches.py" "$leeroy/scripts/"
printf 'name: ci\n' > "$leeroy/.github/workflows/ci.yml"
printf 'readme\n' > "$leeroy/README.md"
printf 'struct Facts\n{\n    int far = 0;  // already there: only added lines count\n};\n' > "$leeroy/modules/mod-leeroy/src/Lib/LeeroyFacts.h"
printf '#include "LeeroyFacts.h"\nint Pure() { return 1; }\n' > "$leeroy/modules/mod-leeroy/src/Lib/LeeroyFacts.cpp"
printf '#include "LeeroyFacts.h"\nint Game() { return 2; }\n' > "$leeroy/modules/mod-leeroy/src/Guild/FactsJob.cpp"
printf '' > "$leeroy/mind/leeroy_mind/__init__.py"
printf '' > "$leeroy/mind/leeroy_mind/storage/__init__.py"
printf 'x = 1\n' > "$leeroy/mind/leeroy_mind/guild/loot.py"
cat > "$leeroy/mind/leeroy_mind/storage/db.py" <<'EOF'
import sqlite3
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"


def open_db(path):
    conn = sqlite3.connect(path)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for script in sorted(MIGRATIONS.glob("*.sql")):
        number = int(script.name[:4])
        if number > current:
            conn.executescript(script.read_text(encoding="utf-8"))
            conn.execute(f"PRAGMA user_version = {number}")
            conn.commit()
    return conn
EOF
cat > "$leeroy/mind/leeroy_mind/__main__.py" <<'EOF'
import sys
from pathlib import Path

from leeroy_mind.storage.db import open_db

args = sys.argv[1:]
assert args[0] == "db" and args[-1] == "migrate", args
data = Path(args[args.index("--data-dir") + 1])
open_db(data / "mind.db").close()
print("mind.db migrated")
EOF
printf 'CREATE TABLE memories (id INTEGER PRIMARY KEY, body TEXT);\n' > "$leeroy/mind/leeroy_mind/storage/migrations/0001_init.sql"
printf 'CREATE TABLE notes (id INTEGER PRIMARY KEY);\n' > "$leeroy/mind/leeroy_mind/storage/migrations/0002_notes.sql"
commit "$leeroy" base

# --- the live install: a module checkout with one local patch, and a mind.db -----------------------
live="$scratch/live"
pb="$live/azerothcore-wotlk/modules/mod-playerbots"
git init --quiet --initial-branch=master "$pb"
mkdir -p "$pb/src/Bot"
cat > "$pb/src/Bot/RandomPlayerbotMgr.cpp" <<'EOF'
bool RandomPlayerbotMgr::ProcessBot(Player* bot)
{
    uint32 botId = bot->GetGUID().GetCounter();
    if (bot->isDead())
        return false;
    // leave group if leader is rndbot
    return true;
}
EOF
printf 'one\n' > "$pb/src/Bot/Notes.txt"
commit "$pb" upstream
git clone --quiet --bare "$pb" "$scratch/upstream-playerbots.git"
sed -i 's|^    if (bot->isDead())|    if (IsPinned(botId))\n        return ProcessPinnedBot(bot);\n    if (bot->isDead())|' "$pb/src/Bot/RandomPlayerbotMgr.cpp"
commit "$pb" "Let other modules pin random bots"
g "$pb" format-patch --quiet -1 -o "$leeroy/patches/mod-playerbots" HEAD
commit "$leeroy" "Snapshot the local playerbots patch"

# Later patches are written in another clone, as a branch's would be.
work="$scratch/playerbots-work"
git clone --quiet "$pb" "$work"
new_patch() {  # new_patch NAME: commit the edit already made in $work as series entry 0002
    commit "$work" "$1"
    rm -f "$leeroy"/patches/mod-playerbots/0002-*.patch
    g "$work" format-patch --quiet --start-number 2 -1 -o "$leeroy/patches/mod-playerbots" HEAD
    g "$work" reset --quiet --hard HEAD~1
}

mkdir -p "$live/mind-data"
"$python" - "$live/mind-data/mind.db" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.executescript("CREATE TABLE memories (id INTEGER PRIMARY KEY, body TEXT);"
                   "CREATE TABLE notes (id INTEGER PRIMARY KEY);"
                   "INSERT INTO memories (body) VALUES ('private text that must never print');"
                   "PRAGMA user_version = 2;")
conn.commit()
conn.close()
EOF
export VERIFY_LIVE_ROOT="$live"
pb_head=$(g "$pb" rev-parse HEAD)
db_sum=$(cksum < "$live/mind-data/mind.db")

# --- plan: each kind of change selects its checks, unrelated paths none --------------------------
g "$leeroy" checkout --quiet -b plan
printf 'docs\n' >> "$leeroy/README.md"
commit "$leeroy" docs
run --plan main; expect 0; has 'checks: none'
run main; expect 0; has 'checks: none'
printf 'name: ci2\n' > "$leeroy/.github/workflows/ci.yml"
printf 'int More() { return 3; }\n' >> "$leeroy/modules/mod-leeroy/src/Guild/FactsJob.cpp"
printf 'CREATE TABLE x (id INTEGER);\n' > "$leeroy/mind/leeroy_mind/storage/migrations/0003_x.sql"
printf 'x = 2\n' > "$leeroy/mind/leeroy_mind/guild/loot.py"
printf 'note\n' > "$leeroy/patches/mod-playerbots/NOTES.md"
commit "$leeroy" everything
run --plan main; expect 0
has 'checks: workflows identifiers numbering patches contract msvc migrate'
run --plan --only identifiers,migrate main; expect 0; has 'checks: identifiers migrate'
run --plan no-such-ref; expect 2; has 'unknown base no-such-ref'
g "$leeroy" checkout --quiet main

# --- workflows: alone is fine; with code a warning, a FAIL when strict ---------------------------
g "$leeroy" checkout --quiet -b workflows
printf 'name: ci3\n' > "$leeroy/.github/workflows/ci.yml"
commit "$leeroy" workflow
run main; expect 0; has 'ok   workflows: only workflow files changed'
printf 'more\n' >> "$leeroy/README.md"
commit "$leeroy" "workflow and docs"
run main; expect 0; has 'warn workflows: 1 workflow file(s) changed together with 1 other path(s)'
VERIFY_STRICT_WORKFLOWS=1 run main; expect 1; has 'FAIL workflows:'
GITHUB_ACTIONS=true run --ci main; expect 0; has '::warning title=verify workflows::'
g "$leeroy" checkout --quiet main

# --- identifiers: added code naming a windows.h macro, never comments, strings or old lines -------
g "$leeroy" checkout --quiet -b identifiers
cat >> "$leeroy/modules/mod-leeroy/src/Lib/LeeroyFacts.h" <<'EOF'
// keep the bots near the boss, far from the interface
inline char const* kNote = "near far small IN OUT";
EOF
printf 'bool near = true;\n' > "$leeroy/modules/mod-leeroy/tests/TestFacts.cpp"
commit "$leeroy" "prose and a test"
run --only identifiers main; expect 0; has 'ok   identifiers: scanned 1 changed C++ file(s), 0 finding(s)'
printf 'struct More\n{\n    bool near = false;\n    int DELETE = 0;\n};\n' >> "$leeroy/modules/mod-leeroy/src/Lib/LeeroyFacts.h"
commit "$leeroy" "a field named near"
run --only identifiers main; expect 1
has 'FAIL identifiers: scanned 1 changed C++ file(s), 2 finding(s)'
has "modules/mod-leeroy/src/Lib/LeeroyFacts.h:9: 'near' is a <windows.h> macro under MSVC"
has "modules/mod-leeroy/src/Lib/LeeroyFacts.h:10: 'DELETE' is a <windows.h> macro under MSVC"
lacks ':3:'
# msvc: no Windows build tree here, so it says why it did not run.
run --only msvc main; expect 0; has "skip msvc: no live build tree at $live/build"
run --ci --only msvc main; expect 0; has 'skip msvc: needs Windows and the live build tree'
g "$leeroy" checkout --quiet main

# msvc's choice of translation units: a header compiles through its own .cpp and a game TU.
output=$("$python" "$scripts/verify/msvc_syntax.py" --vcxproj none --live-module none \
    --module "$leeroy/modules/mod-leeroy" --plan modules/mod-leeroy/src/Lib/LeeroyFacts.h | tr -d '\r')
has 'plan msvc modules/mod-leeroy/src/Lib/LeeroyFacts.cpp'
has 'plan msvc modules/mod-leeroy/src/Guild/FactsJob.cpp'
# ... and its cl arguments: the live project's settings with mod-leeroy swapped for the checkout.
cat > "$scratch/modules.vcxproj" <<'EOF'
<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemDefinitionGroup Condition="'$(Configuration)|$(Platform)'=='Debug|x64'">
    <ClCompile><PreprocessorDefinitions>WRONG_CONFIG</PreprocessorDefinitions></ClCompile>
  </ItemDefinitionGroup>
  <ItemDefinitionGroup Condition="'$(Configuration)|$(Platform)'=='RelWithDebInfo|x64'">
    <ClCompile>
      <AdditionalIncludeDirectories>L:\core\modules\mod-leeroy\src;L:\core\modules\mod-leeroy\src\Lib;L:\core\src\game;%(AdditionalIncludeDirectories)</AdditionalIncludeDirectories>
      <AdditionalOptions>%(AdditionalOptions) /external:I "L:/core/modules/mod-leeroy/deps" /external:I "C:/Program Files/MySQL/include" /utf-8</AdditionalOptions>
      <LanguageStandard>stdcpp20</LanguageStandard>
      <UseStandardPreprocessor>true</UseStandardPreprocessor>
      <TreatSpecificWarningsAsErrors>4263;4264</TreatSpecificWarningsAsErrors>
      <PreprocessorDefinitions>%(PreprocessorDefinitions);WIN32;_CONF_DIR="C:/Program Files (x86)/AzerothCore";NOMINMAX</PreprocessorDefinitions>
    </ClCompile>
  </ItemDefinitionGroup>
</Project>
EOF
output=$(cd "$scripts/verify" && "$python" -c 'import sys
from pathlib import Path
import msvc_syntax as m
args = m.options(m.settings(Path(sys.argv[1])), sys.argv[2], sys.argv[3])
print("\n".join(args)); print("RSP " + " ".join(m.response_line(a) for a in args))' \
    "$scratch/modules.vcxproj" 'L:\core\modules\mod-leeroy' checkout/mod-leeroy | tr -d '\r')
has '/std:c++20'
has '/Zc:preprocessor'
has '/we4263'
has '/DNOMINMAX'
has '/IL:\core\src\game'
has '/Icheckout'
has '/external:Icheckout'
has '/external:IC:/Program Files/MySQL/include'
has '"/D_CONF_DIR=\"C:/Program Files (x86)/AzerothCore\""'
lacks 'WRONG_CONFIG'
lacks '/IL:\core\modules\mod-leeroy'

# --- numbering: a branch's migration against the merge target's --------------------------------
g "$leeroy" checkout --quiet -b numbering
printf 'CREATE TABLE three (id INTEGER);\n' > "$leeroy/mind/leeroy_mind/storage/migrations/0003_three.sql"
commit "$leeroy" "migration 0003"
run --only numbering main; expect 0; has 'ok   numbering: 1 new migration(s), merged numbering 0001-0003 without a hole'
g "$leeroy" checkout --quiet main
g "$leeroy" checkout --quiet -b main-advanced
printf 'CREATE TABLE other (id INTEGER);\n' > "$leeroy/mind/leeroy_mind/storage/migrations/0003_other.sql"
commit "$leeroy" "main's own 0003"
g "$leeroy" checkout --quiet numbering
run --only numbering main-advanced; expect 1; has 'FAIL numbering: migration number taken on the merge target'
has '0003 (0003_other.sql / 0003_three.sql)'
git -C "$leeroy" mv mind/leeroy_mind/storage/migrations/0003_three.sql mind/leeroy_mind/storage/migrations/0005_three.sql
commit "$leeroy" "renumber past a gap"
run --only numbering main-advanced; expect 1; has 'merged numbering skips 0004'
g "$leeroy" checkout --quiet main

# --- migrate: the branch's code on a copy of the live database, the source untouched -------------
g "$leeroy" checkout --quiet -b migrate
printf 'ALTER TABLE notes ADD COLUMN at INTEGER;\n' > "$leeroy/mind/leeroy_mind/storage/migrations/0003_at.sql"
commit "$leeroy" "migration 0003"
run --only numbering,migrate main; expect 0
has 'ok   numbering: 1 new migration(s)'
has 'ok   migrate: live mind.db copy (0.0 GB) v2 -> v3, integrity ok'
lacks 'private text'
[ "$(cksum < "$live/mind-data/mind.db")" = "$db_sum" ] || fail "the live database changed"; ok
[ ! -e "$live/mind-data/snapshots" ] || fail "migrate wrote beside the live database"; ok
leftovers
run --ci --only migrate main; expect 0; has 'ok   migrate: fixture at the base schema v2 -> v3, integrity ok'
leftovers
printf 'ALTER TABLE nowhere ADD COLUMN x INTEGER;\n' > "$leeroy/mind/leeroy_mind/storage/migrations/0004_broken.sql"
commit "$leeroy" "a broken migration"
run --only migrate main; expect 1; has 'FAIL migrate: db migrate exited 1: sqlite3.OperationalError: no such table: nowhere'
[ "$(cksum < "$live/mind-data/mind.db")" = "$db_sum" ] || fail "the live database changed"; ok
leftovers
VERIFY_MIND_DB="$scratch/missing.db" run --only migrate main; expect 0; has 'skip migrate: no mind.db at'
g "$leeroy" checkout --quiet main

# --- contract: whatever -k contract selects, through a stub interpreter --------------------------
g "$leeroy" checkout --quiet -b contract
printf 'x = 3\n' > "$leeroy/mind/leeroy_mind/guild/loot.py"
commit "$leeroy" "guild command code"
cat > "$scratch/stub-python" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    "-c import pytest") exit "${STUB_IMPORT:-0}" ;;
    "-m pytest -q -k contract -p no:cacheprovider tests") printf '%s\n' "$STUB_OUT"; exit "${STUB_RC:-0}" ;;
esac
exit 3
EOF
chmod +x "$scratch/stub-python"
STUB_OUT="3 passed in 0.1s" VERIFY_PYTHON="$scratch/stub-python" run --only contract main
expect 0; has 'ok   contract: 3 passed in 0.1s'
STUB_OUT="no tests ran" STUB_RC=5 VERIFY_PYTHON="$scratch/stub-python" run --only contract main
expect 0; has 'skip contract: no test matches -k contract'
STUB_OUT=$'FAILED tests/test_wire.py::test_contract_give_loot\n1 failed in 0.2s' STUB_RC=1 VERIFY_PYTHON="$scratch/stub-python" run --only contract main
expect 1; has 'FAIL contract: 1 failed in 0.2s'; has 'FAILED tests/test_wire.py::test_contract_give_loot'
STUB_IMPORT=1 VERIFY_PYTHON="$scratch/stub-python" run --only contract main
expect 0; has 'skip contract: no pytest in'
g "$leeroy" checkout --quiet main

# --- patches: the entries the live checkout lacks, rehearsed in clones; the pin guard after -------
g "$leeroy" checkout --quiet -b patches
printf 'two\n' >> "$work/src/Bot/Notes.txt"
new_patch "Note a second thing"
commit "$leeroy" "a new series entry"
run --only patches main; expect 0
has 'ok   patches: 1 series entry the live checkouts lack apply in a rehearsal clone; pin guard OK'
[ "$(g "$pb" rev-parse HEAD)" = "$pb_head" ] && [ -z "$(g "$pb" status --porcelain)" ] || fail "the live checkout changed"; ok
leftovers
# Upstream mode applies the whole series to a fresh clone, as the nightly patches workflow does.
VERIFY_UPSTREAM_PLAYERBOTS="$scratch/upstream-playerbots.git" run --ci --only patches main; expect 0
has 'ok   patches: mod-playerbots: 2 patch(es) apply to upstream master; pin guard OK'
leftovers

sed -i 's|^    // leave group if leader is rndbot|    if (bot->GetGroup())\n        return false;\n    // leave group if leader is rndbot|' "$work/src/Bot/RandomPlayerbotMgr.cpp"
sed -i '/IsPinned(botId)/,+1d' "$work/src/Bot/RandomPlayerbotMgr.cpp"
printf '    if (IsPinned(botId))\n        return ProcessPinnedBot(bot);\n' >> "$work/src/Bot/RandomPlayerbotMgr.cpp"
new_patch "Move the pin guard last"
commit "$leeroy" "a series entry that moves the guard"
run --only patches main; expect 1; has 'FAIL patches: release rehearsal refused: pin_guard_misplaced'
VERIFY_UPSTREAM_PLAYERBOTS="$scratch/upstream-playerbots.git" run --ci --only patches main; expect 1
has 'FAIL patches: mod-playerbots: pin guard MISPLACED'

g "$work" checkout --quiet -b elsewhere HEAD~1
printf 'unrelated\n' > "$work/src/Bot/Notes.txt"
commit "$work" "Rewrite the notes"
printf 'four\n' >> "$work/src/Bot/Notes.txt"
new_patch "Extend notes nobody has"
commit "$leeroy" "a series entry that does not apply"
run --only patches main; expect 1
has 'FAIL patches: release rehearsal refused: mod-playerbots: 0002-Extend-notes-nobody-has.patch: patch_apply_failed'
[ "$(g "$pb" rev-parse HEAD)" = "$pb_head" ] && [ -z "$(g "$pb" status --porcelain)" ] || fail "the live checkout changed"; ok
leftovers
g "$leeroy" checkout --quiet main

printf 'verify runner self-test: %d checks passed\n' "$checks"
