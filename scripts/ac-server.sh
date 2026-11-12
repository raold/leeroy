#!/usr/bin/env bash
# Start, stop, or check worldserver, authserver, and the mind service.
#
# Usage: ac-server.sh start|stop|status
#
# STOPPING: worldserver is a console app that ignores WM_CLOSE. With SOAP on
# (SOAP.Enabled = 1 on 127.0.0.1) and the local credentials file that
# scripts/ac-soap.py reads (%LOCALAPPDATA%\leeroy\remote-console.cnf), "stop" asks it to
# `server shutdown`: it saves every character and exits, after AC_SHUTDOWN_DELAY seconds
# (default 30 while players are connected, else 1), and is given AC_SHUTDOWN_WAIT more
# (default 180) to finish. Without SOAP, or if it does not exit in time, it is terminated,
# keeping only what the server last saved; then type `server shutdown 30` in its console
# first if characters are online.
#
# "start" opens each server in a new tab of the most recently used Windows
# Terminal window (a new window if none is open). Without Windows Terminal, or
# with AC_SERVER_WINDOW=1, each gets its own console window as before.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

running() { tasklist //FI "IMAGENAME eq $1" 2>/dev/null | grep -qi "$1"; }

# The server runs under cmd so that a stopped one (taskkill exits it nonzero) still
# closes its tab: cmd itself exits 0 afterwards. A tab asked for from a background job can
# open nowhere while wt.exe still exits 0 (2026-11-06), so a server that is not running
# 15 s later gets its own console window instead.
launch() {
    local p="$1" wt
    wt=$(command -v wt.exe || true)
    if [ -n "$wt" ] && [ "${AC_SERVER_WINDOW:-0}" != 1 ]; then
        "$wt" -w 0 new-tab --title "$p" --suppressApplicationTitle -d "$(cygpath -w "$AC_BIN")" \
            cmd //c "$p.exe & exit 0" || true
        for _ in $(seq 1 15); do
            running "$p.exe" && return 0
            sleep 1
        done
        warn "Windows Terminal did not start $p; opening it in its own window"
    fi
    ( cd "$AC_BIN" && cmd //c start "$p" "$p.exe" )
}

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

# 0 when worldserver saved and exited on SOAP's `server shutdown`; 1 when SOAP is not set up,
# did not answer, or the server was still running after the wait (the caller terminates it).
soap_shutdown() {
    local python="$AC_ROOT/mind/.venv/Scripts/python.exe" soap="$AC_ROOT/scripts/ac-soap.py"
    local info rc players delay="${AC_SHUTDOWN_DELAY:-}" wait="${AC_SHUTDOWN_WAIT:-180}" i
    [ -x "$python" ] || return 1
    info=$("$python" -I "$soap" "server info" 2>/dev/null) && rc=0 || rc=$?
    if [ "$rc" != 0 ]; then
        [ "$rc" = 2 ] || warn "SOAP did not answer"
        return 1
    fi
    players=$(printf '%s\n' "$info" | sed -n 's/^Connected players: \([0-9][0-9]*\).*/\1/p')
    if [ -z "$delay" ]; then
        if [ "${players:-0}" -gt 0 ]; then delay=30; else delay=1; fi
    fi
    [[ $delay =~ ^[0-9]+$ && $wait =~ ^[0-9]+$ ]] || die "AC_SHUTDOWN_DELAY and AC_SHUTDOWN_WAIT are whole seconds"
    if ! "$python" -I "$soap" "server shutdown $delay" >/dev/null; then
        warn "SOAP refused the shutdown"
        return 1
    fi
    log "worldserver saving and shutting down in ${delay}s (${players:-0} players connected)"
    for ((i = 0; i < delay + wait; i++)); do
        if ! running worldserver.exe; then
            log "worldserver shut down cleanly"
            return 0
        fi
        sleep 1
    done
    warn "worldserver still running ${wait}s after its shutdown"
    return 1
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
    # AppenderFile rotates to '<base>.log.YYYY-MM-DD HH-MM-SS'. Never prune active logs.
    prune_python="$AC_ROOT/mind/.venv/Scripts/python.exe"
    if [ ! -x "$prune_python" ]; then prune_python=$(command -v python || command -v python3 || true); fi
    if [ -n "$prune_python" ]; then
        "$prune_python" "$AC_ROOT/scripts/prune-native-logs.py" "$AC_BIN" || warn "native log pruning failed"
    else
        warn "Python unavailable; native log pruning skipped"
    fi
    for p in authserver worldserver; do
        if running "$p.exe"; then
            warn "$p already running"
        else
            log "starting $p"
            launch "$p"
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
    if running worldserver.exe && ! soap_shutdown; then
        warn "worldserver will be terminated; characters keep only what the server last saved."
        warn "Without SOAP, type 'server shutdown 30' in its console first if characters are online."
    fi
    # Whatever is still running gets the polite request (5s), then is terminated.
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
