#!/usr/bin/env bash
# Installs SolarMonitor's poller as a persistent systemd service — Type=simple,
# Restart=always.
# Safe to re-run; just rewrites the unit file and reloads/restarts.
#
# Requires: venv/ created and requirements installed, .env configured.

set -euo pipefail
cd "$(dirname "$0")/.."   # prod-env/
PROJECT_DIR="$(pwd)"
SERVICE_NAME="${SOLARMONITOR_SERVICE_NAME:-solarmonitor}"
RUN_USER="${SUDO_USER:-$USER}"

if [ ! -f venv/bin/python ]; then
    echo "ERROR: venv/ not found. Run:" >&2
    echo "  python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if [ ! -f .env ]; then
    echo "ERROR: .env not found — copy .env.example to .env and fill it in first." >&2
    exit 1
fi

mkdir -p data

sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=SolarEdge optimizer voltage/current data collector
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${PROJECT_DIR}
EnvironmentFile=${PROJECT_DIR}/.env
ExecStart=${PROJECT_DIR}/venv/bin/python -m src.main
Restart=always
RestartSec=10
StandardOutput=append:${PROJECT_DIR}/data/poller.log
StandardError=append:${PROJECT_DIR}/data/poller.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

sleep 2
sudo systemctl status "${SERVICE_NAME}" --no-pager | head -8

echo ""
echo "Installed and started. Logs: ${PROJECT_DIR}/data/poller.log"
echo "Manage it with: sudo systemctl {status|stop|start|restart} ${SERVICE_NAME}"
