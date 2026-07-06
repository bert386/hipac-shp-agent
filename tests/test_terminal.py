"""terminal.py tests. Run: python -m pytest, or python tests/test_terminal.py

These avoid the network and any real Pi: the checksum-verified "already present"
path is exercised by pointing the module at a temp home and setting the pinned
digest to the temp file's own hash.
"""

import hashlib
import os
import stat
import sys
import threading

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent import terminal  # noqa: E402


# -- small helpers ---------------------------------------------------------
class _Home:
    """Point terminal._home() at a temp dir and restore it after."""

    def __init__(self, tmp):
        self.tmp = str(tmp)
        self._orig = terminal._home

    def __enter__(self):
        terminal._home = lambda: self.tmp
        return self

    def __exit__(self, *a):
        terminal._home = self._orig


def _write_tmp(path, data=b"\x7fELF fake ttyd"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()


# -- arch mapping ----------------------------------------------------------
def test_arch_map_covers_pi_and_devboxes(monkeypatch):
    cases = {
        "aarch64": "ttyd.aarch64",
        "arm64": "ttyd.aarch64",
        "armv7l": "ttyd.armhf",
        "armv6l": "ttyd.arm",
        "x86_64": "ttyd.x86_64",
    }
    for machine, asset in cases.items():
        monkeypatch.setattr(terminal.platform, "machine", lambda m=machine: m)
        assert terminal._asset_for_arch() == asset
        # every mapped asset must have a pinned digest
        assert asset in terminal._TTYD_SHA256


def test_unknown_arch_disables_terminal(monkeypatch):
    monkeypatch.setattr(terminal.platform, "machine", lambda: "sparc64")
    assert terminal._asset_for_arch() is None
    assert terminal.ensure_binary() is None  # no crash, just None


# -- checksum gate ---------------------------------------------------------
def test_ensure_binary_accepts_matching_digest_without_download(tmp_path, monkeypatch):
    with _Home(tmp_path):
        digest = _write_tmp(terminal._bin_path())
        monkeypatch.setattr(terminal.platform, "machine", lambda: "aarch64")
        monkeypatch.setitem(terminal._TTYD_SHA256, "ttyd.aarch64", digest)
        # If it tried to download we'd blow up — force that to be obvious.
        monkeypatch.setattr(terminal.urllib.request, "urlretrieve",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download")))
        assert terminal.ensure_binary() == terminal._bin_path()


def test_ensure_binary_rejects_bad_download(tmp_path, monkeypatch):
    with _Home(tmp_path):
        monkeypatch.setattr(terminal.platform, "machine", lambda: "aarch64")
        # Digest we expect will NOT match whatever the "download" writes.
        monkeypatch.setitem(terminal._TTYD_SHA256, "ttyd.aarch64", "0" * 64)

        def fake_download(url, dest):
            with open(dest, "wb") as f:
                f.write(b"tampered")

        monkeypatch.setattr(terminal.urllib.request, "urlretrieve", fake_download)
        assert terminal.ensure_binary() is None
        # the rejected temp file must not be left lying around
        assert not os.path.exists(terminal._bin_path() + ".download")
        assert not os.path.exists(terminal._bin_path())


# -- wrapper ---------------------------------------------------------------
def test_wrapper_is_executable_and_scoped(tmp_path):
    with _Home(tmp_path):
        path = terminal.write_wrapper({"ssh_key_path": "/home/pi/.ssh/receiver_private_key", "ssh_user": "root"})
        body = terminal._read(path)
        # SSH hop uses our key + user, and only to validated LAN addresses.
        assert "/home/pi/.ssh/receiver_private_key" in body
        assert "root@" in body
        assert "192\\.168\\." in body            # regex guard present
        assert "exec bash -l" in body            # bare-shell path
        # executable bit set (chmod is a no-op on Windows, so only assert on posix)
        if os.name == "posix":
            assert os.stat(path).st_mode & stat.S_IXUSR


def test_wrapper_rewrite_is_stable(tmp_path):
    with _Home(tmp_path):
        cfg = {"ssh_key_path": "/k", "ssh_user": "root"}
        p1 = terminal.write_wrapper(cfg)
        first = terminal._read(p1)
        p2 = terminal.write_wrapper(cfg)
        assert terminal._read(p2) == first


# -- disabled no-op --------------------------------------------------------
def test_disabled_is_a_noop(monkeypatch):
    monkeypatch.setattr(terminal.config, "load", lambda: {"terminal_enabled": False})
    # Should return immediately without touching ttyd/serve.
    monkeypatch.setattr(terminal, "ensure_binary",
                        lambda: (_ for _ in ()).throw(AssertionError("must not provision")))
    terminal.TerminalServer().run()


# -- Thread-internal name-collision guard (same class of bug as commands.py) --
def test_methods_do_not_shadow_thread_internals():
    reserved = set(dir(threading.Thread()))
    reserved.discard("run")
    ours = {n for n in vars(terminal.TerminalServer)
            if not n.startswith("__") and callable(getattr(terminal.TerminalServer, n))}
    clash = ours & reserved
    assert not clash, f"TerminalServer methods shadow Thread internals: {sorted(clash)}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
