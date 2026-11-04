#!/usr/bin/env bash
# Start, stop, or check worldserver, authserver, and the mind service.
#
# Usage: ac-server.sh start|stop|status
#
# NOTE ON STOPPING: worldserver is a console app. It does not act on WM_CLOSE,
# and this install has both remote-control channels disabled
# (Ra.Enable = 0, SOAP.Enabled = 0), so nothing can ask it to shut down from a
# script. "stop" therefore terminates it.
#
# The only clean shutdown is to type this into the worldserver console window:
#     server shutdown 30
# which saves players, then closes. Do that first if characters are online.
# Setting SOAP.Enabled = 1 would let this script do it properly.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

running() { tasklist //FI "IMAGENAME eq $1" 2>/dev/null | grep -qi "$1"; }

mind() {
    local action="$1" python="$AC_ROOT/mind/.venv/Scripts/python.exe"
    local data="${LEEROY_MIND_DATA:-$AC_ROOT/mind-data}"
    if [ ! -x "$python" ]; then
        if [ "$action" = stop ] && [ -e "$data/mind.lock" ]; then
            die "mind venv is unavailable but mind.lock exists; stop the mind manually first"
        fi
        warn "mind venv not installed; mind $action skipped"
        return 0
    fi
    PYTHONPATH="$(cygpath -w "$AC_ROOT/mind")" \
        "$python" "$AC_ROOT/scripts/ac-mind.py" "$action" \
        --root "$(cygpath -w "$AC_ROOT")" --data-dir "$(cygpath -w "$data")"
}

case "${1:-status}" in
status)
    for p in authserver.exe worldserver.exe; do
        if running "$p"; then printf '    %-18s running\n' "$p"; else printf '    %-18s stopped\n' "$p"; fi
    done
    mind status
    ;;
start)
    [ -x "$AC_BIN/worldserver.exe" ] || die "worldserver.exe not built. Run ./ac-build.sh"
    for p in authserver worldserver; do
        if running "$p.exe"; then
            warn "$p already running"
        else
            log "starting $p"
            ( cd "$AC_BIN" && cmd //c start "$p" "$p.exe" )
        fi
    done
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if running authserver.exe && running worldserver.exe; then break; fi
        sleep 1
    done
    if ! running authserver.exe || ! running worldserver.exe; then
        die "servers did not start; mind not started"
    fi
    mind start
    ;;
stop)
    # Stop only the exact mind instance first. An unknown held lock or failed stop
    # aborts here, before either server is touched. Like worldserver, this is forced.
    mind stop
    if running worldserver.exe; then
        warn "worldserver cannot be shut down cleanly from a script on this install."
        warn "If characters are online, type 'server shutdown 30' in its console first."
    fi
    # Try the polite request anyway -- costs 5s, and works if a handler is ever
    # added -- then terminate.
    for p in worldserver.exe authserver.exe; do
        running "$p" || { printf '    %-18s already stopped\n' "$p"; continue; }
        taskkill //IM "$p" >/dev/null 2>&1 || true
        for _ in 1 2 3 4 5; do running "$p" || break; sleep 1; done
        if running "$p"; then
            log "terminating $p"
            taskkill //F //IM "$p" >/dev/null 2>&1 || true
        else
            log "$p exited on request"
        fi
    done
    ;;
*)
    die "usage: $0 start|stop|status"
    ;;
esac
