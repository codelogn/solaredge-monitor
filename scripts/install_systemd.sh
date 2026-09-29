#!/usr/bin/env bash
# Installs SolarMonitor as two systemd services that start at boot and
# restart on failure:
#   solarmonitor.service      the collector (python -m src.main)
#   solarmonitor-web.service  the dashboard (uvicorn, port 8090)
# Kept as two services for the same reason they are two processes:
# restarting the dashboard must never interrupt data collection.
#
# Safe to re-run: rewrites the unit files, reloads, and restarts both.
# Afterwards scripts/poller_ctl.sh and webapp_ctl.sh drive these services.
# Requires: venv/ with requirements installed, and a filled-in .env.
# Remove with:  scripts/install_systemd.sh --uninstall

set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
RUN_USER="${SUDO_USER:-$USER}"
PORT="${SOLARMONITOR_WEBAPP_PORT:-8090}"
POLLER=solarmonitor
WEB=solarmonitor-web

if [ "${1:-}" = "--uninstall" ]; then
    sudo systemctl disable --now "$POLLER" "$WEB" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/$POLLER.service" "/etc/systemd/system/$WEB.service"
    sudo systemctl daemon-reload
    echo "Removed $POLLER and $WEB services."
    exit 0
fi

if [ ! -x venv/bin/python ]; then
    echo "ERROR: venv/ not found. Run:" >&2
    echo "  python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if [ ! -f .env ]; then
    echo "ERROR: .env not found — copy .env.example to .env and fill it in first." >&2
    exit 1
fi
mkdir -p data

# Stop copies started by the ctl scripts, or the service would run a second
# collector — and the inverter accepts only one Modbus connection at a time.
for pidfile in data/poller.pid data/webapp.pid; do
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        kill -TERM "$(cat "$pidfile")" && echo "Stopped process from $pidfile"
    fi
    rm -f "$pidfile"
done
sleep 2

# .env is deliberately not passed as EnvironmentFile: the app reads it itself
# (python-dotenv), and systemd parses such files differently (e.g. "$").
sudo tee "/etc/systemd/system/$POLLER.service" > /dev/null <<EOF
[Unit]
Description=SolarMonitor collector (SolarEdge optimizers + inverter Modbus)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/venv/bin/python -m src.main
Restart=always
RestartSec=10
# Lets a poll cycle in progress finish cleanly on stop.
TimeoutStopSec=90
StandardOutput=append:${PROJECT_DIR}/data/poller.log
StandardError=append:${PROJECT_DIR}/data/poller.log

[Install]
WantedBy=multi-user.target
EOF

sudo tee "/etc/systemd/system/$WEB.service" > /dev/null <<EOF
[Unit]
Description=SolarMonitor dashboard (read-only web UI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/venv/bin/uvicorn webapp.app:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=5
StandardOutput=append:${PROJECT_DIR}/data/webapp.log
StandardError=append:${PROJECT_DIR}/data/webapp.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$POLLER" "$WEB" >/dev/null
sudo systemctl restart "$POLLER" "$WEB"
sleep 3
systemctl --no-pager --lines=0 status "$POLLER" "$WEB" | grep -E "●|Loaded|Active"

echo ""
echo "Installed. Both start at boot and restart on failure."
echo "  Logs:    ${PROJECT_DIR}/data/poller.log, data/webapp.log"
echo "  Control: scripts/poller_ctl.sh | scripts/webapp_ctl.sh  {start|stop|restart|status}"
