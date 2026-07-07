"""Lightweight liveness heartbeat.

Scan cycles can be an hour apart, so between them the server can't tell a live
Pi from a dead one. This posts a small ping every ``heartbeat_seconds`` so the
dashboard shows each Pi as online/offline in near-real-time.

It yields to real work: while a scan is running the poller's own per-receiver
progress uploads already keep the server fresh, so the heartbeat skips those
ticks (the "report when other processes aren't running" behaviour).
"""

import logging
import threading

from . import config, pusher

log = logging.getLogger("hipac.heartbeat")


class Heartbeat(threading.Thread):
    def __init__(self, poller=None) -> None:
        super().__init__(daemon=True)
        self.poller = poller
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            secs = max(15, int(config.load().get("heartbeat_seconds", 60)))
            # Wait first so we don't double up with the agent's startup poll.
            if self._stop.wait(timeout=secs):
                break
            try:
                self.beat_once()
            except Exception:  # never let the loop die
                log.exception("heartbeat failed")

    def beat_once(self) -> bool:
        """Send one heartbeat unless disabled or a scan is currently running.
        Returns True if a heartbeat was actually sent."""
        cfg = config.load()
        if not cfg.get("heartbeat_enabled", True):
            return False
        # A scan already keeps the server fresh via its progress uploads — don't
        # pile a heartbeat on top of active work.
        if self.poller is not None and self.poller.status.get("running"):
            return False
        ok = pusher.heartbeat(cfg.get("server_url"), cfg.get("api_token"), cfg.get("site_name"))
        if ok:
            log.debug("heartbeat sent")
        return ok
