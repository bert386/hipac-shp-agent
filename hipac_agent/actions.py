"""Allow-listed maintenance actions.

SECURITY: this is the single place that turns a server-provided action key +
parameters into a concrete shell command. The server never sends raw shell —
only an action name and validated params — and this module refuses anything not
on the list. Keep it dependency-free so it stays easy to audit and unit-test.
"""

from datetime import datetime, timezone


class UnknownAction(Exception):
    pass


def build_command(action: str, params: dict | None) -> tuple[str, bool]:
    """Return ``(shell_command, expect_disconnect)`` for an allow-listed action.

    ``expect_disconnect`` is True when the command reboots the receiver, so the
    caller treats a dropped SSH connection as success rather than failure.

    Raises :class:`UnknownAction` for anything not allow-listed and
    :class:`ValueError` for invalid parameters.
    """
    params = params or {}

    if action == "reboot":
        return "sync && reboot", True

    if action == "delete_log":
        # Fixed path only — never a caller-supplied path. List the log dir after
        # removing so the command result captures proof it's empty (`total 0`)
        # before the reboot; the reboot then releases the space. The `ls` output
        # is flushed over SSH before the reboot drops the connection.
        return "rm -f /persistent/log/log.dat && sync && ls -lh /persistent/log/ && reboot", True

    if action == "set_date":
        # Receivers run on UTC. Set them to the current UTC time, generated HERE
        # at execution (the Pi keeps accurate time via the network) so the clock
        # is "now" when it actually applies — never a stale value from when the
        # command was queued. No caller-supplied value: it removes the manual
        # entry field and, since the string is fixed-format digits, any shell
        # injection surface with it.
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        return f'date -s "{now}" && hwclock -w && sync && reboot', True

    raise UnknownAction(action)
