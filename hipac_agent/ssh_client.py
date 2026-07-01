"""SSH into a receiver, drive the interactive ``receiver_cli`` TUI, and capture
its final rendered screen as clean text.

The CLI is a full-screen curses-style app that keeps redrawing while data
propagates (10-15s). We allocate a PTY, feed everything it emits into a ``pyte``
terminal emulator, wait for the data to settle, then read the emulator's final
display grid and send a quit sequence.
"""

import socket
import time

import paramiko
import pyte


class ReceiverUnreachable(Exception):
    """The host could not be reached / authenticated as a receiver."""


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
    """Return the rendered CLI screen as newline-joined text.

    Raises :class:`ReceiverUnreachable` for connection/auth failures so the
    poller can simply skip non-receiver hosts.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=user,
            key_filename=key_path,
            timeout=connect_timeout,
            banner_timeout=connect_timeout,
            auth_timeout=connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
    except (paramiko.SSHException, socket.error, EOFError) as exc:
        raise ReceiverUnreachable(f"{host}: {exc}") from exc

    try:
        chan = client.invoke_shell(term="xterm", width=cols, height=rows)
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

        # Best-effort clean exit from the TUI.
        for keys in ("q", "\x03"):  # 'q', then Ctrl-C
            try:
                chan.send(keys)
                time.sleep(0.2)
            except OSError:
                break

        return "\n".join(line.rstrip() for line in screen.display).strip("\n")
    finally:
        client.close()


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

    When ``expect_disconnect`` is True (reboot commands) a dropped connection or
    a missing exit status (-1) is treated as success — the receiver is on its way
    down, which is the desired outcome.

    Raises :class:`ReceiverUnreachable` for connection/auth failures.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=user,
            key_filename=key_path,
            timeout=connect_timeout,
            banner_timeout=connect_timeout,
            auth_timeout=connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
    except (paramiko.SSHException, socket.error, EOFError) as exc:
        raise ReceiverUnreachable(f"{host}: {exc}") from exc

    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=exec_timeout)
        try:
            code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", "replace")
            err = stderr.read().decode("utf-8", "replace")
        except (socket.timeout, EOFError, paramiko.SSHException):
            if expect_disconnect:
                return 0, "(connection closed after command; assumed success)", ""
            raise
        if code == -1 and expect_disconnect:
            return 0, out or "(rebooting)", err
        return code, out, err
    finally:
        try:
            client.close()
        except Exception:
            pass
