"""Allow-listed maintenance actions.

SECURITY: this is the single place that turns a server-provided action key +
parameters into a concrete shell command. The server never sends raw shell —
only an action name and validated params — and this module refuses anything not
on the list. Keep it dependency-free so it stays easy to audit and unit-test.
"""

import re

# datetime must be exactly YYYY-MM-DD HH:MM:SS (24h) — no shell metacharacters.
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


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
        # Fixed path only — never a caller-supplied path. Reboot after so the
        # receiver releases the (now-unlinked) file and reclaims the space.
        return "rm -f /persistent/log/log.dat && sync && reboot", True

    if action == "set_date":
        dt = str(params.get("datetime", ""))
        if not _DATETIME_RE.match(dt):
            raise ValueError(f"invalid datetime: {dt!r}")
        return f'date -s "{dt}" && hwclock -w && sync && reboot', True

    raise UnknownAction(action)
