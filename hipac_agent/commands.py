"""Poll the central server for maintenance commands and execute them.

Flow: GET /api/commands (site token) -> for each command, resolve the target
receiver's current IP from the latest local scan, map the action to a fixed
command via :mod:`actions` (never server-provided shell), run it over SSH, and
POST the result back to /api/commands/{id}/result.
"""

import logging
import os
import subprocess
import threading

import requests

from . import config
from .actions import UnknownAction, build_command
from .ssh_client import ReceiverUnreachable, exec_receiver_command

log = logging.getLogger("hipac.commands")


def _auth(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['api_token']}", "Accept": "application/json"}


class CommandRunner(threading.Thread):
    def __init__(self, storage, poller=None):
        super().__init__(daemon=True)
        self.storage = storage
        self.poller = poller       # for 'poll_now'
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
            self._dispatch(cfg, cmd)

    def _fetch(self, cfg: dict) -> list[dict]:
        url = cfg["server_url"].rstrip("/") + "/api/commands"
        resp = requests.get(url, headers=_auth(cfg), timeout=30)
        resp.raise_for_status()
        return resp.json().get("commands", [])

    # NOTE: not named ``_handle`` — Python 3.13's threading.Thread sets an
    # instance attribute ``self._handle`` (a _thread._ThreadHandle), which would
    # shadow a method of that name and make ``self._handle(...)`` raise
    # "'_thread._ThreadHandle' object is not callable", silently killing the
    # command runner on 3.13. Keep custom Thread method names clear of internals.
    def _dispatch(self, cfg: dict, cmd: dict) -> None:
        cid = cmd.get("id")
        action = cmd.get("action")
        params = cmd.get("params") or {}
        receiver = cmd.get("receiver") or {}

        # Site-level agent commands (poll_now, update_agent) have no receiver.
        if not receiver:
            self._handle_agent(cfg, cid, action)
            return

        # Whatever happens below, the command must produce a reported result so
        # it never sits at "sent" forever.
        try:
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

            status = "done" if code == 0 else "failed"
            log.info("command %s finished: %s (exit %s)", cid, status, code)
            self._report(
                cfg, cid, status,
                output=(out or "")[:4000],
                exit_code=code,
                error=((err or "")[:2000] if code != 0 else None),
            )
        except Exception as exc:  # noqa: BLE001 - never let a command orphan
            log.exception("command %s crashed", cid)
            self._report(cfg, cid, "failed", error=str(exc)[:2000])

    def _handle_agent(self, cfg: dict, cid, action: str) -> None:
        """Handle site-level actions that target this Pi, not a receiver."""
        try:
            if action == "poll_now":
                if self.poller:
                    self.poller.trigger_now()
                self._report(cfg, cid, "done", output="scan triggered")
            elif action == "update_agent":
                self._run_update(cfg, cid)
            else:
                self._report(cfg, cid, "failed", error=f"unknown agent action: {action}")
        except Exception as exc:  # noqa: BLE001
            log.exception("agent command %s crashed", cid)
            self._report(cfg, cid, "failed", error=str(exc)[:2000])

    def _run_update(self, cfg: dict, cid) -> None:
        update_cmd = (cfg.get("agent_update_command") or "").strip()
        if not update_cmd:
            self._report(cfg, cid, "failed", error="agent_update_command not configured")
            return
        # Report BEFORE running — the update restarts this process, so we can't
        # report afterwards. Run detached so it survives our own restart.
        log.info("running agent self-update")
        self._report(cfg, cid, "done", output="update started; agent will restart")
        subprocess.Popen(
            update_cmd, shell=True, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=os.path.expanduser("~"),
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
