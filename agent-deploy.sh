#!/usr/bin/env bash
# Redeploy the agent from the operator's git clone into /opt and restart it.
# Runs as root via a narrow NOPASSWD sudoers rule (see install.sh). The agent
# does the `git pull` as its own user first; this script only copies + restarts.
set -euo pipefail

SRC_USER="${SUDO_USER:-pi}"
CLONE="/home/${SRC_USER}/hipac-shp-agent"

cp -r "${CLONE}/hipac_agent" /opt/hipac-agent/

# Keep the deploy script itself current. This file runs from /opt, and until
# now it only copied the Python package — so any change to THIS script (e.g. the
# operator grant below) never reached /opt and silently never ran. Copy ourselves
# forward so future deploy-script changes propagate without re-running install.sh.
cp "${CLONE}/agent-deploy.sh" /opt/hipac-agent/agent-deploy.sh
chmod 755 /opt/hipac-agent/agent-deploy.sh

# Let the agent user drive `tailscale serve` (the in-browser terminal) without
# sudo. This is the one privileged step terminal.py can't do itself; it's
# idempotent and we're already root here. Harmless if Tailscale isn't installed.
if command -v tailscale >/dev/null 2>&1; then
  tailscale set --operator="${SRC_USER}" || true
fi

systemctl restart hipac-agent
