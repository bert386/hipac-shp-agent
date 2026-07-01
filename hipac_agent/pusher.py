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

import requests

from . import tailscale


class PushError(Exception):
    pass


def push(server_url: str, api_token: str, site_name: str, results: list[dict], timeout: int = 30) -> list[int]:
    """Send results; return the list of local ids the server accepted."""
    if not results:
        return []
    if not server_url or not api_token:
        raise PushError("server_url and api_token must be configured")

    url = server_url.rstrip("/") + "/api/poll"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }
    # Report our Tailscale identity so the server can auto-fill the site's
    # remote-access details (no-op keys if Tailscale isn't up).
    body = {"site_name": site_name, "results": results, **tailscale.local_identity()}
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
