"""Tests for the command allow-list. Run: python tests/test_actions.py"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent.actions import UnknownAction, build_command  # noqa: E402


def test_reboot():
    assert build_command("reboot", {}) == ("sync && reboot", True)


def test_delete_log_is_fixed_path_and_reboots():
    cmd, disconnect = build_command("delete_log", {"path": "/etc/passwd"})
    # Caller path ignored; lists the log dir (proof it's empty) before rebooting.
    assert cmd == "rm -f /persistent/log/log.dat && sync && ls -lh /persistent/log/ && reboot"
    assert "ls -lh /persistent/log/" in cmd   # result captures `total 0`
    assert disconnect is True                  # reboots to release the file


def test_set_date_generates_current_utc_and_reboots():
    from datetime import datetime, timezone
    import re as _re

    cmd, disconnect = build_command("set_date", {})
    m = _re.search(r'date -s "(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"', cmd)
    assert m, f"expected a generated UTC timestamp in: {cmd!r}"
    gen = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    # Generated at call time → within a minute of now (UTC).
    assert abs((datetime.now(timezone.utc) - gen).total_seconds()) < 60
    assert "hwclock -w" in cmd and "reboot" in cmd
    assert disconnect is True


def test_set_date_ignores_supplied_params_no_injection():
    # There's no caller-supplied value any more, so a stray/hostile param can't
    # reach the shell command.
    cmd, _ = build_command("set_date", {"datetime": '"; rm -rf / #'})
    assert "rm -rf" not in cmd


def test_unknown_action():
    for bad in ["run_raw", "rm", "shutdown", ""]:
        try:
            build_command(bad, {})
            assert False, f"should have rejected {bad!r}"
        except UnknownAction:
            pass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("All action allow-list tests passed.")
