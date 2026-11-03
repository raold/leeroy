#!/usr/bin/env bash
# Start, stop, or check the voice service (leeroy-voice).
#
# Usage: ac-voice.sh start|stop|status
#
# The data dir and the two ports come from the service itself
# (python -m leeroy_voice where), so this follows LEEROY_VOICE_DATA and that
# dir's voice.toml. The environment passes the data dir on to the service.
#
# NOTE ON STOPPING: a plain taskkill posts WM_CLOSE to the service's hidden
# window, and it stops cleanly with WoW's volume restored. If it has not gone
# after 5 s it is terminated; the next start then restores WoW's volume.

source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

VOICE_BIN="$AC_ROOT/voice/.venv/Scripts"
PYTHON="$VOICE_BIN/python"
PYTHONW="$VOICE_BIN/pythonw.exe"
START_WAIT_S=30

[ -x "$PYTHON" ] || die "no voice venv at $VOICE_BIN"

DATA_DIR='' MIND_PORT='' UI_PORT=''
where=$("$PYTHON" -m leeroy_voice where) || die "cannot read the voice config"
while IFS='=' read -r key value; do
    value=${value%$'\r'}
    case "$key" in
    data_dir)  DATA_DIR=$(cygpath -u "$value") ;;
    mind_port) MIND_PORT=$value ;;
    ui_port)   UI_PORT=$value ;;
    esac
done <<< "$where"
[ -n "$DATA_DIR" ] && [ -n "$MIND_PORT" ] && [ -n "$UI_PORT" ] || die "bad output from where"

# The pid of the service holding the lock; fails when none does.
held_pid() { "$PYTHON" -m leeroy_voice held | tr -d '\r'; }

listening() {
    local connections
    connections=$(netstat -an -p tcp)
    grep -E "127\.0\.0\.1:$1 .*LISTENING" <<< "$connections" >/dev/null
}

alive() { tasklist //FI "PID eq $1" 2>/dev/null | grep "$1" >/dev/null; }

case "${1:-status}" in
status)
    printf '    data dir   %s\n' "$DATA_DIR"
    if pid=$(held_pid); then
        printf '    pid        %s\n' "$pid"
    else
        printf '    pid        not running\n'
    fi
    for port in "$MIND_PORT" "$UI_PORT"; do
        if listening "$port"; then
            printf '    port %-5s listening\n' "$port"
        else
            printf '    port %-5s closed\n' "$port"
        fi
    done
    ;;
start)
    if pid=$(held_pid); then
        die "already running, pid $pid: $DATA_DIR/voice.lock is held"
    fi
    mkdir -p "$DATA_DIR/logs"
    log "starting leeroy-voice"
    "$PYTHONW" -m leeroy_voice run >"$DATA_DIR/logs/voice-stdout.log" 2>&1 &
    disown
    for ((i = 0; i < START_WAIT_S; i++)); do
        if listening "$MIND_PORT" && listening "$UI_PORT"; then
            log "listening on $MIND_PORT and $UI_PORT"
            exit 0
        fi
        sleep 1
    done
    die "not listening after ${START_WAIT_S} s: see $DATA_DIR/logs/voice-stdout.log and voice.log"
    ;;
stop)
    if ! pid=$(held_pid); then
        printf '    leeroy-voice already stopped\n'
        exit 0
    fi
    [ -n "$pid" ] || die "$DATA_DIR/voice.lock is held, but voice.pid names nobody"
    taskkill //PID "$pid" >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5; do
        alive "$pid" || break
        sleep 1
    done
    if alive "$pid"; then
        log "terminating pid $pid"
        taskkill //F //PID "$pid" >/dev/null 2>&1 || true
    else
        log "leeroy-voice stopped"
    fi
    ;;
*)
    die "usage: $0 start|stop|status"
    ;;
esac
