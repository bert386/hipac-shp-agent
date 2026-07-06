#!/usr/bin/env bash
# Provision Tailscale on a HiPAC Pi so admins can SSH to it (and hop to receivers).
#
#   sudo ./tailscale-provision.sh <auth-key> <hostname>
#   e.g. sudo ./tailscale-provision.sh tskey-auth-XXXX hipac-northern-health
#
# Note: we deliberately do NOT advertise subnet routes — all sites use
# 192.168.1.0/24, so overlapping routes would collide. Reach receivers by
# SSHing to this Pi and hopping to them on the local LAN.
set -euo pipefail

AUTHKEY="${1:?usage: tailscale-provision.sh <auth-key> <hostname>}"
HOSTNAME_ARG="${2:?usage: tailscale-provision.sh <auth-key> <hostname>}"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "==> Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo "==> Bringing Tailscale up (SSH enabled) as ${HOSTNAME_ARG}"
tailscale up --ssh --authkey="${AUTHKEY}" --hostname="${HOSTNAME_ARG}"

# Let the agent user manage `tailscale serve` (the in-browser web terminal)
# without root. Persistent setting; do it here so a freshly provisioned Pi can
# expose its terminal the first time the agent starts, no extra step.
echo "==> Granting operator to ${SUDO_USER:-pi} (for the web terminal)"
tailscale set --operator="${SUDO_USER:-pi}" || true

TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
echo
echo "Done. Tailscale IP: ${TS_IP:-<pending>}"
echo "Next: on the site's dashboard page → Remote access (Tailscale),"
echo "  set the hostname '${HOSTNAME_ARG}'${TS_IP:+ (IP ${TS_IP})}."
