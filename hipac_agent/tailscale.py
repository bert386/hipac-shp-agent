"""Detect this host's Tailscale identity so the agent can report it upstream.

Runs `tailscale status --json` (read-only, no root needed). Returns an empty
dict if Tailscale isn't installed or the node isn't up — safe to call always.
"""

import json
import logging
import subprocess

log = logging.getLogger("hipac.tailscale")


def local_identity() -> dict:
    """Return ``{'tailscale_host', 'tailscale_ip'}`` if available, else ``{}``."""
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        data = json.loads(proc.stdout or "{}")
    except (FileNotFoundError, ValueError, OSError, subprocess.SubprocessError) as exc:
        log.debug("tailscale status unavailable: %s", exc)
        return {}

    me = data.get("Self") or {}
    info = {}
    if me.get("HostName"):
        info["tailscale_host"] = me["HostName"]
    ipv4 = next((a for a in (me.get("TailscaleIPs") or []) if ":" not in a), None)
    if ipv4:
        info["tailscale_ip"] = ipv4
    return info
