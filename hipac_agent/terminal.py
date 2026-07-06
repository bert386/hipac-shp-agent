"""Self-provisioning in-browser terminal (ttyd + Tailscale Serve).

Every Pi running the agent can expose a tailnet-only web terminal that the
dashboard links to. This module owns the whole lifecycle so it ships with a
normal agent update — no per-Pi manual setup:

  * downloads the pinned ttyd static binary (verified by SHA256) to ~/bin/ttyd,
  * writes the ~/hipac-term.sh wrapper (a bare login shell, or an SSH hop to a
    receiver when the dashboard passes ``?arg=<receiver-ip>``),
  * runs ``tailscale serve --bg <port>`` so it is reachable over HTTPS on the
    tailnet only (ttyd itself binds to loopback, so it is never on the LAN),
  * supervises ttyd: (re)spawns it and restarts it if it dies.

Because the agent is a systemd service that starts at boot and restarts on
failure, supervising ttyd here makes the terminal reboot-persistent for free —
no separate systemd unit needed.

Nothing here requires root. The one privileged step, ``tailscale set
--operator``, is done once per update by agent-deploy.sh (which already runs as
root); until that has happened ``tailscale serve`` fails and we log a clear hint
rather than crashing.
"""

import hashlib
import logging
import os
import platform
import shlex
import subprocess
import threading
import time
import urllib.request

from . import config

log = logging.getLogger("hipac.terminal")

# Pinned ttyd release. Pinning the SHA256 digest — not merely the version —
# means a corrupted, truncated or swapped download is rejected instead of
# executed. Digests are from the release's official SHA256SUMS file.
_TTYD_VERSION = "1.7.7"
_TTYD_SHA256 = {
    "ttyd.aarch64": "b38acadd89d1d396a0f5649aa52c539edbad07f4bc7348b27b4f4b7219dd4165",
    "ttyd.armhf": "8240c8438b68d3b10b0e1a4e7c914d70fca6a7606b516f40bf40adfa1044d801",
    "ttyd.arm": "05eac1223914f18c65898d72c8d14e76bbb5435f7762c6dc7f16f041994a8109",
    "ttyd.x86_64": "8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55",
}
# platform.machine() (lowercased) -> ttyd release asset name.
_ARCH_ASSET = {
    "aarch64": "ttyd.aarch64",   # 64-bit Raspberry Pi OS (the norm)
    "arm64": "ttyd.aarch64",
    "armv7l": "ttyd.armhf",      # 32-bit Pi OS
    "armv6l": "ttyd.arm",        # Pi Zero / very old
    "x86_64": "ttyd.x86_64",     # dev boxes
    "amd64": "ttyd.x86_64",
}

# The wrapper ttyd runs. No arg -> a login shell on the Pi. One arg -> it must
# be a 192.168.x.x LAN address (the dashboard passes the receiver's IP via
# ?arg=), and we SSH-hop to it. ttyd is launched with ``-a`` so the client can
# supply that single argument; it arrives as $1 (a distinct argv element, never
# shell-evaluated), and we still validate it before use.
_WRAPPER_TEMPLATE = """#!/usr/bin/env bash
# Managed by hipac-agent (terminal.py) - regenerated on start; do not edit.
# No arg  -> interactive login shell on this Pi.
# One arg -> must be a 192.168.x.x LAN address; SSH-hop to that receiver.
set -euo pipefail
arg="${{1:-}}"
if [ -z "$arg" ]; then
  exec bash -l
fi
if printf '%s' "$arg" | grep -Eq '^192\\.168\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}$'; then
  exec ssh -t -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
    -i {key_path} {ssh_user}@"$arg"
fi
echo "Refusing '$arg' - only 192.168.x.x receiver addresses are allowed." >&2
exit 1
"""


def _home() -> str:
    return os.path.expanduser("~")


def _bin_path() -> str:
    return os.path.join(_home(), "bin", "ttyd")


def _wrapper_path() -> str:
    return os.path.join(_home(), "hipac-term.sh")


def _asset_for_arch() -> str | None:
    return _ARCH_ASSET.get(platform.machine().lower())


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_binary() -> str | None:
    """Return a path to a checksum-verified ttyd, downloading it if needed.

    Returns None (and logs) if this architecture has no pinned build or the
    download/verification fails — the caller then simply skips the terminal.
    """
    asset = _asset_for_arch()
    if not asset:
        log.warning("no pinned ttyd build for arch %r; terminal disabled", platform.machine())
        return None
    want = _TTYD_SHA256[asset]
    path = _bin_path()

    if os.path.exists(path):
        try:
            if _sha256(path) == want:
                return path
            log.info("ttyd at %s has an unexpected digest; re-downloading", path)
        except OSError:
            pass  # unreadable -> fall through and re-download

    os.makedirs(os.path.dirname(path), exist_ok=True)
    url = f"https://github.com/tsl0922/ttyd/releases/download/{_TTYD_VERSION}/{asset}"
    tmp = path + ".download"
    try:
        log.info("downloading ttyd %s (%s)", _TTYD_VERSION, asset)
        urllib.request.urlretrieve(url, tmp)  # noqa: S310 - fixed https URL
        got = _sha256(tmp)
        if got != want:
            log.error("ttyd checksum mismatch (got %s, want %s); refusing to use it", got, want)
            _unlink(tmp)
            return None
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
        log.info("installed ttyd -> %s", path)
        return path
    except OSError as exc:
        log.error("ttyd download failed: %s", exc)
        _unlink(tmp)
        return None


