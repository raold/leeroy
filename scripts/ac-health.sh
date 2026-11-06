#!/usr/bin/env bash
# Check the live install, read-only: the game servers, Server.log's cast line, the mind, its
# log and control endpoint, the console, the voice service, Ollama and the activity mind.db
# should show. One ok/FAIL/note line per check; exits 1 on any FAIL.
#
# Usage: ac-health.sh [--json] [--toast]
#
# Arguments go on to `python -m leeroy_mind health`. It never connects to the bridge (7878),
# loads no model and starts or stops nothing. --toast shows a Windows toast for a new failure
# and remembers it in health-state.json in the mind's data dir, the only file it writes.
# ac-health-schedule.ps1 installs a task that runs it every 15 minutes with --toast.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

python="$AC_ROOT/mind/.venv/Scripts/python.exe"
data="${LEEROY_MIND_DATA:-$AC_ROOT/mind-data}"
[ -x "$python" ] || die "mind venv not installed at $AC_ROOT/mind/.venv"

PYTHONPATH="$(cygpath -w "$AC_ROOT/mind")" exec "$python" -m leeroy_mind health \
    --root "$(cygpath -w "$AC_ROOT")" --data-dir "$(cygpath -w "$data")" "$@"
