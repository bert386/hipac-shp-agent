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

# Order of columns in the Node Properties table.
_NODE_FIELDS = ["relay", "fw_ver", "radio_address", "batt", "heartbeat", "rssi_nr", "rssi_rn"]

# Any vertical box-drawing glyph -> a plain pipe so we can split on columns.
_VERTICALS = "│┃╎╏┆┇┊┋|"
_ROW_LABEL = re.compile(r"^R\d+$")


def _split_cells(line: str) -> list[str]:
    normalised = line
    for ch in _VERTICALS:
        normalised = normalised.replace(ch, "|")
    if "|" in normalised:
        cells = [c.strip() for c in normalised.split("|")]
    else:
        # Fallback for plain-text output: split on runs of 2+ spaces.
        cells = re.split(r"\s{2,}", normalised.strip())
    return [c for c in cells if c != ""]


def parse_nodes(text: str) -> list[dict]:
    nodes = []
    for raw in text.splitlines():
        cells = _split_cells(raw)
        if cells and _ROW_LABEL.match(cells[0]):
            node = {
                field: (cells[i].strip() if i < len(cells) else None)
                for i, field in enumerate(_NODE_FIELDS)
            }
            nodes.append(node)
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
    """A screen is a real receiver if we found its MAC or radio address."""
    r = parsed.get("receiver", {})
    return bool(r.get("mac_address") or r.get("radio_address"))
