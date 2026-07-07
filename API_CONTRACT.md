# HiPAC-SHP API contract (canonical)

> This file is the **source of truth** for the agent↔server contract.
> Mirror a copy into `hipac-shp-server` and keep them in sync when either side changes.

## Upload endpoint

```
POST {server_url}/api/poll
Authorization: Bearer {api_token}     # per-site token
Content-Type: application/json
Accept: application/json
```

### Request body

```json
{
  "site_name": "Warehouse A",
  "results": [
    {
      "id": 42,
      "receiver": {
        "radio_address": "58:2b:0a:be:f9:79",
        "ip_address": "192.168.1.114",
        "mac_address": "3c:18:a0:23:ac:d7",
        "fw_version": "v0.23.3",
        "clock_time": "2026-07-07T06:11:38Z",
        "clock_skew_seconds": -2
      },
      "nodes": [
        {
          "relay": "R1",
          "fw_ver": "v0.23.3",
          "radio_address": "80:34:28:1c:01:f6",
          "batt": "180",
          "heartbeat": "06:11:40",
          "rssi_nr": "-50",
          "rssi_rn": "-54"
        }
      ],
      "polled_at": "2026-07-01T06:11:40Z",
      "source_ip": "192.168.1.114"
    }
  ]
}
```

- `id` is the agent's local row id, echoed back so it can mark rows uploaded.
- All node fields are strings (captured verbatim from the CLI).
- `receiver.clock_time` / `receiver.clock_skew_seconds` (optional): the receiver's
  own clock (read over SSH via `date -u +%s`) and how far it is off true UTC
  (`receiver − Pi`, sampled together). The server flags the dashboard red when
  `abs(clock_skew_seconds) > 900` (15 min). Only sent when the read succeeds; a
  poll without them leaves the last-known value in place.

#### Fault results (optional)

When a receiver's CLI faults (e.g. its socket is stuck — `Address already in
use`), the agent can't capture node data, so it sends a **fault result** instead:
`receiver.mac_address` + an empty `nodes` array + a `fault` object. The agent
also auto-reboots the receiver to clear it (cooldown-guarded).

```json
{
  "id": 43,
  "receiver": { "mac_address": "3c:18:a0:23:ac:d7", "ip_address": "192.168.1.186" },
  "nodes": [],
  "fault": {
    "code": "cli_socket_busy",
    "message": "receiver_cli couldn't start — socket already in use",
    "action": "auto-reboot issued (attempt 1)"
  },
  "polled_at": "2026-07-03T09:00:00Z",
  "source_ip": "192.168.1.186"
}
```

The server records the fault on the receiver **without** bumping `last_seen_at`
or clobbering the known `radio_address`/`fw_version` (the node data is stale),
and **clears** it on the next successful (non-fault) poll for that receiver.

### Response `200`

```json
{ "accepted": [42] }
```

`accepted` is the list of client `id`s the server has stored. The agent marks
those rows uploaded and retries the rest. If the array is empty the agent
assumes everything sent was accepted.

### Errors

- `401` — bad/missing token (agent keeps the results queued, retries later).
- `4xx/5xx` — logged; results stay queued for the next cycle.

## Heartbeat endpoint

```
POST {server_url}/api/heartbeat
Authorization: Bearer {api_token}
Content-Type: application/json
```

A lightweight liveness ping the agent sends every ~60s (skipped while a scan is
running, since the scan's own uploads already keep the server fresh). Body is
the agent metadata only — no results:

```json
{
  "site_name": "Warehouse A",
  "agent_hostname": "hipacpi3",
  "agent": { "version": "0.10.0", "uptime_seconds": 90000, "load_1m": 0.2,
             "disk_free": 1000000, "disk_total": 2000000 },
  "tailscale_host": "hipac-warehouse-a",
  "tailscale_ip": "100.x.y.z"
}
```

Response `200 { "ok": true }`. The server updates the site's agent stats +
`last_heartbeat_at`; a normal `/api/poll` also bumps `last_heartbeat_at`. The
dashboard shows a site online when `last_heartbeat_at` is within 180s.

## Sticky-naming keys (server side)

- **Receiver** friendly name is keyed to `receiver.mac_address` — stable across
  IP or firmware changes.
- **Node** friendly name is keyed to its `radio_address`.

Ingest should **upsert** receivers by `mac_address` and nodes by `radio_address`,
recording a new reading/heartbeat each poll while preserving the user-assigned
names.
