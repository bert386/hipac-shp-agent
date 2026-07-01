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
        "fw_version": "v0.23.3"
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

## Sticky-naming keys (server side)

- **Receiver** friendly name is keyed to `receiver.mac_address` — stable across
  IP or firmware changes.
- **Node** friendly name is keyed to its `radio_address`.

Ingest should **upsert** receivers by `mac_address` and nodes by `radio_address`,
recording a new reading/heartbeat each poll while preserving the user-assigned
names.
