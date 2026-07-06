"""Push queued results to the central Laravel server.

API contract (matched by the Laravel side):

    POST {server_url}/api/poll
    Authorization: Bearer {api_token}
    Content-Type: application/json

    {
      "site_name": "Warehouse A",
      "results": [
        {
          "receiver": {"radio_address", "ip_address", "mac_address", "fw_version"},
          "nodes": [
            {"relay", "fw_ver", "radio_address", "batt", "heartbeat", "rssi_nr", "rssi_rn"}
          ],
          "polled_at": "2026-07-01T06:11:40Z",
          "source_ip": "192.168.1.114"
        }
      ]
    }

The server replies ``200`` with ``{"accepted": [<echoed client ids>]}``.
"""

import socket

import requests

from . import device, tailscale


class PushError(Exception):
    pass


def push(server_url: str, api_token: str, site_name: str, results: list[dict],
         scan: dict | None = None, timeout: int = 30) -> list[int]:
    """Send results (and optional scan progress); return accepted local ids.

    ``scan`` carries live cycle progress ({active, total, done, current,
    started_at, finished_at}). A progress-only ping (empty ``results`` with a
    ``scan``) is allowed so the dashboard can advance the bar past skipped hosts
    and mark the cycle complete.
    """
    if not results and scan is None:
        return []
    if not server_url or not api_token:
        raise PushError("server_url and api_token must be configured")

    url = server_url.rstrip("/") + "/api/poll"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }
    # Report our OS hostname + Tailscale identity so the server can show/auto-fill
    # the site's device details (Tailscale keys are no-ops if it isn't up).
    body = {
        "site_name": site_name,
        "agent_hostname": socket.gethostname(),
        "agent": device.stats(),
        "results": results,
        **({"scan": scan} if scan is not None else {}),
        **tailscale.local_identity(),
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise PushError(str(exc)) from exc

    if resp.status_code >= 400:
        raise PushError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        accepted = resp.json().get("accepted", [])
    except ValueError:
        accepted = []
    # If the server didn't echo ids, assume it took everything we sent.
    if not accepted:
        accepted = [r["id"] for r in results if "id" in r]
    return accepted
