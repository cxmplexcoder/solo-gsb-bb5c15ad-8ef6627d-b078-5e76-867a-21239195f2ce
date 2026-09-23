"""SQLite 持久层：存储解析结果，供指纹幂等复用。"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resolutions (
    fingerprint     TEXT PRIMARY KEY,
    status          TEXT NOT NULL CHECK (status IN ('sat', 'unsat')),
    result_json     TEXT NOT NULL,
    solve_ms        REAL NOT NULL,
    attempts        INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self.init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def get(self, fp: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fingerprint, status, result_json, solve_ms, attempts, created_at "
                "FROM resolutions WHERE fingerprint = ?",
                (fp,),
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result_json"])
        result.update(
            fingerprint=row["fingerprint"],
            status=row["status"],
            reused=True,
            created_at=row["created_at"],
            solve_ms=row["solve_ms"],
            attempts=row["attempts"],
        )
        return result

    def save(
        self,
        fp: str,
        status: str,
        result: dict,
        solve_ms: float,
        attempts: int,
        created_at: Optional[str] = None,
    ) -> None:
        created_at = created_at or utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO resolutions "
                "(fingerprint, status, result_json, solve_ms, attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (fp, status, json.dumps(result, ensure_ascii=False), solve_ms, attempts, created_at),
            )

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
