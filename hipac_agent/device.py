"""Collect basic Raspberry Pi health stats to report to the dashboard."""

import os
import shutil

from . import __version__


def stats() -> dict:
    """Return agent version + host uptime / load / disk (best-effort)."""
    info = {"version": __version__}

    try:
        with open("/proc/uptime", encoding="ascii") as f:
            info["uptime_seconds"] = int(float(f.read().split()[0]))
    except (OSError, ValueError):
        pass

    try:
        info["load_1m"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass

    try:
        usage = shutil.disk_usage("/")
        info["disk_free"] = usage.free
        info["disk_total"] = usage.total
    except OSError:
        pass

    return info
