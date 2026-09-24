"""Хранилище на SQLite: список разрешённых номеров и журнал проездов."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS plates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    valid_until TEXT,               -- YYYY-MM-DD включительно, NULL — бессрочно
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    camera TEXT NOT NULL,
    plate TEXT NOT NULL DEFAULT '',   -- прочитанный номер
    matched TEXT,                     -- номер из списка, с которым совпал
    owner TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    hits INTEGER NOT NULL DEFAULT 0,
    decision TEXT NOT NULL,           -- granted / denied / manual / logged
    detail TEXT NOT NULL DEFAULT '',
    snapshot TEXT,
    crop TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_plate ON events(plate);
"""


class Database:
    def __init__(self, path: Path):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
        self._allowed_cache: list[dict] | None = None

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, args)
            rows = cur.fetchall()
            self._conn.commit()
            return rows

    def _exec(self, sql: str, args=()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid

    # --- список номеров ---------------------------------------------------
    def list_plates(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM plates ORDER BY plate")]

    def get_plate(self, plate_id: int) -> dict | None:
        rows = self._q("SELECT * FROM plates WHERE id=?", (plate_id,))
        return dict(rows[0]) if rows else None

    def add_plate(self, plate: str, owner: str = "", note: str = "", valid_until: str | None = None) -> int:
        self._allowed_cache = None
        return self._exec(
            "INSERT INTO plates(plate, owner, note, valid_until, created_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(plate) DO UPDATE SET owner=excluded.owner, note=excluded.note,"
            " valid_until=excluded.valid_until, active=1",
            (plate, owner, note, valid_until or None, time.time()),
        )

    def update_plate(self, plate_id: int, **fields) -> None:
        allowed = {"plate", "owner", "note", "active", "valid_until"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        self._allowed_cache = None
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE plates SET {sets} WHERE id=?", (*fields.values(), plate_id))

    def delete_plate(self, plate_id: int) -> None:
        self._allowed_cache = None
        self._exec("DELETE FROM plates WHERE id=?", (plate_id,))

    def allowed_plates(self) -> dict[str, dict]:
        """Активные и не просроченные номера: {номер: запись}."""
        if self._allowed_cache is None:
            self._allowed_cache = self.list_plates()
        today = time.strftime("%Y-%m-%d")
        return {
            p["plate"]: p
            for p in self._allowed_cache
            if p["active"] and (not p["valid_until"] or p["valid_until"] >= today)
        }

    # --- журнал -----------------------------------------------------------
    def add_event(self, **e) -> int:
        cols = ["ts", "camera", "plate", "matched", "owner", "confidence", "hits",
                "decision", "detail", "snapshot", "crop"]
        defaults = {"ts": time.time(), "plate": "", "owner": "", "detail": "", "confidence": 0, "hits": 0}
        e = {**defaults, **{k: v for k, v in e.items() if v is not None}}
        vals = [e.get(c) for c in cols]
        return self._exec(
            f"INSERT INTO events({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
        )

    def update_event(self, event_id: int, **fields) -> None:
        allowed = {"decision", "detail", "matched", "owner"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._exec(f"UPDATE events SET {sets} WHERE id=?", (*fields.values(), event_id))

    def get_event(self, event_id: int) -> dict | None:
        rows = self._q("SELECT * FROM events WHERE id=?", (event_id,))
        return dict(rows[0]) if rows else None

    def list_events(self, limit: int = 100, offset: int = 0, plate: str = "", decision: str = "") -> list[dict]:
        where, args = [], []
        if plate:
            where.append("plate LIKE ?")
            args.append(f"%{plate}%")
        if decision:
            where.append("decision = ?")
            args.append(decision)
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
        return [dict(r) for r in self._q(sql, (*args, limit, offset))]

    def old_events(self, before_ts: float) -> list[dict]:
        return [dict(r) for r in self._q("SELECT id, snapshot, crop FROM events WHERE ts < ?", (before_ts,))]

    def delete_events_before(self, before_ts: float) -> None:
        self._exec("DELETE FROM events WHERE ts < ?", (before_ts,))
