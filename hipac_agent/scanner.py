"""Wrapper around ``arp-scan`` to discover devices on the local subnet."""

import re
import subprocess

# Matches lines like: "192.168.1.114\t3c:18:a0:23:ac:d7\t(Unknown: vendor)"
_LINE = re.compile(
    r"^(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\s+"
    r"(?P<mac>[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})\s*"
    r"(?P<vendor>.*)$"
)


def parse_arp_output(stdout: str) -> list[dict]:
    devices = {}
    for line in stdout.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        ip = m.group("ip")
        # arp-scan can list the same host twice; keep the first hit.
        devices.setdefault(
            ip,
            {
                "ip": ip,
                "mac": m.group("mac").lower(),
                "vendor": m.group("vendor").strip(),
            },
        )
    return list(devices.values())


def scan(
    interface: str,
    subnet: str,
    use_sudo: bool = True,
    timeout: int = 120,
    retries: int = 5,
    backoff: float = 2.0,
) -> list[dict]:
    """Run arp-scan and return a list of ``{ip, mac, vendor}`` dicts.

    ``retries``/``backoff`` make discovery reliable on flaky networks — arp-scan
    is best-effort and drops slow-responding hosts with its default 2 retries,
    which is how a site's receivers can go missing from a scan entirely.

    Raises ``FileNotFoundError`` if arp-scan is not installed and
    ``subprocess.TimeoutExpired`` if it hangs.
    """
    cmd = []
    if use_sudo:
        cmd.append("sudo")
    cmd += [
        "arp-scan",
        f"--retry={int(retries)}",
        f"--backoff={backoff}",
        "-I", interface, subnet,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )
    return parse_arp_output(proc.stdout)
