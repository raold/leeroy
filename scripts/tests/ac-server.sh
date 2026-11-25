#!/usr/bin/env bash
# A scratch install, a stub process list and stub Windows tools only; never a real server.
# Stub PIDs are odd: Windows PIDs are multiples of 4, so no real process can match one.
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
temp_root=$(cd "${TMPDIR:-/tmp}" && pwd -P)
scratch=$(mktemp -d "$temp_root/leeroy-server.XXXXXX")
case "$scratch" in "$temp_root"/leeroy-server.*) ;; *) exit 1 ;; esac
trap 'rm -rf -- "$scratch"' EXIT

export AC_ROOT="$scratch/install with spaces" AC_SERVER_WINDOW=1
export LEEROY_MIND_DATA="$scratch/mind-data" AC_PROCESSES="$scratch/stubs/processes"
export ROWS="$scratch/rows" ACTIONS="$scratch/actions" STUBBORN="$scratch/stubborn" UNREADABLE="$scratch/unreadable"
bin="$AC_ROOT/build/bin/RelWithDebInfo"
mkdir -p "$bin" "$scratch/stubs" "$LEEROY_MIND_DATA"
for p in authserver worldserver; do printf 'binary\n' > "$bin/$p.exe"; chmod +x "$bin/$p.exe"; done
STUB_LIVE=$bin
if command -v cygpath >/dev/null 2>&1; then STUB_LIVE=$(cygpath -w "$bin"); fi
export STUB_LIVE
dojo='F:\azerothcore\dojo-runtime\instances\claude-raids\bin'

