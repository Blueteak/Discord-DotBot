import re
import uuid
import sqlite3
import time

from .config import allowed, private_directory


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
            CREATE TABLE IF NOT EXISTS outbound (
                operation_id TEXT PRIMARY KEY, request_id TEXT,
                guild_id TEXT, channel_id TEXT, author_id TEXT,
                destination_key TEXT NOT NULL,
                operation_key TEXT NOT NULL, reply TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', created_at REAL NOT NULL,
                reply_id TEXT, error TEXT, reply_sent_at REAL,
                UNIQUE(destination_key, operation_key)
            );
            CREATE TABLE IF NOT EXISTS channel_access (
                channel_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL,
                checked_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS health (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                updated_at REAL NOT NULL,
                connected INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS callback_attempts (
                id INTEGER PRIMARY KEY,
                request_id TEXT NOT NULL,
                subscription_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                started_at REAL NOT NULL,
                completed_at REAL,
                duration_ms REAL,
                http_status INTEGER,
                error_type TEXT
            );
            CREATE INDEX IF NOT EXISTS callback_attempts_request ON callback_attempts(request_id);
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(requests)")}
        for name, declaration in (("author_name", "TEXT"), ("mentioned", "INTEGER NOT NULL DEFAULT 0"),
                                  ("reply_to", "TEXT"), ("reply_sent_at", "REAL"), ("reply_started_at", "REAL")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE requests ADD COLUMN {name} {declaration}")
        self.db.execute("CREATE INDEX IF NOT EXISTS requests_channel_time ON requests(channel_id, received_at)")
        self.db.commit()
        (directory / "relay.sqlite").chmod(0o600)

    def set_access(self, channels):
        with self.db:
            self.db.execute("DELETE FROM channel_access")
            self.db.executemany("INSERT INTO channel_access VALUES (?, ?, ?)",
                                [(str(channel), str(guild), time.time()) for guild, channel in channels])

    def revoke_access(self, channel_id):
        with self.db:
            self.db.execute("DELETE FROM channel_access WHERE channel_id=?", (str(channel_id),))

    def connected(self):
        row = self.db.execute("SELECT connected, updated_at FROM health WHERE singleton=1").fetchone()
        return bool(row and row["connected"] and time.time() - row["updated_at"] < 15)

    def accessible_ids(self):
        if not self.connected():
            return []
        return [r[0] for r in self.db.execute(
            "SELECT channel_id FROM channel_access WHERE checked_at>?", (time.time() - 15,))]

    def visible(self, settings, message):
        if not allowed(settings, message["author_id"], message["guild_id"], message["channel_id"]):
            return False
        if settings.get("scope", "scoped") != "accessible":
            return True
        row = self.db.execute("SELECT 1 FROM channel_access WHERE guild_id=? AND channel_id=? AND checked_at>?",
                              (message["guild_id"], message["channel_id"], time.time() - 15)).fetchone()
        return bool(row and self.connected())

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

    @staticmethod
    def present(settings, message):
        return {**message, "is_owner": bool(settings.get("owner_id") and str(message["author_id"]) == settings["owner_id"])}

    def visible_list(self, settings, status="pending", limit=50):
        query = "SELECT * FROM requests"
        parameters = ()
        if status != "all":
            query += " WHERE status=?"
            parameters = (status,)
        query += " ORDER BY received_at " + ("DESC" if status == "all" else "ASC")
        result = []
        cursor = self.db.execute(query, parameters)
        try:
            for row in cursor:
                message = dict(row)
                if self.visible(settings, message):
                    result.append(self.present(settings, message))
                    if len(result) >= limit:
                        break
        finally:
            cursor.close()
        return result

    def get(self, request_id):
        row = self.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown request ID.")
        result = dict(row)
        result["callback_attempts"] = [dict(attempt) for attempt in self.db.execute(
            "SELECT event_id, started_at, completed_at, duration_ms, http_status, error_type "
            "FROM callback_attempts WHERE request_id=? ORDER BY id", (request_id,))]
        result["followups"] = [dict(row) for row in self.db.execute(
            "SELECT * FROM outbound WHERE request_id=? ORDER BY created_at, operation_id", (request_id,))]
        return result

    @staticmethod
    def validate_operation(operation_key, text):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_key):
            raise ValueError("Operation key must be 1–128 ASCII letters, digits, dots, underscores, colons or hyphens.")
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > 2000:
            raise ValueError("Reply must contain 1–2000 Discord characters.")

    def followup(self, request_id, operation_key, text):
        self.validate_operation(operation_key, text)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            parent = self.get(request_id)
            if parent["status"] != "sent":
                raise ValueError("Follow-ups require an initial confirmed sent reply.")
            row = self.db.execute("SELECT * FROM outbound WHERE request_id=? AND operation_key=?",
                                  (request_id, operation_key)).fetchone()
            if row:
                if row["reply"] != text:
                    raise ValueError("Operation key already used with different text.")
                return dict(row)
            operation_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO outbound(operation_id, request_id, destination_key, operation_key, reply, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (operation_id, request_id, "reply:" + request_id, operation_key, text, time.time()))
        return dict(self.db.execute("SELECT * FROM outbound WHERE operation_id=?", (operation_id,)).fetchone())

    def destination(self, settings, channel_id):
        row = self.db.execute("SELECT guild_id FROM channel_access WHERE channel_id=? AND checked_at>?",
                              (channel_id, time.time() - 15)).fetchone()
        if not row or not self.connected() or not allowed(settings, settings["owner_id"], row["guild_id"], channel_id):
            raise ValueError("Destination is not currently permitted or its permission snapshot is stale.")
        return {"guild_id": row["guild_id"], "channel_id": channel_id, "author_id": settings["owner_id"]}

    def send(self, settings, channel_id, operation_key, text):
        self.validate_operation(operation_key, text)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            destination = self.destination(settings, channel_id)
            key = "channel:" + channel_id
            row = self.db.execute("SELECT * FROM outbound WHERE destination_key=? AND operation_key=?", (key, operation_key)).fetchone()
            if row:
                if row["reply"] != text:
                    raise ValueError("Operation key already used with different text.")
                return dict(row)
            operation_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO outbound(operation_id, destination_key, operation_key, reply, created_at, guild_id, channel_id, author_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (operation_id, key, operation_key, text, time.time(), destination["guild_id"], channel_id, destination["author_id"]))
        return self.operation(settings, operation_id)

    def operation(self, settings, operation_id):
        row = self.db.execute("SELECT * FROM outbound WHERE operation_id=?", (operation_id,)).fetchone()
        if not row:
            raise ValueError("Unknown operation ID.")
        result = dict(row)
        if result["request_id"]:
            if not self.visible(settings, self.get(result["request_id"])):
                raise ValueError("Request is no longer accessible.")
        else:
            destination = self.destination(settings, result["channel_id"])
            if destination["guild_id"] != result["guild_id"]:
                raise ValueError("Destination server changed.")
        return result

    def retry_operation(self, settings, operation_id, accept_duplicate_risk=False):
        self.operation(settings, operation_id)
        with self.db:
            result = self.db.execute("UPDATE outbound SET status='queued', error=NULL WHERE operation_id=? AND (status='failed' OR (status='uncertain' AND ?))",
                                    (operation_id, accept_duplicate_risk))
        if result.rowcount != 1:
            raise ValueError("Only failed operations can retry automatically; uncertain delivery requires --accept-duplicate-risk after checking Discord.")

    def context(self, request_id, limit=30):
        anchor = self.get(request_id)
        rows = self.db.execute("""SELECT * FROM requests
            WHERE guild_id=? AND channel_id=? AND received_at<=?
            ORDER BY received_at DESC, id DESC LIMIT ?""",
            (anchor["guild_id"], anchor["channel_id"], anchor["received_at"], limit))
        return list(reversed([self.get(row["id"]) for row in rows]))

    def skip(self, request_id):
        with self.db:
            self.db.execute("UPDATE requests SET status='skipped' WHERE id=? AND status='pending'", (request_id,))
        row = self.get(request_id)
        if row["status"] != "skipped":
            raise ValueError("Only pending messages can be skipped.")
        return row

    def begin_reply(self, request_id):
        with self.db:
            result = self.db.execute("""UPDATE requests SET reply_started_at=COALESCE(reply_started_at, ?)
                WHERE id=? AND status='pending'""", (time.time(), request_id))
        if result.rowcount != 1:
            raise ValueError("Only pending messages can begin a reply.")
        return self.get(request_id)

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
                operation = self.db.execute("SELECT * FROM outbound WHERE status='queued' ORDER BY created_at, operation_id LIMIT 1").fetchone()
                if operation is None:
                    return None
                self.db.execute("UPDATE outbound SET status='sending' WHERE operation_id=?", (operation["operation_id"],))
                if operation["request_id"] is None:
                    return {**dict(operation), "id": None}
                parent = self.get(operation["request_id"])
                delivery = {k: v for k, v in dict(operation).items() if k not in ("guild_id", "channel_id", "author_id")}
                return {**parent, **delivery, "id": parent["id"]}
            self.db.execute("UPDATE requests SET status='sending' WHERE id=?", (row["id"],))
        return dict(row)

    def finish(self, request_id, status, reply_id=None, error=None, operation_id=None):
        if operation_id is not None:
            with self.db:
                self.db.execute("UPDATE outbound SET status=?, reply_id=?, error=?, reply_sent_at=? WHERE request_id IS ? AND operation_id=? AND status='sending'",
                                (status, reply_id, error, time.time() if status == "sent" else None, request_id, operation_id))
            return
        with self.db:
            self.db.execute("UPDATE requests SET status=?, reply_id=?, error=?, reply_sent_at=? WHERE id=? AND status='sending'",
                            (status, reply_id, error, time.time() if status == "sent" else None, request_id))

    def recover(self):
        with self.db:
            self.db.execute("""UPDATE requests SET status='uncertain',
                error='Relay stopped during delivery. Check Discord before retrying.'
                WHERE status='sending'""")

            self.db.execute("UPDATE outbound SET status='uncertain', error='Relay stopped during delivery. Check Discord before retrying.' WHERE status='sending'")

    def retry_followup(self, request_id, operation_key, accept_duplicate_risk=False):
        with self.db:
            result = self.db.execute("UPDATE outbound SET status='queued', error=NULL WHERE request_id=? AND operation_key=? AND (status='failed' OR (status='uncertain' AND ?))",
                                    (request_id, operation_key, accept_duplicate_risk))
        if result.rowcount != 1:
            raise ValueError("Only failed follow-ups can retry automatically; uncertain delivery requires --accept-duplicate-risk after checking Discord.")

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

        result["outbound_counts"] = {r["status"]: r["count"] for r in self.db.execute(
            "SELECT status, COUNT(*) AS count FROM outbound GROUP BY status")}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='subscriptions'").fetchone():
            result["active_subscriptions"] = self.db.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE expires>?", (time.time(),)).fetchone()[0]
            result["event_deliveries"] = {r["status"]: r["count"] for r in self.db.execute(
                "SELECT status, COUNT(*) AS count FROM deliveries GROUP BY status")}
        return result
