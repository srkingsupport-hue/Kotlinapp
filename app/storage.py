#!/usr/bin/env python3
"""Thread-safe SQLite WAL storage for download metrics, settings, and sticky client state."""

import sqlite3
import threading
import time
from typing import Any, Dict, Optional

from app import config


class WebStorage:
    """Lightweight SQLite persistence with WAL mode and zero external dependencies."""

    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    title TEXT,
                    media_type TEXT NOT NULL,
                    quality TEXT NOT NULL,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    duration INTEGER NOT NULL DEFAULT 0,
                    from_cache INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_downloads_created
                    ON downloads(created_at DESC);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def record_download(
        self,
        video_id: str,
        title: str,
        media_type: str,
        quality: str,
        file_size: int,
        duration: int,
        from_cache: bool,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO downloads
                    (video_id, title, media_type, quality, file_size, duration, from_cache, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    title[:160],
                    media_type,
                    quality,
                    int(file_size or 0),
                    int(duration or 0),
                    1 if from_cache else 0,
                    now,
                ),
            )
            self._conn.commit()

    def get_stats(self) -> Dict[str, Any]:
        now = time.time()
        day_ago = now - 86400
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
            last_24h = self._conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE created_at >= ?", (day_ago,)
            ).fetchone()[0]
            cache_hits = self._conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE from_cache = 1"
            ).fetchone()[0]
            by_type_rows = self._conn.execute(
                "SELECT media_type, COUNT(*) AS cnt FROM downloads GROUP BY media_type"
            ).fetchall()
            by_type = {r["media_type"]: r["cnt"] for r in by_type_rows}
            return {
                "total_downloads": total,
                "downloads_24h": last_24h,
                "cache_hits": cache_hits,
                "by_type": by_type,
            }

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
            self._conn.commit()
