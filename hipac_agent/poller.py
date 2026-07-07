"""Background poller: scan the subnet, capture each receiver, store, upload.

Runs on its own thread. Sleeps between cycles for the configured interval but
wakes early if the web UI requests an immediate scan ("Scan now").
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone

from . import config, parser, pusher, scanner
from .actions import build_command
from .ssh_client import (
    ReceiverAuthFailed,
    ReceiverUnreachable,
    capture_receiver_cli,
    exec_receiver_command,
)
from .storage import Storage

log = logging.getLogger("hipac.poller")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- vitals parsing helpers (tolerant: bad/blank input -> None) ---------------
def _to_int(s: str | None) -> int | None:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def _to_float(s: str | None) -> float | None:
    try:
        return round(float(str(s).strip()), 2)
    except (TypeError, ValueError):
        return None


def _first_int(s: str | None) -> int | None:
    """First run of digits in a string, e.g. 'MemTotal:  503260 kB' -> 503260."""
    if not s:
        return None
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _mem_pct(total: str | None, avail: str | None, free: str | None) -> int | None:
    """Percent of memory available, from /proc/meminfo lines (kB). Falls back to
    MemFree on older kernels that don't expose MemAvailable."""
    t = _first_int(total)
    a = _first_int(avail)
    if a is None:
        a = _first_int(free)
    if not t or a is None:
        return None
    return max(0, min(100, round(a / t * 100)))


def _df_used_pct(df_line: str | None) -> int | None:
    """Extract the Use% from a `df -k` data line (the token ending in '%')."""
    if not df_line:
        return None
    for tok in df_line.split():
        if tok.endswith("%"):
            return _to_int(tok.rstrip("%"))
    return None


