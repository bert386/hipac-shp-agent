"""Parser tests. Run: python -m pytest, or python tests/test_parser.py"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent import parser, scanner  # noqa: E402

SAMPLE = open(os.path.join(os.path.dirname(__file__), "sample_screen.txt"), encoding="utf-8").read()
SAMPLE_ACS = open(os.path.join(os.path.dirname(__file__), "sample_screen_acs.txt"), encoding="utf-8").read()
SAMPLE_UNKNOWN = open(os.path.join(os.path.dirname(__file__), "sample_screen_unknown.txt"), encoding="utf-8").read()


def test_receiver_fields():
    parsed = parser.parse_screen(SAMPLE)
    r = parsed["receiver"]
    assert r["radio_address"] == "58:2b:0a:be:f9:79"
    assert r["ip_address"] == "192.168.1.114"
    assert r["mac_address"] == "3c:18:a0:23:ac:d7"
    assert r["fw_version"] == "v0.23.3"
    assert parser.is_valid_receiver(parsed)


def test_nodes():
    nodes = parser.parse_screen(SAMPLE)["nodes"]
    assert len(nodes) == 3
    assert nodes[0] == {
        "relay": "R1", "fw_ver": "v0.23.3", "radio_address": "80:34:28:1c:01:f6",
        "batt": "180", "heartbeat": "06:11:40", "rssi_nr": "-50", "rssi_rn": "-54",
    }
    assert nodes[2]["radio_address"] == "80:34:28:1b:c8:1a"
    assert nodes[1]["batt"] == "198"


def test_real_hardware_acs_capture():
    # VT100 line-drawing capture from a real receiver (vertical bar renders 'x').
    parsed = parser.parse_screen(SAMPLE_ACS)
    assert parsed["receiver"]["mac_address"] == "3c:18:a0:21:95:4e"
    assert parsed["receiver"]["ip_address"] == "192.168.1.140"
    assert parsed["receiver"]["fw_version"] == "v0.23.4"

    nodes = parsed["nodes"]
    assert len(nodes) == 7  # R1..R7; trailing blank row ignored
    assert nodes[0] == {
        "relay": "R1", "fw_ver": "v0.23.4", "radio_address": "80:34:28:1b:d4:5b",
        "batt": "198", "heartbeat": "07:46:55", "rssi_nr": "-74", "rssi_rn": "-78",
    }
    assert nodes[6]["radio_address"] == "80:34:28:1c:2d:28"
    assert nodes[4]["rssi_nr"] == "-43"


def test_receiver_with_unknown_header_is_valid_via_nodes():
    # A receiver reporting its own props as "unknown" but relaying 4 nodes.
    parsed = parser.parse_screen(SAMPLE_UNKNOWN)
    assert parser.looks_like_receiver(SAMPLE_UNKNOWN)
    assert parser.is_valid_receiver(parsed)          # valid via its nodes
    assert not parsed["receiver"].get("mac_address")  # header was "unknown"
    nodes = parsed["nodes"]
    assert len(nodes) == 4
    assert nodes[0]["radio_address"] == "80:34:28:1c:90:a7"
    assert nodes[3]["rssi_rn"] == "-59"


def test_header_ready():
    # Real radio address in the header -> ready.
    assert parser.header_ready(SAMPLE)
    assert parser.header_ready(SAMPLE_ACS)
    # Header still shows "unknown" (loading) -> NOT ready; keep waiting.
    assert not parser.header_ready(SAMPLE_UNKNOWN)
    # Blank / no header -> not ready.
    assert not parser.header_ready("x Radio Add.:                                 x")
    assert not parser.header_ready("nothing here")


def test_not_a_receiver():
    text = "bash: receiver_cli: not found"
    assert not parser.is_valid_receiver(parser.parse_screen(text))
    assert not parser.looks_like_receiver(text)


def test_arp_parsing():
    out = (
        "Interface: eth0, type: EN10MB\n"
        "192.168.1.1\t9c:1c:12:aa:bb:cc\t(Router)\n"
        "192.168.1.114\t3c:18:a0:23:ac:d7\t(Unknown)\n"
        "\n2 packets received\n"
    )
    devs = scanner.parse_arp_output(out)
    assert {d["ip"] for d in devs} == {"192.168.1.1", "192.168.1.114"}
    assert devs[1]["mac"] == "3c:18:a0:23:ac:d7"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("All parser tests passed.")
