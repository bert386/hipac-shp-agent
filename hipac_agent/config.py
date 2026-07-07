"""Agent configuration: load/save a JSON config file, with sane defaults.

The config lives in the data directory (``HIPAC_DATA_DIR`` env var, default
``~/.hipac`` so it works both on a Pi and on a dev box). It is editable both by
hand and through the local web UI.
"""

import json
import os
import threading

_LOCK = threading.RLock()


def data_dir() -> str:
    d = os.environ.get("HIPAC_DATA_DIR") or os.path.join(
        os.path.expanduser("~"), ".hipac"
    )
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


def db_path() -> str:
    return os.path.join(data_dir(), "hipac.db")


DEFAULTS = {
    # Identity / reporting
    "site_name": "Unnamed Site",
    "server_url": "https://hipac.eastec.com.au",
    "api_token": "",
    # Scanning
    "interface": "eth0",
    "subnet": "192.168.1.0/24",
    "use_sudo": True,
    "poll_interval_minutes": 60,
    "excluded_ips": [],
    "arp_scan_timeout": 120,
    "arp_scan_retries": 5,           # more retries = fewer missed hosts on flaky nets
    "results_keep_per_receiver": 200,  # local DB: keep this many uploaded results/receiver
    # Command delivery. The agent long-polls: it asks the server to hold the
    # request open for up to command_longpoll_seconds and return the instant a
    # command is queued (near-instant delivery). command_poll_seconds is the
    # fallback pace used only when long-poll isn't held (old server / errors).
    "command_longpoll_seconds": 25,
    "command_poll_seconds": 60,
    # Command run for the "Update agent" button (pull latest, redeploy, restart).
    # Requires the sudoers entry from install.sh. Empty = disabled.
    "agent_update_command": 'cd "$HOME/hipac-shp-agent" && git pull --ff-only && sudo /opt/hipac-agent/agent-deploy.sh',
    # SSH into receivers
    "ssh_user": "root",
    "ssh_key_path": os.path.join(os.path.expanduser("~"), ".ssh", "receiver_private_key"),
    "ssh_connect_timeout": 15,
    "cli_command": "/receiver/receiver_cli",
    # Adaptive capture: wait until the node table settles rather than a fixed time.
    "cli_wait_seconds": 15,          # legacy floor; also the minimum for max_wait
    "cli_min_wait_seconds": 10,      # never accept a screen before this
    "cli_max_wait_seconds": 60,      # hard cap per receiver (header can be slow)
    "cli_stable_seconds": 8,         # node count unchanged this long => settled
    "cli_header_seconds": 15,        # no Receiver-CLI screen by now => not a receiver
    "term_cols": 200,
    "term_rows": 60,
    # Auto-recovery: when a receiver's CLI reports a known fault (e.g. its
    # socket is stuck — "Address already in use"), reboot it to clear the fault
    # so the next scan captures normally. Guarded so a genuinely broken receiver
    # can't reboot-loop: a per-receiver cooldown + a cap on consecutive reboots.
    "fault_auto_reboot": True,
    "fault_reboot_cooldown_seconds": 1800,   # min gap between auto-reboots of one receiver
    "fault_reboot_max_attempts": 3,          # after this many with no recovery, log only
    # Read each receiver's clock during the poll (a quick `date` over SSH) and
    # report how far it is off UTC, so the dashboard can flag clock drift.
    "report_receiver_clock": True,
    # Local web UI
    "config_password": "changeme",
    "web_host": "0.0.0.0",
    "web_port": 8080,
    # In-browser terminal (ttyd + Tailscale Serve). The agent self-provisions
    # ttyd and exposes it on the tailnet ONLY (ttyd binds to loopback); see
    # terminal.py. Set terminal_enabled=false to opt a Pi out entirely.
    "terminal_enabled": True,
    "terminal_port": 7681,
}


def load() -> dict:
    """Return the current config, merged over defaults."""
    with _LOCK:
        cfg = dict(DEFAULTS)
        path = config_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except (ValueError, OSError):
                # Corrupt config: fall back to defaults rather than crashing.
                pass
        # Normalise a couple of fields.
        cfg["excluded_ips"] = list(cfg.get("excluded_ips") or [])
        return cfg


def save(updates: dict) -> dict:
    """Merge ``updates`` into the stored config and persist it."""
    with _LOCK:
        cfg = load()
        cfg.update(updates)
        tmp = config_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
        os.replace(tmp, config_path())
        return cfg