class Poller(threading.Thread):
    def __init__(self, storage: Storage):
        super().__init__(daemon=True)
        self.storage = storage
        self._wake = threading.Event()   # set to trigger an immediate cycle
        self._stop = threading.Event()
        self._upload_lock = threading.Lock()  # serialise auto + manual uploads
        # Per-receiver auto-reboot budget (key -> {"at": monotonic, "count": n}),
        # so a stuck receiver is rebooted to recover but can't reboot-loop.
        self._fault_reboots: dict[str, dict] = {}
        self.status = {
            "running": False,
            "current_ip": None,
            "last_run": None,
            "last_found": 0,
            "last_error": None,
            "pending_upload": 0,
            "last_upload": None,
            "last_upload_count": 0,
            # Live scan progress (also pushed to the server each upload).
            "scan_active": False,
            "scan_total": 0,
            "scan_done": 0,
            "scan_current": None,
            "scan_started_at": None,
            "scan_finished_at": None,
        }

    # -- control -----------------------------------------------------------
    def trigger_now(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_cycle()
            except Exception as exc:  # never let the loop die
                log.exception("poll cycle failed")
                self.status["last_error"] = str(exc)
            interval_min = max(1, int(config.load().get("poll_interval_minutes", 60)))
            self._wake.wait(timeout=interval_min * 60)
            self._wake.clear()

    def run_cycle(self) -> dict:
        cfg = config.load()
        self.status.update(running=True, current_ip=None, last_error=None,
                           scan_active=True, scan_total=0, scan_done=0,
                           scan_current=None, scan_started_at=_now_iso(),
                           scan_finished_at=None)
        found = 0
        try:
            devices = scanner.scan(
                cfg["interface"], cfg["subnet"],
                use_sudo=cfg.get("use_sudo", True),
                timeout=int(cfg.get("arp_scan_timeout", 120)),
                retries=int(cfg.get("arp_scan_retries", 5)),
            )
            # Backstop for flaky arp-scan: also poll receivers we've recorded
            # before, at their last-known IP, even if this scan missed them.
            seen_ips = {d["ip"] for d in devices}
            for result in self.storage.latest_per_receiver():
                rec = result.get("receiver", {})
                kip = rec.get("ip_address") or result.get("source_ip")
                if kip and kip not in seen_ips:
                    devices.append({"ip": kip, "mac": rec.get("mac_address"), "vendor": "(known)"})
                    seen_ips.add(kip)

            excluded = set(cfg.get("excluded_ips") or [])
            targets = [d for d in devices if d["ip"] not in excluded]
            log.info("scan found %d devices (incl. known), %d after exclusions", len(devices), len(targets))

            self.status["scan_total"] = len(targets)
            self._push_progress(cfg)  # start ping: 0/total, active

            for i, dev in enumerate(targets):
                if self._stop.is_set():
                    break
                ip = dev["ip"]
                self.status["current_ip"] = ip
                self.status["scan_current"] = ip
                log.info("scanning %s", ip)
                if self._scan_one(cfg, dev, ip):
                    found += 1
                self.status["scan_done"] = i + 1
                # Incremental upload: push this receiver's result (if any) plus
                # live progress, so the dashboard advances receiver-by-receiver
                # instead of jumping only at the end of the cycle.
                self._push_progress(cfg)
        finally:
            self.status.update(running=False, current_ip=None,
                               last_run=_now_iso(), last_found=found,
                               scan_active=False, scan_current=None,
                               scan_finished_at=_now_iso())
            self._push_progress(cfg)  # final flush + "scan complete"

        # Keep the local DB bounded: trim old uploaded results per receiver.
        try:
            removed = self.storage.prune(int(cfg.get("results_keep_per_receiver", 200)))
            if removed:
                log.info("pruned %d old local results", removed)
        except Exception:
            log.exception("prune failed")

        return self.status

    def _scan_one(self, cfg: dict, dev: dict, ip: str) -> bool:
        """Capture, parse and store a single host. Returns True if a receiver
        was recorded, False if the host was skipped (not a receiver / error)."""
        cli_wait = int(cfg.get("cli_wait_seconds", 15))
        max_wait = max(int(cfg.get("cli_max_wait_seconds", 60)), cli_wait)
        t0 = time.monotonic()
        try:
            screen = capture_receiver_cli(
                host=ip,
                user=cfg["ssh_user"],
                key_path=cfg["ssh_key_path"],
                command=cfg["cli_command"],
                min_wait=min(int(cfg.get("cli_min_wait_seconds", 10)), max_wait),
                max_wait=max_wait,
                stable_seconds=int(cfg.get("cli_stable_seconds", 8)),
                header_seconds=int(cfg.get("cli_header_seconds", 15)),
                blank_seconds=int(cfg.get("cli_blank_seconds", 25)),
                connect_timeout=int(cfg.get("ssh_connect_timeout", 15)),
                cols=int(cfg.get("term_cols", 200)),
                rows=int(cfg.get("term_rows", 60)),
            )
        except ReceiverAuthFailed as exc:
            # SSH is open but every method was rejected — likely a real
            # receiver with a credential mismatch. Surface it.
            log.warning("auth rejected at %s: %s", ip, exc)
            return False
        except ReceiverUnreachable:
            return False  # not a receiver / refused / timeout -> skip quietly
        except Exception as exc:
            log.warning("capture failed for %s: %s", ip, exc)
            return False

        parsed = parser.parse_screen(screen)
        if not parser.is_valid_receiver(parsed):
            # SSH worked but the CLI didn't render a receiver. If it printed a
            # known receiver-side fault (e.g. a stuck socket), log it to the card
            # and reboot the receiver to clear it — next pass captures normally.
            fault = parser.detect_cli_fault(screen)
            if fault:
                self._handle_receiver_fault(cfg, dev, ip, fault, screen)
            elif parser.is_blank_receiver(screen):
                # Receiver_cli is drawn but the receiver knows nothing (own
                # identity unknown, 0 nodes) — a stuck state a reboot doesn't
                # reliably clear. Record a SKIP (no auto-reboot) so the dashboard
                # shows it was skipped this poll and why.
                self._handle_blank_receiver(cfg, dev, ip)
            elif screen.strip():
                # Something rendered but no receiver data — usually a
                # non-receiver, or a receiver that didn't paint in time.
                log.info("no valid receiver data at %s (%d chars captured)", ip, len(screen))
            return False

        # Confirmed a receiver. Some report their own MAC/IP as "unknown";
        # backfill from the arp-scan result so the sticky identity is stable.
        recv = parsed.setdefault("receiver", {})
        if not recv.get("mac_address") and dev.get("mac"):
            recv["mac_address"] = dev["mac"]
        if not recv.get("ip_address"):
            recv["ip_address"] = ip

        # Read the receiver's clock + health vitals (one SSH round-trip).
        if cfg.get("report_receiver_clock", True):
            vitals = self._read_receiver_vitals(cfg, ip)
            if vitals:
                if "clock_time" in vitals:
                    recv["clock_time"] = vitals["clock_time"]
                    recv["clock_skew_seconds"] = vitals["clock_skew_seconds"]
                if vitals.get("health"):
                    recv["health"] = vitals["health"]

        self.storage.save_result(parsed, screen, _now_iso(), ip)
        # Recovered: reset this receiver's auto-reboot budget.
        self._fault_reboots.pop(self._fault_key(recv.get("mac_address"), ip), None)
        dur = time.monotonic() - t0
        capped = " (hit max_wait)" if dur >= max_wait - 0.5 else ""
        log.info("recorded receiver at %s (%d nodes) in %.0fs%s", ip, len(parsed["nodes"]), dur, capped)
        return True

    @staticmethod
    def _fault_key(mac: str | None, ip: str) -> str:
        return (mac or "").lower() or ip

    # One SSH round-trip that returns the receiver's clock + health vitals, all
    # from tools baked into the receiver's BusyBox/Buildroot image (no installs).
    # Emits `key=value` lines so a missing/failed field doesn't break the others.
    _VITALS_CMD = (
        "echo E=$(date -u +%s 2>/dev/null); "
        "echo U=$(cut -d. -f1 /proc/uptime 2>/dev/null); "
        "echo L=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null); "
        "echo MT=$(grep '^MemTotal:' /proc/meminfo 2>/dev/null); "
        "echo MA=$(grep '^MemAvailable:' /proc/meminfo 2>/dev/null); "
        "echo MF=$(grep '^MemFree:' /proc/meminfo 2>/dev/null); "
        "echo D=$(df -k /persistent 2>/dev/null | tail -1); "
        "echo G=$(wc -c < /persistent/log/log.dat 2>/dev/null)"
    )

    def _read_receiver_vitals(self, cfg: dict, ip: str) -> dict | None:
        """Read the receiver's clock + health (uptime/load/mem/disk/log size) in
        a single SSH command. Returns a dict with optional ``clock_time`` +
        ``clock_skew_seconds`` and an optional ``health`` sub-dict, or None if the
        read failed entirely. Never raises — vitals must not fail a scan."""
        try:
            pi_now = time.time()
            code, out, _ = exec_receiver_command(
                host=ip, user=cfg["ssh_user"], key_path=cfg["ssh_key_path"],
                command=self._VITALS_CMD,
                connect_timeout=int(cfg.get("ssh_connect_timeout", 15)),
            )
            if code != 0:
                return None
        except Exception as exc:  # noqa: BLE001
            log.info("vitals read failed for %s: %s", ip, exc)
            return None

        vals = {}
        for line in (out or "").splitlines():
            k, sep, v = line.partition("=")
            if sep:
                vals[k.strip()] = v.strip()

        result: dict = {}

        # Clock: epoch is TZ-independent; skew = receiver − Pi's true UTC.
        epoch = _to_int(vals.get("E"))
        if epoch is not None:
            result["clock_time"] = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            result["clock_skew_seconds"] = int(round(epoch - pi_now))

        health = {
            "uptime_seconds": _to_int(vals.get("U")),
            "load_1m": _to_float(vals.get("L")),
            "mem_pct": _mem_pct(vals.get("MT"), vals.get("MA"), vals.get("MF")),
            "persistent_used_pct": _df_used_pct(vals.get("D")),
            "log_bytes": _to_int(vals.get("G")),
        }
        health = {k: v for k, v in health.items() if v is not None}
        if health:
            result["health"] = health

        return result or None

    def _handle_receiver_fault(self, cfg: dict, dev: dict, ip: str, fault: dict, screen: str) -> None:
        """Record a known receiver-side CLI fault and (cooldown-permitting)
        reboot the receiver to clear it."""
        mac = (dev.get("mac") or "").lower()
        action = self._maybe_auto_reboot(cfg, self._fault_key(mac, ip), ip)
        log.warning("receiver fault at %s: %s — %s", ip, fault["message"], action)
        if mac:
            self.storage.save_fault(
                {"mac_address": mac, "ip_address": ip},
                {**fault, "action": action},
                _now_iso(), ip, raw_screen=screen[:4000],
            )
        else:
            log.warning("fault at %s not reported (no MAC to key it)", ip)

    def _handle_blank_receiver(self, cfg: dict, dev: dict, ip: str) -> None:
        """Record a stuck/blank receiver as a SKIP (no auto-reboot — a soft
        reboot doesn't clear this state). Shows on the dashboard as skipped."""
        mac = (dev.get("mac") or "").lower()
        log.info("skipping %s: receiver CLI blank (no identity, 0 nodes)", ip)
        if mac:
            self.storage.save_fault(
                {"mac_address": mac, "ip_address": ip},
                {
                    "code": "cli_blank",
                    "message": "Receiver CLI returned no data (blank)",
                    "action": "Skipped this poll — needs recovery (delete-log/reboot or power-cycle)",
                },
                _now_iso(), ip, raw_screen="",
            )

    def _maybe_auto_reboot(self, cfg: dict, key: str, ip: str) -> str:
        """Reboot the receiver if allowed by config + the per-receiver budget.
        Returns a short human description of what was done (goes on the card)."""
        if not cfg.get("fault_auto_reboot", True):
            return "auto-reboot disabled"
        cooldown = int(cfg.get("fault_reboot_cooldown_seconds", 1800))
        max_attempts = int(cfg.get("fault_reboot_max_attempts", 3))
        now = time.monotonic()
        entry = self._fault_reboots.get(key, {"at": 0.0, "count": 0})

        if entry["count"] and (now - entry["at"]) < cooldown:
            return f"in cooldown (auto-rebooted {int(now - entry['at'])}s ago)"
        if entry["count"] >= max_attempts:
            return f"needs manual attention (auto-rebooted {entry['count']}× without recovery)"

        attempt = entry["count"] + 1
        command, expect_disconnect = build_command("reboot", {})
        try:
            exec_receiver_command(
                host=ip, user=cfg["ssh_user"], key_path=cfg["ssh_key_path"],
                command=command, connect_timeout=int(cfg.get("ssh_connect_timeout", 15)),
                expect_disconnect=expect_disconnect,
            )
            result = f"auto-reboot issued (attempt {attempt})"
        except Exception as exc:  # noqa: BLE001 - reboot must never crash the scan
            log.warning("auto-reboot of %s failed: %s", ip, exc)
            result = f"auto-reboot failed: {exc}"
        # Count the attempt either way so an unreachable box backs off too.
        self._fault_reboots[key] = {"at": now, "count": attempt}
        return result

    def _scan_payload(self) -> dict:
        s = self.status
        return {
            "active": bool(s["scan_active"]),
            "total": int(s["scan_total"]),
            "done": int(s["scan_done"]),
            "current": s["scan_current"],
            "started_at": s["scan_started_at"],
            "finished_at": s["scan_finished_at"],
        }

    def _push_progress(self, cfg: dict) -> None:
        """Upload any pending results together with the live scan progress."""
        try:
            self.upload_pending(cfg, scan=self._scan_payload())
        except Exception:
            log.exception("progress upload failed")

        # Keep the local DB bounded: trim old uploaded results per receiver.
        try:
            removed = self.storage.prune(int(cfg.get("results_keep_per_receiver", 200)))
            if removed:
                log.info("pruned %d old local results", removed)
        except Exception:
            log.exception("prune failed")

        return self.status

    def upload_pending(self, cfg: dict | None = None, scan: dict | None = None) -> int:
        # Only one upload at a time — a manual "Upload now" must not race the
        # per-receiver / end-of-cycle uploads and re-post the same readings.
        if not self._upload_lock.acquire(blocking=False):
            log.info("upload already in progress; skipping")
            return 0
        try:
            return self._do_upload(cfg, scan=scan)
        finally:
            self._upload_lock.release()

    def _do_upload(self, cfg: dict | None = None, scan: dict | None = None) -> int:
        cfg = cfg or config.load()
        pending = self.storage.unuploaded()
        self.status["pending_upload"] = len(pending)
        # Nothing to say: no pending results and no progress to report.
        if not pending and scan is None:
            return 0
        try:
            accepted = pusher.push(
                cfg["server_url"], cfg["api_token"], cfg["site_name"], pending, scan=scan
            )
            if pending:
                self.storage.mark_uploaded(accepted)
            self.status["pending_upload"] = self.storage.pending_count()
            self.status["last_upload"] = _now_iso()
            self.status["last_upload_count"] = len(accepted)
            if accepted:
                log.info("uploaded %d results", len(accepted))
            return len(accepted)
        except pusher.PushError as exc:
            self.status["last_error"] = f"upload: {exc}"
            log.warning("upload failed (will retry): %s", exc)
            return 0
