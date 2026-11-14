"""SQLite transfer history (survives restarts, powers the history view)."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at   REAL NOT NULL,
    session       TEXT NOT NULL DEFAULT '',
    camera_ip     TEXT NOT NULL DEFAULT '',
    original_name TEXT NOT NULL,
    final_name    TEXT NOT NULL DEFAULT '',
    final_path    TEXT NOT NULL DEFAULT '',
    backup_path   TEXT NOT NULL DEFAULT '',
    ext           TEXT NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    upload_s      REAL,
    status        TEXT NOT NULL,
    message       TEXT NOT NULL DEFAULT '',
    sha1          TEXT NOT NULL DEFAULT '',
    exif          TEXT NOT NULL DEFAULT '{}',
    has_thumb     INTEGER NOT NULL DEFAULT 0,
    lr_picked_up  REAL
);
CREATE INDEX IF NOT EXISTS ix_transfers_sha1 ON transfers(sha1);
CREATE INDEX IF NOT EXISTS ix_transfers_session ON transfers(session);
CREATE INDEX IF NOT EXISTS ix_transfers_received ON transfers(received_at);
"""

STATUSES = ("imported", "held", "ignored", "duplicate", "failed")
COLUMNS = (
    "id", "received_at", "session", "camera_ip", "original_name", "final_name",
    "final_path", "backup_path", "ext", "size", "upload_s", "status", "message",
    "sha1", "exif", "has_thumb", "lr_picked_up",
)


class History:
    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        d = dict(row)
        try:
            d["exif"] = json.loads(d.get("exif") or "{}")
        except ValueError:
            d["exif"] = {}
        d["has_thumb"] = bool(d["has_thumb"])
        return d

    def add(self, **values: Any) -> int:
        values.setdefault("received_at", time.time())
        if isinstance(values.get("exif"), dict):
            values["exif"] = json.dumps(values["exif"], ensure_ascii=False)
        cols = [c for c in values if c in COLUMNS and c != "id"]
        sql = f"INSERT INTO transfers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        with self._lock:
            cur = self._db.execute(sql, [values[c] for c in cols])
            self._db.commit()
            return int(cur.lastrowid)

    def update(self, transfer_id: int, **values: Any) -> None:
        if isinstance(values.get("exif"), dict):
            values["exif"] = json.dumps(values["exif"], ensure_ascii=False)
        cols = [c for c in values if c in COLUMNS and c != "id"]
        if not cols:
            return
        sql = f"UPDATE transfers SET {', '.join(c + '=?' for c in cols)} WHERE id=?"
        with self._lock:
            self._db.execute(sql, [values[c] for c in cols] + [transfer_id])
            self._db.commit()

    def get(self, transfer_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        return self._row(row) if row else None

    def find_imported_sha1(self, sha1: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM transfers WHERE sha1=? AND status='imported' ORDER BY id DESC LIMIT 1",
                (sha1,),
            ).fetchone()
        return self._row(row) if row else None

    def _where(self, q: str = "", status: str = "", session: str = "", since: float = 0):
        clauses, args = [], []
        if q:
            clauses.append("(original_name LIKE ? OR final_name LIKE ? OR exif LIKE ? OR session LIKE ?)")
            args += [f"%{q}%"] * 4
        if status:
            clauses.append("status=?")
            args.append(status)
        if session:
            clauses.append("session=?")
            args.append(session)
        if since:
            clauses.append("received_at>=?")
            args.append(since)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args

    def list(self, limit: int = 50, offset: int = 0, **filters: Any) -> Dict[str, Any]:
        where, args = self._where(**filters)
        limit = max(1, min(int(limit), 500))
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) FROM transfers{where}", args).fetchone()[0]
            rows = self._db.execute(
                f"SELECT * FROM transfers{where} ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [limit, max(0, int(offset))],
            ).fetchall()
        return {"total": total, "items": [self._row(r) for r in rows]}

    def stats(self, session: str) -> Dict[str, Any]:
        day_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        with self._lock:
            s = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size),0), AVG(upload_s), "
                "COALESCE(SUM(upload_s),0) FROM transfers WHERE session=? AND status='imported'",
                (session,),
            ).fetchone()
            today = self._db.execute(
                "SELECT COUNT(*) FROM transfers WHERE status='imported' AND received_at>=?",
                (day_start,),
            ).fetchone()[0]
            by_status = dict(self._db.execute(
                "SELECT status, COUNT(*) FROM transfers WHERE session=? GROUP BY status", (session,)
            ).fetchall())
            total = self._db.execute("SELECT COUNT(*) FROM transfers WHERE status='imported'").fetchone()[0]
            sessions = [r[0] for r in self._db.execute(
                "SELECT session FROM transfers GROUP BY session ORDER BY MAX(id) DESC LIMIT 30"
            ).fetchall()]
        count, size, avg_upload, sum_upload = s
        return {
            "session_count": count,
            "session_bytes": size,
            "avg_upload_s": avg_upload,
            "avg_mbps": (size / sum_upload / 1e6 * 8) if sum_upload else None,
            "today_count": today,
            "total_count": total,
            "by_status": by_status,
            "sessions": sessions,
        }

    def timeline(self, minutes: int = 60) -> List[int]:
        now = time.time()
        start = now - minutes * 60
        buckets = [0] * minutes
        with self._lock:
            rows = self._db.execute(
                "SELECT received_at FROM transfers WHERE status='imported' AND received_at>=?", (start,)
            ).fetchall()
        for (ts,) in rows:
            i = int((ts - start) // 60)
            if 0 <= i < minutes:
                buckets[i] += 1
        return buckets

    def session_seq(self, session: str) -> int:
        """Number of distinct shots (RAW+JPEG of one shot count once) in a session."""
        with self._lock:
            rows = self._db.execute(
                "SELECT original_name FROM transfers WHERE session=? AND status IN ('imported','held')",
                (session,),
            ).fetchall()
        return len({Path(r[0]).stem.upper() for r in rows})

    def export_csv(self, **filters: Any) -> str:
        where, args = self._where(**filters)
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM transfers{where} ORDER BY id", args).fetchall()
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["Zeit", "Session", "Original", "Neuer Name", "Status", "Größe (MB)",
                    "Upload (s)", "Kamera", "Objektiv", "Belichtung", "Blende", "ISO",
                    "Brennweite", "Lightroom übernommen", "Meldung"])
        for r in rows:
            d = self._row(r)
            e = d["exif"]
            w.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d["received_at"])),
                d["session"], d["original_name"], d["final_name"], d["status"],
                f"{d['size'] / 1e6:.2f}".replace(".", ","),
                f"{d['upload_s']:.2f}".replace(".", ",") if d["upload_s"] else "",
                e.get("model", ""), e.get("lens", ""), e.get("exposure", ""),
                e.get("fnumber") or "", e.get("iso") or "", e.get("focal_mm") or "",
                time.strftime("%H:%M:%S", time.localtime(d["lr_picked_up"])) if d["lr_picked_up"] else "",
                d["message"],
            ])
        return "﻿" + out.getvalue()  # BOM so Excel detects UTF-8
