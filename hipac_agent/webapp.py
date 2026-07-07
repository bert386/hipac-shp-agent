"""Local configuration web UI for the Pi agent.

A tiny password-protected Flask app to edit settings (site name, polling
interval, excluded IPs, server/token, SSH details), view the last scan and
trigger a scan on demand.
"""

import functools
import hashlib
import os
import threading

from flask import (
    Flask, jsonify, redirect, render_template, request, session, url_for, flash,
)

from . import config
from .commands import CommandRunner
from .heartbeat import Heartbeat
from .poller import Poller
from .storage import Storage
from .terminal import TerminalServer

_storage = Storage()
_poller = Poller(_storage)
_command_runner = CommandRunner(_storage, _poller)
_terminal = TerminalServer()
_heartbeat = Heartbeat(_poller)


def _check_password(supplied: str) -> bool:
    return supplied == config.load().get("config_password", "")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def create_app() -> Flask:
    app = Flask(__name__)
    # Stable-ish secret so sessions survive restarts; overridable via env.
    app.secret_key = os.environ.get("HIPAC_SECRET") or hashlib.sha256(
        (config.load().get("config_password", "") + config.data_dir()).encode()
    ).hexdigest()

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if _check_password(request.form.get("password", "")):
                session["authed"] = True
                return redirect(request.args.get("next") or url_for("index"))
            flash("Incorrect password.")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return render_template(
            "index.html",
            status=_poller.status,
            terminal=_terminal.status,
            receivers=_storage.latest_per_receiver(),
            site_name=config.load().get("site_name"),
            poll_interval=config.load().get("poll_interval_minutes"),
        )

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        if request.method == "POST":
            f = request.form
            excluded = [ip.strip() for ip in f.get("excluded_ips", "").replace(",", "\n").splitlines() if ip.strip()]
            updates = {
                "site_name": f.get("site_name", "").strip(),
                "server_url": f.get("server_url", "").strip(),
                "api_token": f.get("api_token", "").strip(),
                "interface": f.get("interface", "").strip() or "eth0",
                "subnet": f.get("subnet", "").strip(),
                "poll_interval_minutes": int(f.get("poll_interval_minutes") or 60),
                "excluded_ips": excluded,
                "ssh_user": f.get("ssh_user", "").strip() or "root",
                "ssh_key_path": f.get("ssh_key_path", "").strip(),
                "cli_command": f.get("cli_command", "").strip() or "/receiver/receiver_cli",
                "cli_wait_seconds": int(f.get("cli_wait_seconds") or 15),
            }
            new_pw = f.get("config_password", "").strip()
            if new_pw:
                updates["config_password"] = new_pw
            config.save(updates)
            flash("Settings saved.")
            return redirect(url_for("settings"))
        return render_template("settings.html", cfg=config.load())

    @app.route("/scan-now", methods=["POST"])
    @login_required
    def scan_now():
        _poller.trigger_now()
        flash("Scan triggered — results will appear shortly.")
        return redirect(url_for("index"))

    @app.route("/status")
    def status():
        # Read-only, non-sensitive, LAN-only — no auth so it's easy to curl.
        cfg = config.load()
        return jsonify({
            **_poller.status,
            "terminal": _terminal.status,
            "site_name": cfg.get("site_name"),
            "poll_interval_minutes": cfg.get("poll_interval_minutes"),
            "server_url": cfg.get("server_url"),
        })

    @app.route("/upload-now", methods=["POST"])
    @login_required
    def upload_now():
        # Run in the background so the request returns immediately; the poller
        # lock prevents overlap with an in-progress upload.
        threading.Thread(target=_poller.upload_pending, daemon=True).start()
        flash("Upload triggered — pending results are being sent.")
        return redirect(url_for("index"))

    return app


def run() -> None:
    """Start the poller thread and serve the web UI (used by ``__main__``)."""
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    _poller.start()
    _command_runner.start()
    _terminal.start()   # self-provisions + supervises the tailnet web terminal
    _heartbeat.start()  # 60s liveness ping so the dashboard shows online/offline

    app = create_app()
    try:
        from waitress import serve
        serve(app, host=cfg["web_host"], port=int(cfg["web_port"]))
    except ImportError:
        app.run(host=cfg["web_host"], port=int(cfg["web_port"]))