def write_wrapper(cfg: dict) -> str:
    """(Re)write ~/hipac-term.sh from config and mark it executable."""
    path = _wrapper_path()
    content = _WRAPPER_TEMPLATE.format(
        key_path=shlex.quote(cfg.get("ssh_key_path") or ""),
        ssh_user=shlex.quote(cfg.get("ssh_user") or "root"),
    )
    if not (os.path.exists(path) and _read(path) == content):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    os.chmod(path, 0o755)
    return path


def ensure_serve(port: int) -> bool:
    """Expose the loopback ttyd over HTTPS on the tailnet only. Idempotent.

    Returns False (with a warning) if Tailscale isn't installed or the operator
    grant hasn't been applied yet — ttyd still runs locally either way, and the
    next agent update (agent-deploy.sh) sets the operator so a later start
    succeeds.
    """
    try:
        proc = subprocess.run(
            ["tailscale", "serve", "--bg", str(port)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        log.warning("could not run `tailscale serve` (%s); terminal not exposed on tailnet", exc)
        return False
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        log.warning("`tailscale serve` failed (operator not set yet?): %s", err or "unknown error")
        return False
    log.info("ttyd exposed on the tailnet via `tailscale serve` (port %s)", port)
    return True


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _reap_stray(port: int) -> bool:
    """Kill any pre-existing ttyd on our port (e.g. one started by hand with
    nohup before the agent managed it) so we can take over the bind cleanly.
    Returns True if something was killed."""
    try:
        r = subprocess.run(
            ["pkill", "-f", f"ttyd -p {port}"],
            capture_output=True, timeout=10, check=False,
        )
        return r.returncode == 0
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return False


class TerminalServer(threading.Thread):
    """Owns the ttyd process + Tailscale Serve exposure and keeps ttyd alive."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        # Live health for the local web UI. Single-key mutations are atomic
        # under the GIL, so the web thread can read this without a lock.
        self.status = {
            "enabled": True,
            "supported": True,   # this arch has a pinned ttyd build
            "installed": False,  # binary present + checksum-verified
            "serving": False,    # exposed on the tailnet via `tailscale serve`
            "running": False,    # ttyd process alive right now
            "port": None,
            "detail": "starting",
        }

    def _set(self, **kw) -> None:
        self.status.update(kw)

    def stop(self) -> None:
        self._stop.set()
        self._terminate()

    def run(self) -> None:
        cfg = config.load()
        port = int(cfg.get("terminal_port", 7681))
        self._set(port=port)
        if not cfg.get("terminal_enabled", True):
            self._set(enabled=False, detail="disabled in config")
            log.info("in-browser terminal disabled (terminal_enabled=false)")
            return
        if not _asset_for_arch():
            self._set(supported=False, detail=f"no ttyd build for {platform.machine()}")
            log.warning("no pinned ttyd build for arch %r; terminal disabled", platform.machine())
            return
        binary = ensure_binary()
        if not binary:
            self._set(detail="ttyd download/verify failed")
            return
        self._set(installed=True)
        write_wrapper(cfg)
        if _reap_stray(port):
            time.sleep(1)  # let the old process release the port
        served = ensure_serve(port)  # best-effort; ttyd still runs if this fails
        self._set(serving=served)
        self._supervise(binary, port)

    # NOTE: keep custom method names clear of threading.Thread internals (e.g.
    # ``_handle`` on 3.13); see commands.py for the bug this avoids.
    def _supervise(self, binary: str, port: int) -> None:
        backoff = 2
        while not self._stop.is_set():
            started = time.monotonic()
            self._spawn(binary, port)
            self._set(running=True, detail=(
                "serving on tailnet" if self.status["serving"]
                else "ttyd running (local only — operator not set?)"))
            while self._proc.poll() is None and not self._stop.is_set():
                self._stop.wait(timeout=2)
            self._set(running=False)
            if self._stop.is_set():
                break
            ran = time.monotonic() - started
            # A ttyd that ran a good while then died is a fresh fault — reset the
            # backoff. A ttyd that dies instantly (e.g. port busy) is throttled.
            backoff = 2 if ran > 60 else min(backoff * 2, 60)
            self._set(detail=f"ttyd exited (code {self._proc.returncode}); retrying")
            log.warning("ttyd exited (code %s) after %.0fs; restarting in %ss",
                        self._proc.returncode, ran, backoff)
            self._stop.wait(timeout=backoff)
        self._terminate()
        self._set(running=False)

    def _spawn(self, binary: str, port: int) -> None:
        cmd = [binary, "-p", str(port), "-i", "lo", "-W", "-a", _wrapper_path()]
        log.info("starting ttyd: %s", " ".join(cmd))
        self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _terminate(self) -> None:
        proc = self._proc
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
