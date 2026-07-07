"""Heartbeat behaviour. Run: python -m pytest, or directly."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent import heartbeat as hb  # noqa: E402

_ENABLED = {
    "heartbeat_enabled": True, "heartbeat_seconds": 60,
    "server_url": "http://server", "api_token": "t", "site_name": "S",
}


class _FakePoller:
    def __init__(self, running=False):
        self.status = {"running": running}


def test_sends_when_idle(monkeypatch):
    sent = []
    monkeypatch.setattr(hb.config, "load", lambda: dict(_ENABLED))
    monkeypatch.setattr(hb.pusher, "heartbeat", lambda *a, **k: sent.append(a) or True)
    assert hb.Heartbeat(_FakePoller(running=False)).beat_once() is True
    assert len(sent) == 1
    assert sent[0] == ("http://server", "t", "S")


def test_skips_while_scanning(monkeypatch):
    sent = []
    monkeypatch.setattr(hb.config, "load", lambda: dict(_ENABLED))
    monkeypatch.setattr(hb.pusher, "heartbeat", lambda *a, **k: sent.append(a) or True)
    assert hb.Heartbeat(_FakePoller(running=True)).beat_once() is False
    assert sent == []   # the scan's own uploads keep the server fresh


def test_disabled_sends_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(hb.config, "load", lambda: {"heartbeat_enabled": False})
    monkeypatch.setattr(hb.pusher, "heartbeat", lambda *a, **k: called.append(1) or True)
    assert hb.Heartbeat(_FakePoller()).beat_once() is False
    assert called == []


def test_works_without_a_poller(monkeypatch):
    sent = []
    monkeypatch.setattr(hb.config, "load", lambda: dict(_ENABLED))
    monkeypatch.setattr(hb.pusher, "heartbeat", lambda *a, **k: sent.append(a) or True)
    assert hb.Heartbeat(poller=None).beat_once() is True
    assert len(sent) == 1


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
