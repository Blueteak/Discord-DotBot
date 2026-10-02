"""Durable MCP webhook subscriptions and Standard Webhooks delivery."""
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from urllib.parse import urlsplit

import aiohttp

from .config import allowed

NAME = "message.created"
log = logging.getLogger("dotbot")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def key(secret):
    try:
        if not secret.startswith("whsec_"):
            raise ValueError()
        result = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(result) <= 64:
            raise ValueError()
        return result
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Invalid webhook signing secret.") from None


def public_ip(address):
    ip = ipaddress.ip_address(address)
    if not ip.is_global or ip.is_multicast or getattr(ip, "ipv4_mapped", None):
        raise ValueError("Callback must resolve to public addresses.")


def callback_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Callback must be an HTTPS URL without credentials or a fragment.")
    if parsed.port not in (None, 443):
        raise ValueError("Callback must use HTTPS port 443.")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        public_ip(parsed.hostname)
    return url


class PublicResolver(aiohttp.resolver.ThreadedResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await super().resolve(host, port, family)
        for item in addresses:
            public_ip(item["host"])
        return addresses


def headers(subscription, event_id, body, now):
    signed = str(int(now))
    payload = event_id.encode() + b"." + signed.encode() + b"." + body
    keys = [subscription["secret"]]
    if subscription.get("old_secret") and subscription.get("rotate_until", 0) > now:
        keys.append(subscription["old_secret"])
    signatures = ["v1," + base64.b64encode(hmac.new(key(secret), payload, hashlib.sha256).digest()).decode()
                  for secret in keys]
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": signed, "webhook-signature": " ".join(signatures),
            "X-MCP-Subscription-Id": subscription["id"]}


async def post_signed(subscription, event_id, payload):
    url = callback_url(subscription["url"])
    body = canonical(payload).encode()
    if len(body) > 262144:
        raise ValueError("Event exceeds 256 KiB.")
    # A new connector validates DNS for each connection and connects to exactly
    # those addresses. TLS still verifies the original hostname. No redirects.
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as session:
        async with session.post(url, data=body, headers=headers(subscription, event_id, body, time.time()),
                                allow_redirects=False) as response:
            data = await response.content.read(4097)
            return response.status, data


class CallbackError(ValueError):
    def __init__(self, reason):
        super().__init__("Callback verification failed.")
        self.reason = reason