cat > "$scratch/stubs/processes" <<'EOF'
#!/usr/bin/env bash
[ ! -e "$UNREADABLE" ] || exit 1
cat "$ROWS"
printf 'end\r\n'
EOF
# taskkill //PID n ends n unless n is stubborn (then only //F does); never by image name.
cat > "$scratch/stubs/taskkill" <<'EOF'
#!/usr/bin/env bash
printf 'taskkill %s\n' "$*" >> "$ACTIONS"
force=0 pid=''
while [ $# -gt 0 ]; do
    case "$1" in //F) force=1 ;; //PID) pid=$2; shift ;; *) exit 1 ;; esac
    shift
done
[ -n "$pid" ] || exit 1
if [ "$force" = 0 ] && grep -qx "$pid" "$STUBBORN" 2>/dev/null; then exit 0; fi
grep -v "^$pid"$'\t' "$ROWS" > "$ROWS.tmp" || true
mv "$ROWS.tmp" "$ROWS"
EOF
# ac-server.sh launches each server with: cmd //c start <title> <image>, from $AC_BIN.
cat > "$scratch/stubs/cmd" <<'EOF'
#!/usr/bin/env bash
printf 'cmd %s\n' "$*" >> "$ACTIONS"
if [ "${1:-}" = //c ] && [ "${2:-}" = start ]; then
    n=$(wc -l < "$ROWS")
    printf '%s\t%s\t%s\\%s\r\n' "$((3001 + 2 * n))" "$4" "$STUB_LIVE" "$4" >> "$ROWS"
fi
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$scratch/stubs/sleep"
chmod +x "$scratch/stubs/"*
export PATH="$scratch/stubs:$PATH"

checks=0
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok() { checks=$((checks + 1)); }
for tool in taskkill cmd sleep; do
    [ "$(command -v "$tool")" = "$scratch/stubs/$tool" ] || fail "the $tool stub does not come first on PATH"
done
has() { grep -Fq -- "$1" <<< "$output" || fail "missing '$1' in: $output"; ok; }
lacks() { ! grep -Fq -- "$1" <<< "$output" || fail "unexpected '$1' in: $output"; ok; }
did() { grep -Fqx -- "$1" "$ACTIONS" || fail "no action '$1' in: $(cat "$ACTIONS")"; ok; }
row_left() { grep -q "^$1"$'\t' "$ROWS" || fail "process $1 was stopped"; ok; }
row_gone() { ! grep -q "^$1"$'\t' "$ROWS" || fail "process $1 still runs"; ok; }
run() { set +e; output=$(bash "$scripts/ac-server.sh" "$@" 2>&1); code=$?; set -e; }
expect() { [ "$code" -eq "$1" ] || fail "exit $code, wanted $1: $output"; ok; }
# rows PID:IMAGE:PATH... sets the process list afresh; clears the action log and stubbornness.
rows() {
    local spec pid image path
    : > "$ROWS"; : > "$ACTIONS"; rm -f "$STUBBORN" "$UNREADABLE"
    for spec in "$@"; do
        IFS=: read -r pid image path <<< "$spec"
        printf '%s\t%s\t%s\r\n' "$pid" "$image" "$path" >> "$ROWS"
    done
}
live_auth="1001:authserver.exe:$STUB_LIVE\\authserver.exe"
live_world="1003:worldserver.exe:$STUB_LIVE\\worldserver.exe"
dojo_world="2001:worldserver.exe:$dojo\\worldserver.exe"
dojo_auth="2003:authserver.exe:$dojo\\authserver.exe"

# --- status: only this install's servers count ---------------------------------
# The case and slashes Windows reports may differ from AC_BIN's.
shouting="1003:WORLDSERVER.EXE:${STUB_LIVE^^}/WORLDSERVER.EXE"
rows "$live_auth" "$shouting" "$dojo_world"
run status; expect 0
has 'authserver.exe     running (pid 1001)'
has 'worldserver.exe    running (pid 1003); 1 running from elsewhere'
lacks 2001
rows "$dojo_world" "$dojo_auth"
run status; expect 0
has 'authserver.exe     stopped (1 running from elsewhere)'
has 'worldserver.exe    stopped (1 running from elsewhere)'
rows "1005:worldserver.exe:"
run status; expect 0
has 'worldserver.exe    stopped (1 whose path cannot be read)'
rows; touch "$UNREADABLE"
run status; expect 0
has 'worldserver.exe    unknown: the process list cannot be read'

# --- start: a dojo's worldserver does not stand in for the live one (2026-11-24) ---
rows "$dojo_world"
run start; expect 0
has 'starting authserver'
has 'starting worldserver (1 running from elsewhere, left alone)'
lacks 'already running'
did 'cmd //c start authserver authserver.exe'
did 'cmd //c start worldserver worldserver.exe'
row_left 2001
[ "$(grep -c "$(printf '\t')" "$ROWS")" = 3 ] || fail "start did not launch both live servers: $(cat "$ROWS")"; ok

rows "$live_auth" "$live_world" "$dojo_world"
run start; expect 0
has 'authserver already running (pid 1001)'
has 'worldserver already running (pid 1003)'
lacks 'cmd '
[ ! -s "$ACTIONS" ] || fail "start acted on running servers: $(cat "$ACTIONS")"; ok

rows "$live_auth" "1005:worldserver.exe:"
run start; expect 1
has 'worldserver not started: 1 worldserver.exe whose path cannot be read'
has 'servers did not start; mind not started'
[ ! -s "$ACTIONS" ] || fail "start launched beside an unreadable server: $(cat "$ACTIONS")"; ok

rows "$dojo_world"; touch "$UNREADABLE"
run start; expect 1
has 'the process list cannot be read; authserver not started'
[ ! -s "$ACTIONS" ] || fail "start launched without a process list: $(cat "$ACTIONS")"; ok

# --- stop: by PID, and never a dojo's server -----------------------------------
rows "$live_auth" "$live_world" "$dojo_world" "$dojo_auth"
run stop; expect 0
did 'taskkill //PID 1003'
did 'taskkill //PID 1001'
has 'worldserver.exe exited on request'
has 'authserver.exe exited on request'
has 'worldserver.exe: 1 running from elsewhere, left alone'
has 'authserver.exe: 1 running from elsewhere, left alone'
row_gone 1001; row_gone 1003; row_left 2001; row_left 2003
lacks 'terminating'
! grep -q '//IM\|2001\|2003' "$ACTIONS" || fail "stop touched a dojo server or used an image name: $(cat "$ACTIONS")"; ok

# A live worldserver that ignores the request is terminated, by its PID only.
rows "$live_world" "$dojo_world"; printf '1003\n' > "$STUBBORN"
run stop; expect 0
did 'taskkill //PID 1003'
did 'taskkill //F //PID 1003'
has 'terminating worldserver.exe (pid 1003)'
row_gone 1003; row_left 2001
! grep -q '2001' "$ACTIONS" || fail "stop touched the dojo server: $(cat "$ACTIONS")"; ok

rows "$dojo_world" "$dojo_auth" "1005:worldserver.exe:"
run stop; expect 0
has 'worldserver.exe    already stopped'
has 'authserver.exe     already stopped'
has 'worldserver.exe: 1 running from elsewhere, 1 whose path cannot be read, left alone'
[ ! -s "$ACTIONS" ] || fail "stop acted with none of this install's servers running: $(cat "$ACTIONS")"; ok

rows "$live_world"; touch "$UNREADABLE"
run stop; expect 0
has 'worldserver.exe not stopped: the process list cannot be read'
[ ! -s "$ACTIONS" ] || fail "stop acted without a process list: $(cat "$ACTIONS")"; ok

printf 'ok: %s server checks\n' "$checks"
