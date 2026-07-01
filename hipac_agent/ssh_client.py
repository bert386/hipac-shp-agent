"""SSH into a receiver, drive the interactive ``receiver_cli`` TUI, and capture
its final rendered screen — plus run one-shot maintenance commands.

Receivers differ in how they authenticate: some accept the ``none`` method (no
credential required), others require the receiver private key. We authenticate
like OpenSSH does — try ``none`` first, then fall back to publickey — so both
kinds work. The CLI itself is a full-screen curses app that keeps redrawing
while data propagates (10-20s); we feed its output into a ``pyte`` terminal
emulator and read the final display.
"""

import contextlib
import logging
import socket
import threading
import time

import paramiko
import pyte

from . import parser

log = logging.getLogger("hipac.ssh")


@contextlib.contextmanager
def _abort_after(transport, seconds: float):
    """Force ``transport`` closed after ``seconds`` — bounds SSH steps that
    otherwise have no timeout (auth, pty/shell requests), so a host that stalls
    can't hang the whole poll cycle. Any in-progress call then raises."""
    timer = threading.Timer(seconds, transport.close)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


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
        with _abort_after(transport, timeout * 2):
            transport.start_client(timeout=timeout)

            # 1) 'none' — receivers with open SSH (no credential required).
            try:
                transport.auth_none(user)
            except (paramiko.BadAuthenticationType, paramiko.SSHException):
                pass  # server wants a real method; fall through to publickey
            if transport.is_authenticated():
                return transport

            # 2) publickey with the configured key (receivers that require it).
            key = _load_key(key_path)
            if key is not None:
                try:
                    transport.auth_publickey(user, key)
                except paramiko.SSHException:
                    pass
            if transport.is_authenticated():
                return transport
    except (paramiko.SSHException, EOFError, OSError) as exc:
        transport.close()
        raise ReceiverUnreachable(f"{host}: handshake/auth failed ({exc})") from exc

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
    min_wait: int = 5,
    max_wait: int = 35,
    stable_seconds: int = 3,
    header_seconds: int = 12,
    connect_timeout: int = 15,
    cols: int = 200,
    rows: int = 60,
) -> str:
    """Capture the CLI screen, waiting until the node table has actually settled.

    Adaptive timing instead of a fixed wait:
      * wait at least ``min_wait`` before accepting anything,
      * give up early (host isn't a receiver) if the Receiver header hasn't
        rendered within ``header_seconds``,
      * return as soon as the node count stops growing for ``stable_seconds``
        (receivers showing zero nodes wait the full ``max_wait``, since nodes
        can take ~20s to paint after the header),
      * never wait longer than ``max_wait``.
    """
    transport = _authenticate(host, user, key_path, connect_timeout)
    # Backstop: force-close if any step wedges (the loop self-bounds at max_wait,
    # but open_session/get_pty/invoke_shell have no timeout of their own).
    guard = threading.Timer(max_wait + connect_timeout + 10, transport.close)
    guard.daemon = True
    guard.start()
    try:
        chan = transport.open_session(timeout=connect_timeout)
        chan.get_pty(term="xterm", width=cols, height=rows)
        chan.invoke_shell()
        chan.settimeout(1.0)
        _drain(chan, 0.6)  # swallow login banner / prompt

        screen = pyte.Screen(cols, rows)
        stream = pyte.ByteStream(screen)
        chan.send(command + "\n")

        start = time.time()
        seen_valid = False
        node_count = -1
        node_stable_since = start

        def render() -> str:
            return "\n".join(line.rstrip() for line in screen.display).strip("\n")

        while True:
            try:
                data = chan.recv(65536)
                if data:
                    stream.feed(data)
            except socket.timeout:
                pass

            now = time.time()
            elapsed = now - start
            if elapsed >= max_wait:
                break
            if elapsed < min_wait:
                continue

            text = render()
            if not parser.looks_like_receiver(text):
                # Not the receiver_cli screen; give a grace window then bail
                # (keeps non-receiver hosts from costing the full max_wait).
                if not seen_valid and elapsed >= header_seconds:
                    break
                continue

            seen_valid = True
            nc = len(parser.parse_nodes(text))
            if nc != node_count:
                node_count = nc
                node_stable_since = now

            # Complete only when the node list has settled AND the header has
            # loaded its real values (Radio Add. no longer "unknown"). The header
            # paints last, so stopping at node-settle alone loses the radio addr.
            # Receivers whose header never resolves fall through to max_wait.
            nodes_settled = (now - node_stable_since) >= stable_seconds
            if nodes_settled and parser.header_ready(text):
                break

        for keys in ("q", "\x03"):  # 'q', then Ctrl-C
            try:
                chan.send(keys)
                time.sleep(0.2)
            except OSError:
                break

        return render()
    finally:
        guard.cancel()
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
    guard = threading.Timer(exec_timeout + connect_timeout + 10, transport.close)
    guard.daemon = True
    guard.start()
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
        guard.cancel()
        transport.close()


def _read(stream) -> str:
    try:
        data = stream.read()
    except Exception:
        return ""
    return data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
