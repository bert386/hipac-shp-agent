"""Local SQLite storage for poll results and an upload queue.

Results are stored with an ``uploaded`` flag so the agent keeps working (and
retries the upload) even when the central server is unreachable.
"""

import json
import sqlite3
import threading

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    receiver_mac TEXT,
    receiver_ip  TEXT,
    polled_at    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    raw_screen   TEXT,
    uploaded     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_results_uploaded ON results(uploaded);
CREATE INDEX IF NOT EXISTS idx_results_mac ON results(receiver_mac);
"""


class Storage:
    def __init__(self, path: str | None = None):
        self._path = path or config.db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def save_result(self, parsed: dict, raw_screen: str, polled_at: str, source_ip: str) -> int:
        receiver = parsed.get("receiver", {})
        payload = {
            "receiver": receiver,
            "nodes": parsed.get("nodes", []),
            "polled_at": polled_at,
            "source_ip": source_ip,
        }
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO results (receiver_mac, receiver_ip, polled_at, payload, raw_screen) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    receiver.get("mac_address"),
                    receiver.get("ip_address") or source_ip,
                    polled_at,
                    json.dumps(payload),
                    raw_screen,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def unuploaded(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, payload FROM results WHERE uploaded = 0 ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"id": r["id"], **json.loads(r["payload"])} for r in rows]

    def mark_uploaded(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE results SET uploaded = 1 WHERE id = ?", [(i,) for i in ids]
            )
            self._conn.commit()

    def latest_per_receiver(self) -> list[dict]:
        """Most recent result for each receiver, for the local dashboard."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT r.payload FROM results r
                JOIN (
                    SELECT COALESCE(receiver_mac, receiver_ip) AS k, MAX(id) AS max_id
                    FROM results GROUP BY k
                ) latest ON r.id = latest.max_id
                ORDER BY r.receiver_ip
                """
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def pending_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS c FROM results WHERE uploaded = 0"
            ).fetchone()["c"]
