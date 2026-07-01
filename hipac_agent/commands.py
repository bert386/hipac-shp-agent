"""Poll the central server for maintenance commands and execute them.

Flow: GET /api/commands (site token) -> for each command, resolve the target
receiver's current IP from the latest local scan, map the action to a fixed
command via :mod:`actions` (never server-provided shell), run it over SSH, and
POST the result back to /api/commands/{id}/result.
"""

import logging
import threading

import requests

from . import config
from .actions import UnknownAction, build_command
from .ssh_client import ReceiverUnreachable, exec_receiver_command

log = logging.getLogger("hipac.commands")


def _auth(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['api_token']}", "Accept": "application/json"}


class CommandRunner(threading.Thread):
    def __init__(self, storage):
        super().__init__(daemon=True)
        self.storage = storage
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("command poll failed")
            secs = max(15, int(config.load().get("command_poll_seconds", 60)))
            self._stop.wait(timeout=secs)

    def poll_once(self) -> None:
        cfg = config.load()
        if not cfg.get("server_url") or not cfg.get("api_token"):
            return
        for cmd in self._fetch(cfg):
            self._handle(cfg, cmd)

    def _fetch(self, cfg: dict) -> list[dict]:
        url = cfg["server_url"].rstrip("/") + "/api/commands"
        resp = requests.get(url, headers=_auth(cfg), timeout=30)
        resp.raise_for_status()
        return resp.json().get("commands", [])

    def _handle(self, cfg: dict, cmd: dict) -> None:
        cid = cmd.get("id")
        action = cmd.get("action")
        params = cmd.get("params") or {}
        receiver = cmd.get("receiver") or {}

        ip = self._resolve_ip(receiver) or receiver.get("ip_address")
        if not ip:
            self._report(cfg, cid, "failed", error="no known IP for receiver")
            return

        try:
            command, expect_disconnect = build_command(action, params)
        except (ValueError, UnknownAction) as exc:
            log.warning("rejected command %s (%s): %s", cid, action, exc)
            self._report(cfg, cid, "failed", error=f"rejected: {exc}")
            return

        log.info("executing command %s (%s) on %s", cid, action, ip)
        try:
            code, out, err = exec_receiver_command(
                host=ip,
                user=cfg["ssh_user"],
                key_path=cfg["ssh_key_path"],
                command=command,
                connect_timeout=int(cfg.get("ssh_connect_timeout", 15)),
                expect_disconnect=expect_disconnect,
            )
        except ReceiverUnreachable as exc:
            self._report(cfg, cid, "failed", error=f"unreachable: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - report anything back to the dashboard
            self._report(cfg, cid, "failed", error=str(exc))
            return

        status = "done" if code == 0 else "failed"
        self._report(
            cfg, cid, status,
            output=(out or "")[:4000],
            exit_code=code,
            error=((err or "")[:2000] if code != 0 else None),
        )

    def _resolve_ip(self, receiver: dict) -> str | None:
        """Prefer the current IP we last saw for this MAC over the server's."""
        mac = (receiver.get("mac_address") or "").lower()
        if not mac:
            return None
        for result in self.storage.latest_per_receiver():
            rec = result.get("receiver", {})
            if (rec.get("mac_address") or "").lower() == mac:
                return rec.get("ip_address") or result.get("source_ip")
        return None

    def _report(self, cfg: dict, cid, status: str, output=None, exit_code=None, error=None) -> None:
        url = cfg["server_url"].rstrip("/") + f"/api/commands/{cid}/result"
        body = {"status": status}
        if output is not None:
            body["output"] = output
        if exit_code is not None:
            body["exit_code"] = exit_code
        if error is not None:
            body["error"] = error
        try:
            requests.post(url, json=body, headers=_auth(cfg), timeout=30).raise_for_status()
            log.info("reported command %s -> %s", cid, status)
        except requests.RequestException as exc:
            log.warning("failed to report command %s: %s", cid, exc)