class Events:
    def __init__(self, store, config, principal):
        self.store, self.config, self.principal = store, config, principal
        self.db = store.db
        self.lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, channel_id TEXT NOT NULL,
                url TEXT NOT NULL, secret TEXT NOT NULL, expires REAL NOT NULL,
                created REAL NOT NULL, verified REAL NOT NULL,
                old_secret TEXT, rotate_until REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                subscription_id TEXT NOT NULL, request_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (subscription_id, request_id)
            );
        """)

    def channel_ids(self):
        return self.store.accessible_ids() if self.config.get("scope") == "accessible" else self.config["channel_ids"]

    def definition(self):
        return {"name": NAME, "description": "A permitted person sent a message in an enabled Discord channel. Read the message and context to decide whether it is directed at you. If it is, call begin_reply promptly before preparing your answer, then reply. Otherwise skip it without typing or replying.",
                "delivery": ["webhook"],
                "inputSchema": {"type": "object", "properties": {"channel_id": {"type": "string", "enum": self.channel_ids()}},
                                "required": ["channel_id"], "additionalProperties": False},
                "payloadSchema": {"type": "object", "properties": {name: {"type": "string"} for name in
                                 ("message_id", "guild_id", "channel_id", "author_id", "url")},
                                  "required": ["message_id", "guild_id", "channel_id", "author_id", "url"], "additionalProperties": False}}

    def identity(self, params, check_access=True):
        args, delivery = params["arguments"], params["delivery"]
        if params["name"] != NAME or set(args) != {"channel_id"} or (check_access and args["channel_id"] not in self.channel_ids()):
            raise ValueError("Unknown event or unauthorized channel.")
        if delivery["mode"] != "webhook":
            raise ValueError("Only webhook delivery is supported.")
        url = callback_url(delivery["url"])
        identity = [self.principal, url, NAME, args]
        return "sub_" + hashlib.sha256(canonical(identity).encode()).hexdigest(), url, args["channel_id"]

    async def subscribe(self, params):
        # Serialize refresh/rotation with delivery and unsubscribe.
        async with self.lock:
            sid, url, channel = self.identity(params)
            secret = params["delivery"]["secret"]
            key(secret)
            ttl = params.get("ttlMs")
            if ttl is not None and (type(ttl) not in (int, float) or ttl <= 0 or ttl != ttl):
                raise ValueError("ttlMs must be a positive duration or null.")
            lifetime = min(ttl / 1000, 86400) if ttl is not None else 86400
            now = time.time()
            old = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone()
            # Verification cache is bound to principal, URL, and key for five minutes.
            verified = old and old["secret"] == secret and old["verified"] > now - 300
            if not verified:
                challenge = secrets.token_urlsafe(32)
                try:
                    status, body = await post_signed({"id": sid, "url": url, "secret": secret},
                        "verify_" + secrets.token_hex(16), {"type": "verification", "challenge": challenge})
                    echoed = json.loads(body).get("challenge")
                    if not 200 <= status < 300 or not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
                        raise CallbackError("challenge_failed")
                except asyncio.TimeoutError:
                    raise CallbackError("timeout") from None
                except (aiohttp.ClientError, ValueError, UnicodeError):
                    raise CallbackError("challenge_failed") from None
            now = time.time()
            # A refresh after expiry starts a new non-replay subscription interval.
            created = old["created"] if old and old["expires"] > now else now
            old_secret = old["secret"] if old and old["secret"] != secret else (old["old_secret"] if old else None)
            rotate_until = now + 300 if old and old["secret"] != secret else (old["rotate_until"] if old else 0)
            with self.db:
                if old and old["expires"] <= now:
                    self.db.execute("DELETE FROM deliveries WHERE subscription_id=?", (sid,))
                self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, self.principal, channel, url, secret, now + lifetime, created,
                     old["verified"] if verified else now, old_secret, rotate_until))
            return {"id": sid, "refreshBefore": iso(now + lifetime), "cursor": None, "truncated": False}

    async def unsubscribe(self, params):
        async with self.lock:
            sid, _, _ = self.identity(params, check_access=False)
            with self.db:
                self.db.execute("DELETE FROM subscriptions WHERE id=? AND principal=?", (sid, self.principal))
                self.db.execute("DELETE FROM deliveries WHERE subscription_id=?", (sid,))
            return {}

    async def tick(self):
        async with self.lock:
            now = time.time()
            # Reconstruct unsent notifications from durable requests, even after a crash.
            with self.db:
                self.db.execute("""INSERT OR IGNORE INTO deliveries (subscription_id, request_id)
                    SELECT s.id, r.id FROM subscriptions s JOIN requests r ON s.channel_id=r.channel_id
                    WHERE s.principal=? AND s.expires>? AND r.received_at>=s.created""", (self.principal, now))
            row = self.db.execute("""SELECT d.request_id, d.attempts, s.* FROM deliveries d
                JOIN subscriptions s ON s.id=d.subscription_id
                WHERE s.principal=? AND s.expires>? AND d.status='pending' AND d.next_attempt<=?
                ORDER BY d.next_attempt LIMIT 1""", (self.principal, now, now)).fetchone()
            if row is None:
                return False
            sub = dict(row)
            request = self.store.get(sub["request_id"])
            if not self.store.visible(self.config, request):
                status = "stopped"
            else:
                payload = {"eventId": "discord_" + request["id"], "name": NAME,
                           "timestamp": iso(request["received_at"]), "cursor": None,
                           "data": {"message_id": request["id"], "guild_id": request["guild_id"],
                                    "channel_id": request["channel_id"], "author_id": request["author_id"],
                                    "url": f'https://discord.com/channels/{request["guild_id"]}/{request["channel_id"]}/{request["id"]}'}}
                started = time.time()
                with self.db:
                    attempt_id = self.db.execute("""INSERT INTO callback_attempts
                        (request_id, subscription_id, event_id, started_at) VALUES (?, ?, ?, ?)""",
                        (request["id"], sub["id"], payload["eventId"], started)).lastrowid
                log.info("Callback start event=%s attempt=%s received_at=%s started_at=%s queue_ms=%.1f",
                         payload["eventId"], attempt_id, iso(request["received_at"]), iso(started),
                         (started - request["received_at"]) * 1000)
                clock = time.monotonic()
                code, error_type = None, None
                try:
                    code, _ = await post_signed(sub, payload["eventId"], payload)
                    status = "delivered" if 200 <= code < 300 else ("pending" if code == 429 or code >= 500 else "stopped")
                    if code == 410:
                        with self.db:
                            self.db.execute("UPDATE subscriptions SET expires=0 WHERE id=?", (sub["id"],))
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    error_type = type(exc).__name__
                    status = "pending"
                except ValueError as exc:
                    error_type = type(exc).__name__
                    status = "stopped"
                except asyncio.CancelledError:
                    error_type = "CancelledError"
                    raise
                except Exception as exc:
                    error_type = type(exc).__name__
                    raise
                finally:
                    completed = time.time()
                    duration_ms = (time.monotonic() - clock) * 1000
                    with self.db:
                        self.db.execute("""UPDATE callback_attempts SET completed_at=?, duration_ms=?,
                            http_status=?, error_type=? WHERE id=?""",
                            (completed, duration_ms, code, error_type, attempt_id))
                    # Never log callback URLs, signing secrets, headers, or response bodies.
                    log.info("Callback complete event=%s attempt=%s completed_at=%s duration_ms=%.1f http_status=%s error_type=%s",
                             payload["eventId"], attempt_id, iso(completed), duration_ms, code, error_type)
            attempts = sub["attempts"] + 1
            if status == "pending" and attempts >= 8:
                status = "exhausted"
            with self.db:
                self.db.execute("""UPDATE deliveries SET status=?, attempts=?, next_attempt=?
                    WHERE subscription_id=? AND request_id=?""",
                    (status, attempts, time.time() + min(300, 2 ** attempts), sub["id"], request["id"]))
            return True

    def wake(self):
        self.wakeup.set()

    async def run(self):
        while True:
            self.wakeup.clear()
            if await self.tick():
                continue  # Drain ready callbacks without a delay between messages.
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass  # Recover persisted work/retries and writes from other processes.
