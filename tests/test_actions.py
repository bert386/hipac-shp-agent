"""Tests for the command allow-list. Run: python tests/test_actions.py"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hipac_agent.actions import UnknownAction, build_command  # noqa: E402


def test_reboot():
    assert build_command("reboot", {}) == ("sync && reboot", True)


def test_delete_log_is_fixed_path():
    cmd, disconnect = build_command("delete_log", {"path": "/etc/passwd"})
    assert cmd == "rm -f /persistent/log/log.dat"   # caller path ignored
    assert disconnect is False


def test_set_date_valid():
    cmd, disconnect = build_command("set_date", {"datetime": "2026-05-20 14:30:00"})
    assert 'date -s "2026-05-20 14:30:00"' in cmd
    assert "hwclock -w" in cmd and "reboot" in cmd
    assert disconnect is True


def test_set_date_rejects_bad_format():
    for bad in ["garbage", "2026-5-20 14:30:00", "", "2026-05-20"]:
        try:
            build_command("set_date", {"datetime": bad})
            assert False, f"should have rejected {bad!r}"
        except ValueError:
            pass


def test_set_date_blocks_shell_injection():
    # A classic injection attempt must be refused by the strict format check.
    try:
        build_command("set_date", {"datetime": '2026-05-20 14:30:00"; rm -rf / #'})
        assert False, "injection payload should be rejected"
    except ValueError:
        pass


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
