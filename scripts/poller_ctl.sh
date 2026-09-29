#!/usr/bin/env bash
# Start/stop/check the data-capturing poller as an independent background
# process (separate from the webapp — see webapp_ctl.sh). Safe to leave
# running for days; restarting the webapp never touches this process.
set -euo pipefail
cd "$(dirname "$0")/.."

# Once installed as a service (scripts/install_systemd.sh), systemd owns the
# process and restarts it at boot; drive it through systemctl so a second,
# unmanaged copy can never be started alongside it.
UNIT=solarmonitor
if [ -f "/etc/systemd/system/$UNIT.service" ]; then
  case "${1:-}" in
    start|stop|restart)
      sudo systemctl "$1" "$UNIT"
      echo "Poller $1: systemd service $UNIT is now $(systemctl is-active "$UNIT" || true)"
      ;;
    status)
      if systemctl is-active --quiet "$UNIT"; then
        echo "Poller running (systemd service $UNIT, pid $(systemctl show -p MainPID --value "$UNIT"), starts at boot)"
      else
        echo "Poller not running (systemd service $UNIT: $(systemctl is-active "$UNIT" || true))"
      fi
      ;;
    *) echo "Usage: $0 {start|stop|restart|status}" >&2; exit 1 ;;
  esac
  exit 0
fi
PIDFILE=data/poller.pid
LOGFILE=data/poller.log
mkdir -p data

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if is_running; then
      echo "Poller already running (pid $(cat "$PIDFILE"))"; exit 0
    fi
    setsid nohup venv/bin/python -m src.main >> "$LOGFILE" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    disown
    sleep 1
    echo "Started poller (pid $(cat "$PIDFILE")). Logs: $LOGFILE"
    ;;
  stop)
    if is_running; then
      kill -TERM "$(cat "$PIDFILE")"
      echo "Sent shutdown signal to poller (pid $(cat "$PIDFILE"))"
    else
      echo "Poller not running"
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
      echo "Poller running (pid $(cat "$PIDFILE"))"
    else
      echo "Poller not running"
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}" >&2
    exit 1
    ;;
esac
