# HiPAC-SHP — Pi Agent

Python agent for Raspberry Pi 4+. On a schedule it:

1. **Scans** the local subnet with `arp-scan -I eth0 192.168.1.0/24`.
2. For each discovered IP (minus your exclusions) it **SSHes in** as `root` using
   `~/.ssh/receiver_private_key`, runs the interactive `/receiver/receiver_cli`,
   waits ~15s for data to propagate, and **captures the rendered TUI screen**
   (via a PTY fed into a `pyte` terminal emulator).
3. **Parses** the Receiver Properties + Node Properties table into structured data.
4. **Stores** it in local SQLite and **pushes** it to the central server
   (`hipac.eastec.com.au`) using a per-site API token.

Hosts that aren't receivers (SSH refuses, or the CLI isn't there) are skipped
quietly. Results queue locally and retry if the server is offline.

A password-protected **local web UI** (port 8080) lets you set the site name,
polling interval, excluded IPs, server URL/token and SSH details, view the last
scan, and trigger "Scan now".

## Install on the Pi

### Option A — apt / `.deb` (recommended)

Once the package is built/hosted (see [docs/PACKAGING.md](docs/PACKAGING.md)):

```bash
sudo apt-get install hipac-shp-agent          # from a hosted apt repo
#   or, from a local build:
sudo apt-get install ./hipac-shp-agent_0.1.0_all.deb

# then place the receiver key for the service user:
sudo cp receiver_private_key /var/lib/hipac/.ssh/receiver_private_key
sudo chown hipac:hipac /var/lib/hipac/.ssh/receiver_private_key
sudo chmod 600 /var/lib/hipac/.ssh/receiver_private_key
```

The package pulls all deps from apt (no pip/venv), creates a `hipac` service
user, grants it passwordless `arp-scan`, and enables the systemd service.

### Option B — script install (no packaging)

```bash
mkdir -p ~/.ssh && cp receiver_private_key ~/.ssh/ && chmod 600 ~/.ssh/receiver_private_key
git clone <repo> && cd hipac-shp-agent
sudo ./install.sh
```

Either way, open `http://<pi-ip>:8080`, log in (default password `changeme`),
and set everything under **Settings** — including a new password.

## Local development (Windows/Mac/Linux)

```bash
cd hipac-shp-agent
python -m venv .venv && . .venv/Scripts/activate   # or .venv/bin/activate
pip install -r requirements.txt
python tests/test_parser.py       # offline parser checks — no deps needed
python -m hipac_agent             # runs the web UI + poller
```

Config and the SQLite DB live in `~/.hipac` (override with `HIPAC_DATA_DIR`).

## Central server API contract

```
POST {server_url}/api/poll
Authorization: Bearer {api_token}
Content-Type: application/json

{
  "site_name": "Warehouse A",
  "results": [{
    "receiver": {"radio_address","ip_address","mac_address","fw_version"},
    "nodes": [{"relay","fw_ver","radio_address","batt","heartbeat","rssi_nr","rssi_rn"}],
    "polled_at": "2026-07-01T06:11:40Z",
    "source_ip": "192.168.1.114"
  }]
}
```

Response `200`: `{"accepted": [<echoed client-side result ids>]}`.
The **receiver `mac_address` is the sticky key** for naming on the server side;
each node's **`radio_address`** is the sticky key for node names.
