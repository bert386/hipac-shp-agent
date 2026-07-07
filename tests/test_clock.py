"""Receiver vitals (clock + health) read. Run: python -m pytest, or directly."""

import os
import re
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent import poller  # noqa: E402

_CFG = {"ssh_user": "root", "ssh_key_path": "/k", "ssh_connect_timeout": 15}


def _out(epoch="", uptime="", load="", mt="", ma="", mf="", df="", log=""):
    """Build the receiver's key=value vitals output (mem as real /proc/meminfo lines)."""
    def mem(label, v):
        return f"{label}:  {v} kB" if v != "" else ""
    return (f"E={epoch}\nU={uptime}\nL={load}\n"
            f"MT={mem('MemTotal', mt)}\nMA={mem('MemAvailable', ma)}\nMF={mem('MemFree', mf)}\n"
            f"D={df}\nG={log}\n")


def _fake(out):
    return lambda **kw: (0, out, "")


def test_reads_epoch_and_computes_skew(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(poller, "exec_receiver_command", _fake(_out(epoch=now + 1200)))
    v = poller.Poller(storage=None)._read_receiver_vitals(_CFG, "192.168.1.186")
    assert 1195 <= v["clock_skew_seconds"] <= 1205
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", v["clock_time"])


def test_parses_health_vitals(monkeypatch):
    now = int(time.time())
    df = "/dev/root 100000 88000 12000 88% /persistent"
    monkeypatch.setattr(poller, "exec_receiver_command",
                        _fake(_out(epoch=now, uptime=1960, load="1.31", mt=512000, ma=51200, df=df, log=780)))
    v = poller.Poller(storage=None)._read_receiver_vitals(_CFG, "192.168.1.186")
    h = v["health"]
    assert h["uptime_seconds"] == 1960
    assert h["load_1m"] == 1.31
    assert h["mem_pct"] == 10                 # 51200 / 512000
    assert h["persistent_used_pct"] == 88     # from the df Use% token
    assert h["log_bytes"] == 780


def test_mem_pct_falls_back_to_memfree(monkeypatch):
    # Older kernels expose no MemAvailable — fall back to MemFree.
    now = int(time.time())
    monkeypatch.setattr(poller, "exec_receiver_command",
                        _fake(_out(epoch=now, mt=500000, ma="", mf=125000)))
    v = poller.Poller(storage=None)._read_receiver_vitals(_CFG, "192.168.1.186")
    assert v["health"]["mem_pct"] == 25       # 125000 / 500000


def test_partial_output_keeps_good_fields_drops_bad(monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(poller, "exec_receiver_command",
                        _fake(_out(epoch=now, uptime=100, log=0)))   # no mem / df
    v = poller.Poller(storage=None)._read_receiver_vitals(_CFG, "192.168.1.186")
    assert v["health"]["uptime_seconds"] == 100
    assert v["health"]["log_bytes"] == 0       # a zero-byte (freshly cleared) log is kept
    assert "mem_pct" not in v["health"]
    assert "persistent_used_pct" not in v["health"]


def test_read_failure_returns_none(monkeypatch):
    p = poller.Poller(storage=None)

    monkeypatch.setattr(poller, "exec_receiver_command", lambda **kw: (1, "", "boom"))
    assert p._read_receiver_vitals(_CFG, "192.168.1.186") is None

    def raise_it(**kw):
        raise OSError("dropped")

    monkeypatch.setattr(poller, "exec_receiver_command", raise_it)
    assert p._read_receiver_vitals(_CFG, "192.168.1.186") is None


def test_all_blank_output_returns_none(monkeypatch):
    monkeypatch.setattr(poller, "exec_receiver_command", _fake(_out()))
    assert poller.Poller(storage=None)._read_receiver_vitals(_CFG, "192.168.1.186") is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
