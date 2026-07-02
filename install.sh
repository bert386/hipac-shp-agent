#!/usr/bin/env bash
# HiPAC-SHP Pi agent installer. Run on a Raspberry Pi 4+ (Raspberry Pi OS / Debian).
#   sudo ./install.sh
set -euo pipefail

APP_USER="${SUDO_USER:-pi}"
APP_DIR="/opt/hipac-agent"
DATA_DIR="/home/${APP_USER}/.hipac"
KEY_PATH="/home/${APP_USER}/.ssh/receiver_private_key"

echo "==> Installing system packages"
apt-get update
apt-get install -y arp-scan python3 python3-venv python3-pip

echo "==> Deploying app to ${APP_DIR}"
mkdir -p "${APP_DIR}"
cp -r hipac_agent "${APP_DIR}/"
cp requirements.txt "${APP_DIR}/"
cp agent-deploy.sh "${APP_DIR}/"

echo "==> Creating virtualenv"
python3 -m venv "${APP_DIR}/venv"
"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"
# The self-update helper is run as root via sudoers, so keep it root-owned and
# not writable by the agent user (prevents privilege escalation).
chown root:root "${APP_DIR}/agent-deploy.sh"
chmod 755 "${APP_DIR}/agent-deploy.sh"

echo "==> Granting the service user narrow passwordless sudo"
cat > /etc/sudoers.d/hipac-arpscan <<EOF
${APP_USER} ALL=(root) NOPASSWD: /usr/sbin/arp-scan, /usr/bin/arp-scan, ${APP_DIR}/agent-deploy.sh
EOF
chmod 440 /etc/sudoers.d/hipac-arpscan

echo "==> Fixing SSH key permissions (if present)"
if [ -f "${KEY_PATH}" ]; then
  chmod 600 "${KEY_PATH}"
  chown "${APP_USER}:${APP_USER}" "${KEY_PATH}"
else
  echo "    NOTE: ${KEY_PATH} not found. Copy the receiver private key there, then:"
  echo "          chmod 600 ${KEY_PATH}"
fi

echo "==> Installing systemd service"
sed "s#__APP_USER__#${APP_USER}#g; s#__APP_DIR__#${APP_DIR}#g; s#__DATA_DIR__#${DATA_DIR}#g" \
  hipac-agent.service > /etc/systemd/system/hipac-agent.service
systemctl daemon-reload
systemctl enable hipac-agent
systemctl restart hipac-agent

echo
echo "Done. Agent web UI: http://$(hostname -I | awk '{print $1}'):8080"
echo "Default password is 'changeme' — change it under Settings immediately."
