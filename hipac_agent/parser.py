"""Parse the rendered ``receiver_cli`` screen into structured data.

The CLI draws two boxed sections:

    Receiver Properties
      Radio Add.:  58:2b:0a:be:f9:79
      IP Add.:     192.168.1.114
      MAC Add.:    3c:18:a0:23:ac:d7
      F/W Version: v0.23.3

    Node Properties
      Relay | F/W Ver. | Radio Address     | Batt. | Heartbeat | RSSI N-R | RSSI R-N
      R1    | v0.23.3  | 80:34:28:1c:01:f6 |  180  | 06:11:40  |   -50    |   -54

By the time this runs the screen has been rendered by ``pyte`` into clean text,
so vertical box-drawing characters delimit the table columns.
"""

import re

_MAC = r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}"

_RECEIVER_FIELDS = {
    "radio_address": re.compile(r"Radio\s*Add\.?\s*:\s*(" + _MAC + r")", re.I),
    "ip_address": re.compile(r"IP\s*Add\.?\s*:\s*(\d{1,3}(?:\.\d{1,3}){3})", re.I),
    "mac_address": re.compile(r"MAC\s*Add\.?\s*:\s*(" + _MAC + r")", re.I),
    "fw_version": re.compile(r"F/?W\s*Version\s*:\s*(v?[\w.]+)", re.I),
}

# A single node row, matched by its field *content* rather than the column
# separator. Real receivers draw the table with the VT100 line-drawing set, and
# when the alternate charset isn't translated the vertical bar arrives as the
# letter 'x'; the mock/sample uses Unicode │; ncurses' ASCII fallback uses |.
# Matching relay → firmware → radio addr → battery → heartbeat → RSSI×2 with a
# non-digit gap (\D+?) between fields sidesteps whatever the separator is.
#
# Firmware is OPTIONAL: after a receiver-side fault (e.g. following a network
# outage) nodes stay fully present and reachable — radio/batt/heartbeat/RSSI all
# report — but the F/W Ver. column goes blank. Requiring firmware here dropped
# those still-live nodes from the capture, so they wrongly went stale on the
# dashboard. When the column is blank fw_ver is None and the node still parses.
_NODE_ROW = re.compile(
    r"R(?P<relay>\d+)\D+?"
    r"(?:(?P<fw_ver>v[\d.]+)\D+?)?"
    r"(?P<radio_address>[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\D+?"
    r"(?P<batt>\d+)\D+?"
    r"(?P<heartbeat>\d{1,2}:\d{2}:\d{2})\D+?"
    r"(?P<rssi_nr>-?\d+)\D+?"
    r"(?P<rssi_rn>-?\d+)"
)


def parse_nodes(text: str) -> list[dict]:
    nodes = []
    for line in text.splitlines():
        m = _NODE_ROW.search(line)
        if not m:
            continue
        nodes.append({
            "relay": "R" + m.group("relay"),
            "fw_ver": m.group("fw_ver"),
            "radio_address": m.group("radio_address"),
            "batt": m.group("batt"),
            "heartbeat": m.group("heartbeat"),
            "rssi_nr": m.group("rssi_nr"),
            "rssi_rn": m.group("rssi_rn"),
        })
    return nodes


def parse_screen(text: str) -> dict:
    """Return ``{"receiver": {...}, "nodes": [...]}`` parsed from the screen."""
    receiver = {}
    for field, pattern in _RECEIVER_FIELDS.items():
        m = pattern.search(text)
        if m:
            receiver[field] = m.group(1).strip()
    return {"receiver": receiver, "nodes": parse_nodes(text)}


def is_valid_receiver(parsed: dict) -> bool:
    """A real receiver: it has its own MAC/radio, OR it reports nodes.

    Some receivers show their own properties as "unknown" while still relaying a
    full node table — those are valid receivers (we backfill their MAC from the
    arp-scan result upstream).
    """
    r = parsed.get("receiver", {})
    return bool(r.get("mac_address") or r.get("radio_address") or parsed.get("nodes"))


def looks_like_receiver(text: str) -> bool:
    """Structural check: is this the receiver_cli screen at all, even if every
    field is still blank/unknown? Used to decide whether to keep waiting for the
    node table to paint vs. give up on a non-receiver host.
    """
    return "Receiver Properties" in text or "Node Properties" in text


# The Receiver Properties header paints LAST (after the node table) and can be
# slow. While loading, its fields read "unknown"; it's only "ready" once the
# Radio Add. shows a real address.
_RADIO_READY = re.compile(r"Radio\s*Add\.?\s*:\s*(" + _MAC + r")", re.I)


def header_ready(text: str) -> bool:
    """True once the receiver's header has finished loading — the Radio Add.
    field shows a real address rather than "unknown"/blank. The header paints
    last and can be slow, so this signals the whole screen is fully rendered."""
    return bool(_RADIO_READY.search(text))


# Known receiver-side CLI faults, recognised from the captured screen. When
# `receiver_cli` can't start it prints an error instead of the TUI. The classic
# one (seen after a network blip) is its listening socket still being held by a
# previous instance — rebooting the receiver clears it. Each entry maps a
# recognisable substring to a short code + a human message.
_CLI_FAULTS = (
    ("Address already in use", "cli_socket_busy",
     "receiver_cli couldn't start — socket already in use"),
    ("Failed to bind socket", "cli_socket_busy",
     "receiver_cli couldn't start — failed to bind socket"),
)


def detect_cli_fault(text: str) -> dict | None:
    """Return ``{"code", "message"}`` if the screen shows a known receiver-side
    CLI fault (instead of the normal TUI), else None. A reboot clears these."""
    for needle, code, message in _CLI_FAULTS:
        if needle in text:
            return {"code": code, "message": message}
    return None


def is_blank_receiver(text: str) -> bool:
    """A stuck/empty receiver: the receiver_cli screen IS drawn, but the receiver
    doesn't even know its own identity (Radio Add. still "unknown") and reports
    zero nodes. Distinct from a healthy receiver that legitimately has 0 nodes
    (which resolves its own header) or one that's still painting (which shows
    nodes first). A reboot doesn't reliably clear this — needs manual recovery,
    so the agent reports it as a skip rather than looping reboots."""
    return looks_like_receiver(text) and not header_ready(text) and not parse_nodes(text)
