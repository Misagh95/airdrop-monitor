"""مدیریت دیتابیس SQLite برای ذخیره منابع و وضعیت"""
import sqlite3
import os
import tempfile
from datetime import datetime, timedelta


def _find_writable_path(requested_path: str) -> str:
    """پیدا کردن مسیری که قابل نوشتن است."""
    candidates = [requested_path, "/tmp/data.db", "data.db"]
    for path in candidates:
        if not path or path == ":memory:":
            continue
        try:
            # تلاش برای ساختن فایل تست
            d = os.path.dirname(path) or "."
            os.makedirs(d, exist_ok=True)
            test_file = path + ".test"
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
            return path
        except Exception:
            continue
    # اگر هیچ مسیری کار نکرد، از :memory: استفاده کن
    return ":memory:"


class Database:
    def __init__(self, path="data.db"):
        # مسیر قابل نوشتن پیدا کن
        self.path = _find_writable_path(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
        if self.path == ":memory:":
            import logging
            logging.warning("⚠️ دیتابیس در حافظه اجرا می‌شود — داده‌ها موقع ری‌استرت از بین می‌روند")

    def _init_tables(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (
                key       TEXT PRIMARY KEY,
                timestamp TEXT
            );

            CREATE TABLE IF NOT EXISTS sources (
                type      TEXT NOT NULL,
                name      TEXT NOT NULL,
                url       TEXT,
                entity_id INTEGER,
                active    INTEGER DEFAULT 1,
                added_at  TEXT,
                PRIMARY KEY (type, name)
            );

            CREATE TABLE IF NOT EXISTS admins (
                chat_id   INTEGER PRIMARY KEY,
                username  TEXT,
                added_at  TEXT
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self.conn.commit()

    # ---- Meta ----
    def get_meta(self, key, default=None):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    # ---- Seen (deduplication) ----
    def is_seen(self, key):
        return (
            self.conn.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone()
            is not None
        )

    def mark_seen(self, key):
        self.conn.execute(
            "INSERT OR REPLACE INTO seen (key, timestamp) VALUES (?, ?)",
            (key, datetime.now().isoformat()),
        )
        self.conn.commit()

    def prune_seen(self, days=30):
        """حذف پیام‌های قدیمی برای جلوگیری از بزرگ شدن دیتابیس"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        self.conn.execute("DELETE FROM seen WHERE timestamp < ?", (cutoff,))
        self.conn.commit()

    def seen_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    # ---- Sources ----
    def get_sources(self, source_type=None, active_only=False):
        query = "SELECT type, name, url, entity_id, active, added_at FROM sources"
        conditions, params = [], []
        if source_type:
            conditions.append("type = ?")
            params.append(source_type)
        if active_only:
            conditions.append("active = 1")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY type, name"
        return self.conn.execute(query, params).fetchall()

    def add_source(self, source_type, name, url, entity_id=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO sources (type, name, url, entity_id, active, added_at) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (source_type, name, url, entity_id, datetime.now().isoformat()),
        )
        self.conn.commit()

    def remove_source(self, source_type, name):
        cur = self.conn.execute(
            "DELETE FROM sources WHERE type = ? AND name = ?", (source_type, name)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def update_entity_id(self, source_type, name, entity_id):
        self.conn.execute(
            "UPDATE sources SET entity_id = ? WHERE type = ? AND name = ?",
            (entity_id, source_type, name),
        )
        self.conn.commit()

    # ---- Admins ----
    def add_admin(self, chat_id, username=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO admins (chat_id, username, added_at) VALUES (?, ?, ?)",
            (chat_id, username, datetime.now().isoformat()),
        )
        self.conn.commit()

    def remove_admin(self, chat_id):
        cur = self.conn.execute("DELETE FROM admins WHERE chat_id = ?", (chat_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def get_admins(self):
        return self.conn.execute(
            "SELECT chat_id, username, added_at FROM admins ORDER BY added_at"
        ).fetchall()

    def is_admin(self, chat_id):
        return (
            self.conn.execute(
                "SELECT 1 FROM admins WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            is not None
        )
