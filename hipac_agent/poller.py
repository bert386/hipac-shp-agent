"""Background poller: scan the subnet, capture each receiver, store, upload.

Runs on its own thread. Sleeps between cycles for the configured interval but
wakes early if the web UI requests an immediate scan ("Scan now").
"""

import logging
import threading
import time
from datetime import datetime, timezone

from . import config, parser, pusher, scanner
from .ssh_client import ReceiverAuthFailed, ReceiverUnreachable, capture_receiver_cli
from .storage import Storage

log = logging.getLogger("hipac.poller")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Poller(threading.Thread):
    def __init__(self, storage: Storage):
        super().__init__(daemon=True)
        self.storage = storage
        self._wake = threading.Event()   # set to trigger an immediate cycle
        self._stop = threading.Event()
        self._upload_lock = threading.Lock()  # serialise auto + manual uploads
        self.status = {
            "running": False,
            "current_ip": None,
            "last_run": None,
            "last_found": 0,
            "last_error": None,
            "pending_upload": 0,
            "last_upload": None,
            "last_upload_count": 0,
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
        self.status.update(running=True, current_ip=None, last_error=None)
        found = 0
        try:
            devices = scanner.scan(
                cfg["interface"], cfg["subnet"],
                use_sudo=cfg.get("use_sudo", True),
                timeout=int(cfg.get("arp_scan_timeout", 120)),
            )
            excluded = set(cfg.get("excluded_ips") or [])
            targets = [d for d in devices if d["ip"] not in excluded]
            log.info("scan found %d devices, %d after exclusions", len(devices), len(targets))

            for dev in targets:
                if self._stop.is_set():
                    break
                ip = dev["ip"]
                self.status["current_ip"] = ip
                log.info("scanning %s", ip)
                cli_wait = int(cfg.get("cli_wait_seconds", 15))
                max_wait = max(int(cfg.get("cli_max_wait_seconds", 45)), cli_wait)
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
                        connect_timeout=int(cfg.get("ssh_connect_timeout", 15)),
                        cols=int(cfg.get("term_cols", 200)),
                        rows=int(cfg.get("term_rows", 60)),
                    )
                except ReceiverAuthFailed as exc:
                    # SSH is open but every method was rejected — likely a real
                    # receiver with a credential mismatch. Surface it.
                    log.warning("auth rejected at %s: %s", ip, exc)
                    continue
                except ReceiverUnreachable:
                    continue  # not a receiver / refused / timeout -> skip quietly
                except Exception as exc:
                    log.warning("capture failed for %s: %s", ip, exc)
                    continue

                parsed = parser.parse_screen(screen)
                if not parser.is_valid_receiver(parsed):
                    if screen.strip():
                        # SSH worked and something rendered, but no receiver data —
                        # usually a non-receiver, or a receiver that didn't paint in time.
                        log.info("no valid receiver data at %s (%d chars captured)", ip, len(screen))
                    continue

                # Confirmed a receiver. Some report their own MAC/IP as "unknown";
                # backfill from the arp-scan result so the sticky identity is stable.
                recv = parsed.setdefault("receiver", {})
                if not recv.get("mac_address") and dev.get("mac"):
                    recv["mac_address"] = dev["mac"]
                if not recv.get("ip_address"):
                    recv["ip_address"] = ip

                self.storage.save_result(parsed, screen, _now_iso(), ip)
                found += 1
                dur = time.monotonic() - t0
                capped = " (hit max_wait)" if dur >= max_wait - 0.5 else ""
                log.info("recorded receiver at %s (%d nodes) in %.0fs%s", ip, len(parsed["nodes"]), dur, capped)
        finally:
            self.status.update(running=False, current_ip=None,
                               last_run=_now_iso(), last_found=found)

        self.upload_pending(cfg)
        return self.status

    def upload_pending(self, cfg: dict | None = None) -> int:
        # Only one upload at a time — a manual "Upload now" must not race the
        # end-of-cycle upload and re-post the same readings.
        if not self._upload_lock.acquire(blocking=False):
            log.info("upload already in progress; skipping")
            return 0
        try:
            return self._do_upload(cfg)
        finally:
            self._upload_lock.release()

    def _do_upload(self, cfg: dict | None = None) -> int:
        cfg = cfg or config.load()
        pending = self.storage.unuploaded()
        self.status["pending_upload"] = len(pending)
        if not pending:
            return 0
        try:
            accepted = pusher.push(
                cfg["server_url"], cfg["api_token"], cfg["site_name"], pending
            )
            self.storage.mark_uploaded(accepted)
            self.status["pending_upload"] = self.storage.pending_count()
            self.status["last_upload"] = _now_iso()
            self.status["last_upload_count"] = len(accepted)
            log.info("uploaded %d results", len(accepted))
            return len(accepted)
        except pusher.PushError as exc:
            self.status["last_error"] = f"upload: {exc}"
            log.warning("upload failed (will retry): %s", exc)
            return 0
