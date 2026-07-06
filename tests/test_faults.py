"""Receiver-fault detection + auto-reboot. Run: python -m pytest, or directly."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent import parser, poller  # noqa: E402
from hipac_agent.storage import Storage  # noqa: E402

_CFG = {
    "fault_auto_reboot": True,
    "fault_reboot_cooldown_seconds": 1800,
    "fault_reboot_max_attempts": 3,
    "ssh_user": "root",
    "ssh_key_path": "/k",
    "ssh_connect_timeout": 15,
}


# -- detection -------------------------------------------------------------
def test_detects_socket_busy_faults():
    assert parser.detect_cli_fault("bind(): Address already in use")["code"] == "cli_socket_busy"
    assert parser.detect_cli_fault("Failed to bind socket")["code"] == "cli_socket_busy"


def test_normal_screen_is_not_a_fault():
    normal = "Receiver Properties\n Radio Add.: 58:2b:0a:be:f9:79\nNode Properties\n R1 ..."
    assert parser.detect_cli_fault(normal) is None


# -- auto-reboot budget ----------------------------------------------------
def _poller(monkeypatch):
    calls = []
    monkeypatch.setattr(poller, "exec_receiver_command",
                        lambda **kw: calls.append(kw) or (0, "", ""))
    p = poller.Poller(storage=None)
    return p, calls


def test_first_fault_reboots_then_cooldown_blocks(monkeypatch):
    p, calls = _poller(monkeypatch)
    key = p._fault_key("aa:bb:cc:dd:ee:ff", "192.168.1.186")

    first = p._maybe_auto_reboot(_CFG, key, "192.168.1.186")
    assert "issued (attempt 1)" in first
    assert len(calls) == 1
    assert calls[0]["command"] == "sync && reboot"

    # Immediately again → within cooldown, no second reboot.
    second = p._maybe_auto_reboot(_CFG, key, "192.168.1.186")
    assert "cooldown" in second
    assert len(calls) == 1


def test_stops_after_max_attempts(monkeypatch):
    p, calls = _poller(monkeypatch)
    key = "k"
    import time
    # Simulate 3 past reboots, cooldown already elapsed.
    p._fault_reboots[key] = {"at": time.monotonic() - 10_000, "count": 3}
    msg = p._maybe_auto_reboot(_CFG, key, "192.168.1.186")
    assert "manual attention" in msg
    assert len(calls) == 0  # capped, no further reboot


def test_reboots_again_after_cooldown_until_cap(monkeypatch):
    p, calls = _poller(monkeypatch)
    key = "k"
    import time
    p._fault_reboots[key] = {"at": time.monotonic() - 10_000, "count": 2}  # cooldown elapsed, under cap
    msg = p._maybe_auto_reboot(_CFG, key, "192.168.1.186")
    assert "issued (attempt 3)" in msg
    assert len(calls) == 1


def test_disabled_does_not_reboot(monkeypatch):
    p, calls = _poller(monkeypatch)
    msg = p._maybe_auto_reboot({**_CFG, "fault_auto_reboot": False}, "k", "192.168.1.186")
    assert "disabled" in msg
    assert len(calls) == 0


def test_handle_fault_records_and_reboots(monkeypatch, tmp_path):
    p, calls = _poller(monkeypatch)
    p.storage = Storage(str(tmp_path / "t.db"))
    fault = {"code": "cli_socket_busy", "message": "socket busy"}
    p._handle_receiver_fault(_CFG, {"mac": "AA:BB:CC:DD:EE:FF"}, "192.168.1.186", fault, "screen text")

    assert len(calls) == 1  # rebooted
    pending = p.storage.unuploaded()
    assert len(pending) == 1
    assert pending[0]["fault"]["code"] == "cli_socket_busy"
    assert "issued (attempt 1)" in pending[0]["fault"]["action"]
    assert pending[0]["receiver"]["mac_address"] == "aa:bb:cc:dd:ee:ff"
    assert pending[0]["nodes"] == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
