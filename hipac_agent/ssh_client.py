"""SSH into a receiver, drive the interactive ``receiver_cli`` TUI, and capture
its final rendered screen — plus run one-shot maintenance commands.

Receivers differ in how they authenticate: some accept the ``none`` method (no
credential required), others require the receiver private key. We authenticate
like OpenSSH does — try ``none`` first, then fall back to publickey — so both
kinds work. The CLI itself is a full-screen curses app that keeps redrawing
while data propagates (10-20s); we feed its output into a ``pyte`` terminal
emulator and read the final display.
"""

import logging
import socket
import time

import paramiko
import pyte

log = logging.getLogger("hipac.ssh")


class ReceiverUnreachable(Exception):
    """Could not reach the host (refused / timeout / not SSH)."""


class ReceiverAuthFailed(ReceiverUnreachable):
    """SSH is open but every auth method we tried was rejected."""


def _load_key(key_path: str):
    """Load a private key of any supported type, or return None if unavailable."""
    try:
        return paramiko.PKey.from_path(key_path)
    except Exception as exc:  # missing file, bad format, etc.
        log.debug("could not load key %s: %s", key_path, exc)
        return None


def _authenticate(host: str, user: str, key_path: str, timeout: int) -> paramiko.Transport:
    """Open an authenticated Transport, trying 'none' then publickey.

    Raises :class:`ReceiverUnreachable` if the host can't be reached and
    :class:`ReceiverAuthFailed` if SSH is open but auth is rejected.
    """
    try:
        sock = socket.create_connection((host, 22), timeout=timeout)
    except OSError as exc:
        raise ReceiverUnreachable(f"{host}: {exc}") from exc

    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
    except (paramiko.SSHException, EOFError) as exc:
        transport.close()
        raise ReceiverUnreachable(f"{host}: SSH handshake failed ({exc})") from exc

    # 1) 'none' — receivers with open SSH (no credential required).
    try:
        transport.auth_none(user)
        if transport.is_authenticated():
            return transport
    except paramiko.BadAuthenticationType:
        pass  # server wants a real method; fall through to publickey
    except paramiko.SSHException:
        pass

    # 2) publickey with the configured key (receivers that require it).
    key = _load_key(key_path)
    if key is not None:
        try:
            transport.auth_publickey(user, key)
            if transport.is_authenticated():
                return transport
        except paramiko.SSHException:
            pass

    transport.close()
    raise ReceiverAuthFailed(f"{host}: authentication rejected (tried none + publickey)")


def _drain(chan, seconds: float = 0.5) -> None:
    end = time.time() + seconds
    while time.time() < end:
        if chan.recv_ready():
            chan.recv(65536)
        else:
            time.sleep(0.05)


def capture_receiver_cli(
    host: str,
    user: str,
    key_path: str,
    command: str,
    wait_seconds: int = 15,
    connect_timeout: int = 15,
    cols: int = 200,
    rows: int = 60,
) -> str:
    """Return the rendered CLI screen as newline-joined text."""
    transport = _authenticate(host, user, key_path, connect_timeout)
    try:
        chan = transport.open_session(timeout=connect_timeout)
        chan.get_pty(term="xterm", width=cols, height=rows)
        chan.invoke_shell()
        chan.settimeout(1.0)
        _drain(chan, 0.6)  # swallow login banner / prompt

        screen = pyte.Screen(cols, rows)
        stream = pyte.ByteStream(screen)

        chan.send(command + "\n")

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                data = chan.recv(65536)
                if data:
                    stream.feed(data)
            except socket.timeout:
                pass

        for keys in ("q", "\x03"):  # 'q', then Ctrl-C
            try:
                chan.send(keys)
                time.sleep(0.2)
            except OSError:
                break

        return "\n".join(line.rstrip() for line in screen.display).strip("\n")
    finally:
        transport.close()


def exec_receiver_command(
    host: str,
    user: str,
    key_path: str,
    command: str,
    connect_timeout: int = 15,
    exec_timeout: int = 30,
    expect_disconnect: bool = False,
) -> tuple[int, str, str]:
    """Run a one-shot command over SSH; return ``(exit_code, stdout, stderr)``.

    When ``expect_disconnect`` is True (reboot), a dropped connection or missing
    exit status is treated as success.
    """
    transport = _authenticate(host, user, key_path, connect_timeout)
    try:
        chan = transport.open_session(timeout=connect_timeout)
        chan.settimeout(exec_timeout)
        chan.exec_command(command)

        if expect_disconnect:
            deadline = time.time() + 5
            while time.time() < deadline and not chan.exit_status_ready():
                time.sleep(0.2)
            if chan.exit_status_ready():
                return chan.recv_exit_status(), _read(chan.makefile("rb")), _read(chan.makefile_stderr("rb"))
            return 0, "(reboot issued; connection dropping)", ""

        try:
            out = _read(chan.makefile("rb"))
            err = _read(chan.makefile_stderr("rb"))
            code = chan.recv_exit_status()
        except (socket.timeout, EOFError, paramiko.SSHException) as exc:
            raise ReceiverUnreachable(f"{host}: {exc}") from exc
        return code, out, err
    finally:
        transport.close()


def _read(stream) -> str:
    try:
        data = stream.read()
    except Exception:
        return ""
    return data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
