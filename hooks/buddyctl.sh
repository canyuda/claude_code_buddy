#!/usr/bin/env bash
# buddyctl — manage the claude-code-buddy BLE daemon
#
# Usage:
#   buddyctl start       Start the daemon in background
#   buddyctl stop        Stop the daemon
#   buddyctl restart     Restart the daemon
#   buddyctl status      Check if daemon is running and connected
#   buddyctl log         Tail the daemon log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DAEMON="$SCRIPT_DIR/buddyd.py"
SOCK_PATH="${BUDDY_SOCK:-$HOME/.claude/buddy.sock}"
PID_FILE="$SOCK_PATH.pid"
LOG_FILE="${BUDDY_LOG:-$HOME/.claude/buddyd.log}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

status_text() {
    if is_running; then
        echo -e "${GREEN}running${NC} (pid $(cat "$PID_FILE"))"
    else
        echo -e "${RED}stopped${NC}"
    fi
}

is_running() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid=$(cat "$PID_FILE")
    kill -0 "$pid" 2>/dev/null
}

do_start() {
    if is_running; then
        echo -e "buddyd is already ${GREEN}running${NC} (pid $(cat "$PID_FILE"))"
        return 0
    fi

    echo "Starting buddyd..."
    # Run buddyd in --foreground mode under nohup instead of relying on its
    # built-in os.fork() daemonization (which crashes on macOS + Python 3.14:
    # the forked child dies before write_pid, so the PID file never appears).
    # --foreground still runs write_pid(), so stop/status keep working.
    nohup python3 "$DAEMON" --foreground --log "$LOG_FILE" --socket "$SOCK_PATH" \
        >/dev/null 2>&1 &
    disown 2>/dev/null || true
    sleep 1

    if is_running; then
        echo -e "buddyd ${GREEN}started${NC} (pid $(cat "$PID_FILE"))"
    else
        echo -e "${RED}Failed to start buddyd. Check log: $LOG_FILE${NC}" >&2
        return 1
    fi
}

do_stop() {
    if ! is_running; then
        echo -e "buddyd is already ${RED}stopped${NC}"
        return 0
    fi

    local pid
    pid=$(cat "$PID_FILE")
    echo "Stopping buddyd (pid $pid)..."
    kill "$pid" 2>/dev/null || true

    # Wait up to 5 seconds for graceful shutdown
    for i in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo -e "buddyd ${GREEN}stopped${NC}"
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 0.5
    done

    # Force kill
    echo -e "${YELLOW}Graceful shutdown timed out, killing...${NC}"
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo -e "buddyd ${RED}killed${NC}"
}

do_restart() {
    do_stop
    sleep 1
    do_start
}

do_status() {
    echo -n "buddyd: "
    status_text

    if is_running; then
        # Check BLE connection via ping
        if [ -S "$SOCK_PATH" ]; then
            local ping_result
            ping_result=$(echo '{"action":"ping"}' | socat - UNIX-CONNECT:"$SOCK_PATH" 2>/dev/null || echo '{"ok":false}')
            local ble_status
            ble_status=$(echo "$ping_result" | python3 -c "import sys,json; d=json.load(sys.stdin); print('connected' if d.get('ble') else 'disconnected')" 2>/dev/null || echo "unknown")
            echo "BLE: $ble_status"
        else
            echo "Socket: not found"
        fi
    fi
}

do_log() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "No log file at $LOG_FILE"
        return 1
    fi
}

# Main
case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_restart ;;
    status)  do_status ;;
    log)     do_log ;;
    *)
        echo "Usage: buddyctl {start|stop|restart|status|log}"
        echo ""
        echo "Manage the claude-code-buddy BLE daemon."
        echo ""
        echo "Commands:"
        echo "  start     Start the daemon in background"
        echo "  stop      Stop the daemon"
        echo "  restart   Restart the daemon"
        echo "  status    Check daemon and BLE connection status"
        echo "  log       Tail the daemon log file"
        exit 1
        ;;
esac
