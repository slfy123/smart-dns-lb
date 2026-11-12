from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.data_dir.chmod(0o700)
        self.db_path = self.data_dir / "smart-dns.sqlite3"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript("""
          CREATE TABLE IF NOT EXISTS config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
          );
          CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            node_id TEXT NOT NULL DEFAULT ''
          );
          CREATE INDEX IF NOT EXISTS idx_events_created ON events(id DESC);
        """)
        self._secure_permissions(self.db_path)
        maximum = self._conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM events"
        ).fetchone()[0]
        self._conn.execute("DELETE FROM events WHERE id <= ?", (maximum - 1000,))
        self._last_event_prune = maximum

    @staticmethod
    def _secure_permissions(path: Path) -> None:
        if os.name != "nt":
            try:
                path.chmod(0o600)
            except OSError:
                pass

    def get_config(self) -> tuple[int, dict[str, Any]] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, payload FROM config WHERE id=1"
            ).fetchone()
            if row is None:
                return None
            return int(row["version"]), json.loads(row["payload"])

    def save_config(
        self, payload: dict[str, Any], expected_version: int | None = None
    ) -> int:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT version FROM config WHERE id=1"
                ).fetchone()
                current = int(row["version"]) if row else 0
                if expected_version is not None and expected_version != current:
                    raise VersionConflict(f"配置版本已更新：当前版本 {current}")
                new_version = current + 1
                self._conn.execute(
                    "INSERT INTO config(id, version, payload, updated_at) VALUES(1, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET version=excluded.version, payload=excluded.payload, updated_at=excluded.updated_at",
                    (new_version, encoded, utc_now()),
                )
                self._conn.execute("COMMIT")
                self._secure_permissions(self.db_path)
                return new_version
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def append_event(
        self, message: str, level: str = "info", node_id: str = ""
    ) -> None:
        message = message[:500]
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO events(created_at, level, message, node_id) VALUES(?, ?, ?, ?)",
                (utc_now(), level[:16], message, node_id[:80]),
            )
            if cursor.lastrowid - self._last_event_prune >= 100:
                self._conn.execute(
                    "DELETE FROM events WHERE id <= ?", (cursor.lastrowid - 1000,)
                )
                self._last_event_prune = cursor.lastrowid

    def get_events(self, limit: int = 50) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, level, message, node_id FROM events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_or_create_admin_token(self) -> tuple[str, bool]:
        token_path = self.data_dir / "admin.token"
        with self._lock:
            if token_path.exists():
                token = token_path.read_text(encoding="utf-8").strip()
                if token:
                    return token, False
            return self.reset_admin_token(), True

    def reset_admin_token(self) -> str:
        token_path = self.data_dir / "admin.token"
        temporary = self.data_dir / (".admin-token-" + secrets.token_hex(8))
        with self._lock:
            token = secrets.token_urlsafe(32)
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    self._secure_permissions(temporary)
                    handle.write(token + "\n")
                os.replace(temporary, token_path)
            finally:
                temporary.unlink(missing_ok=True)
        return token

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class VersionConflict(Exception):
    pass
