"""Background poller: scan the subnet, capture each receiver, store, upload.

Runs on its own thread. Sleeps between cycles for the configured interval but
wakes early if the web UI requests an immediate scan ("Scan now").
"""

import logging
import threading
from datetime import datetime, timezone

from . import config, parser, pusher, scanner
from .ssh_client import ReceiverUnreachable, capture_receiver_cli
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
        self.status = {
            "running": False,
            "current_ip": None,
            "last_run": None,
            "last_found": 0,
            "last_error": None,
            "pending_upload": 0,
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
                try:
                    screen = capture_receiver_cli(
                        host=ip,
                        user=cfg["ssh_user"],
                        key_path=cfg["ssh_key_path"],
                        command=cfg["cli_command"],
                        wait_seconds=int(cfg.get("cli_wait_seconds", 15)),
                        connect_timeout=int(cfg.get("ssh_connect_timeout", 15)),
                        cols=int(cfg.get("term_cols", 200)),
                        rows=int(cfg.get("term_rows", 60)),
                    )
                except ReceiverUnreachable:
                    continue  # not a receiver / can't auth -> skip quietly
                except Exception as exc:
                    log.warning("capture failed for %s: %s", ip, exc)
                    continue

                parsed = parser.parse_screen(screen)
                if not parser.is_valid_receiver(parsed):
                    continue

                self.storage.save_result(parsed, screen, _now_iso(), ip)
                found += 1
                log.info("recorded receiver at %s (%d nodes)", ip, len(parsed["nodes"]))
        finally:
            self.status.update(running=False, current_ip=None,
                               last_run=_now_iso(), last_found=found)

        self.upload_pending(cfg)
        return self.status

    def upload_pending(self, cfg: dict | None = None) -> int:
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
            log.info("uploaded %d results", len(accepted))
            return len(accepted)
        except pusher.PushError as exc:
            self.status["last_error"] = f"upload: {exc}"
            log.warning("upload failed (will retry): %s", exc)
            return 0
