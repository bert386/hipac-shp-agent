"""Receiver clock read + skew. Run: python -m pytest, or directly."""

import os
import re
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent import poller  # noqa: E402

_CFG = {"ssh_user": "root", "ssh_key_path": "/k", "ssh_connect_timeout": 15}


def test_reads_epoch_and_computes_skew(monkeypatch):
    # Receiver reports ~1200s ahead of the Pi.
    monkeypatch.setattr(poller, "exec_receiver_command",
                        lambda **kw: (0, str(int(time.time()) + 1200), ""))
    p = poller.Poller(storage=None)
    clock_iso, skew = p._read_receiver_clock(_CFG, "192.168.1.186")
    assert 1195 <= skew <= 1205
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", clock_iso)


def test_in_sync_reads_near_zero_skew(monkeypatch):
    monkeypatch.setattr(poller, "exec_receiver_command",
                        lambda **kw: (0, str(int(time.time())), ""))
    p = poller.Poller(storage=None)
    _, skew = p._read_receiver_clock(_CFG, "192.168.1.186")
    assert abs(skew) <= 2


def test_read_failure_returns_none(monkeypatch):
    # Non-zero exit, garbage output, and a raising call all yield None (no crash).
    p = poller.Poller(storage=None)

    monkeypatch.setattr(poller, "exec_receiver_command", lambda **kw: (1, "", "boom"))
    assert p._read_receiver_clock(_CFG, "192.168.1.186") is None

    monkeypatch.setattr(poller, "exec_receiver_command", lambda **kw: (0, "not-a-number", ""))
    assert p._read_receiver_clock(_CFG, "192.168.1.186") is None

    def raise_it(**kw):
        raise OSError("dropped")

    monkeypatch.setattr(poller, "exec_receiver_command", raise_it)
    assert p._read_receiver_clock(_CFG, "192.168.1.186") is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
