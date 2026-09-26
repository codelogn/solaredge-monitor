#!/usr/bin/env bash
# Start/stop/check the read-only dashboard webapp — a completely separate
# process from the poller (see poller_ctl.sh). Stopping/restarting/redeploying
# this never interrupts data capturing; it only re-reads the same SQLite file.
set -euo pipefail
cd "$(dirname "$0")/.."
PIDFILE=data/webapp.pid
LOGFILE=data/webapp.log
PORT="${SOLARMONITOR_WEBAPP_PORT:-8090}"
mkdir -p data

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if is_running; then
      echo "Webapp already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    setsid nohup venv/bin/uvicorn webapp.app:app --host 0.0.0.0 --port "$PORT" \
      >> "$LOGFILE" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    disown
    sleep 1
    echo "Started webapp (pid $(cat "$PIDFILE")) on port $PORT. Logs: $LOGFILE"
    ;;
  stop)
    if is_running; then
      kill -TERM "$(cat "$PIDFILE")"
      echo "Sent shutdown signal to webapp (pid $(cat "$PIDFILE"))"
    else
      echo "Webapp not running"
    fi
    rm -f "$PIDFILE"
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    if is_running; then
      echo "Webapp running (pid $(cat "$PIDFILE")) on port $PORT"
    else
      echo "Webapp not running"
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}" >&2
    exit 1
    ;;
esac
