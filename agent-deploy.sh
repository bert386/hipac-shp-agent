#!/usr/bin/env bash
# Redeploy the agent from the operator's git clone into /opt and restart it.
# Runs as root via a narrow NOPASSWD sudoers rule (see install.sh). The agent
# does the `git pull` as its own user first; this script only copies + restarts.
set -euo pipefail

SRC_USER="${SUDO_USER:-pi}"
CLONE="/home/${SRC_USER}/hipac-shp-agent"

cp -r "${CLONE}/hipac_agent" /opt/hipac-agent/
systemctl restart hipac-agent
