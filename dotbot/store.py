import sqlite3
import time

from .config import private_directory


class Store:
    def __init__(self, directory):
        private_directory(directory)
        self.db = sqlite3.connect(directory / "relay.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                content TEXT NOT NULL,
                received_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reply TEXT,
                reply_id TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS health (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                updated_at REAL NOT NULL,
                connected INTEGER NOT NULL
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(requests)")}
        for name, declaration in (("author_name", "TEXT"), ("mentioned", "INTEGER NOT NULL DEFAULT 0"),
                                  ("reply_to", "TEXT"), ("reply_sent_at", "REAL")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE requests ADD COLUMN {name} {declaration}")
        self.db.execute("CREATE INDEX IF NOT EXISTS requests_channel_time ON requests(channel_id, received_at)")
        self.db.commit()
        (directory / "relay.sqlite").chmod(0o600)

    def close(self):
        self.db.close()

    def receive(self, message):
        message = {"author_name": None, "mentioned": 0, "reply_to": None, **message}
        with self.db:
            result = self.db.execute("""
                INSERT OR IGNORE INTO requests
                (id, guild_id, channel_id, author_id, content, received_at, author_name, mentioned, reply_to)
                VALUES (:id, :guild_id, :channel_id, :author_id, :content, :received_at, :author_name, :mentioned, :reply_to)
            """, message)
        return result.rowcount == 1

    def list(self, status="pending", limit=50):
        if status == "all":
            rows = self.db.execute("SELECT * FROM requests ORDER BY received_at DESC LIMIT ?", (limit,))
        else:
            rows = self.db.execute("SELECT * FROM requests WHERE status=? ORDER BY received_at LIMIT ?",
                                   (status, limit))
        return [dict(row) for row in rows]

    def get(self, request_id):
        row = self.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown request ID.")
        return dict(row)

    def context(self, request_id, limit=30):
        anchor = self.get(request_id)
        rows = self.db.execute("""SELECT * FROM requests
            WHERE guild_id=? AND channel_id=? AND received_at<=?
            ORDER BY received_at DESC, id DESC LIMIT ?""",
            (anchor["guild_id"], anchor["channel_id"], anchor["received_at"], limit))
        return list(reversed([dict(row) for row in rows]))

    def skip(self, request_id):
        with self.db:
            self.db.execute("UPDATE requests SET status='skipped' WHERE id=? AND status='pending'", (request_id,))
        row = self.get(request_id)
        if row["status"] != "skipped":
            raise ValueError("Only pending messages can be skipped.")
        return row

    def reply(self, request_id, text):
        # A single message keeps delivery and recovery unambiguous.
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > 2000:
            raise ValueError("Reply must contain 1–2000 Discord characters. Shorten it and try again.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.get(request_id)
            if existing["reply"] == text and existing["status"] != "pending":
                return existing
            result = self.db.execute("""
                UPDATE requests SET reply=?, status='queued', error=NULL
                WHERE id=? AND status='pending'
            """, (text, request_id))
        if result.rowcount != 1:
            raise ValueError("Request is missing or already has a different reply. Use show to inspect it.")
        return self.get(request_id)

    def claim(self):
        # BEGIN IMMEDIATE prevents two consumers from claiming the same reply.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM requests WHERE status='queued' ORDER BY received_at LIMIT 1").fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE requests SET status='sending' WHERE id=?", (row["id"],))
        return dict(row)

    def finish(self, request_id, status, reply_id=None, error=None):
        with self.db:
            self.db.execute("UPDATE requests SET status=?, reply_id=?, error=?, reply_sent_at=? WHERE id=? AND status='sending'",
                            (status, reply_id, error, time.time() if status == "sent" else None, request_id))

    def recover(self):
        with self.db:
            self.db.execute("""UPDATE requests SET status='uncertain',
                error='Relay stopped during delivery. Check Discord before retrying.'
                WHERE status='sending'""")

    def retry(self, request_id, accept_duplicate_risk=False):
        with self.db:
            result = self.db.execute("""UPDATE requests SET status='queued', error=NULL
                WHERE id=? AND (status='failed' OR (status='uncertain' AND ?))""",
                (request_id, accept_duplicate_risk))
        if result.rowcount != 1:
            raise ValueError("Only failed replies can be retried automatically. For uncertain delivery, check Discord and use --accept-duplicate-risk if needed.")

    def heartbeat(self, connected):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO health VALUES (1, ?, ?)", (time.time(), int(connected)))

    def health(self):
        row = self.db.execute("SELECT * FROM health WHERE singleton=1").fetchone()
        result = {
            "connected": bool(row and row["connected"] and time.time() - row["updated_at"] < 15),
            "last_heartbeat": row["updated_at"] if row else None,
            "counts": {r["status"]: r["count"] for r in self.db.execute(
                "SELECT status, COUNT(*) AS count FROM requests GROUP BY status")},
        }

        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='subscriptions'").fetchone():
            result["active_subscriptions"] = self.db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE expires>?", (time.time(),)).fetchone()[0]
            result["event_deliveries"] = {r["status"]: r["count"] for r in self.db.execute(
                "SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status")}
        return result
