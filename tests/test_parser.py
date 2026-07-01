"""Parser tests. Run: python -m pytest, or python tests/test_parser.py"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent import parser, scanner  # noqa: E402

SAMPLE = open(os.path.join(os.path.dirname(__file__), "sample_screen.txt"), encoding="utf-8").read()


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


def test_not_a_receiver():
    assert not parser.is_valid_receiver(parser.parse_screen("bash: receiver_cli: not found"))


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
