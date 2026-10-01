import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

from dotbot import config
from dotbot.cli import parser, setup
from dotbot.locking import relay_lock
from dotbot.events import Events, CallbackError, callback_url, headers, PublicResolver
from dotbot.mcp import Auth, make_app, PREFIX, VERSION
from dotbot.relay import Relay
from dotbot.store import Store

OWNER, GUILD, CHANNEL, BOT = [str(100000000000000000 + n) for n in range(4)]
SETTINGS = {"owner_id": OWNER, "guild_ids": [GUILD], "channel_ids": [CHANNEL]}
SECRET = "whsec_" + base64.b64encode(b"s" * 32).decode()


def message(mid="100000000000000010"):
    return {"id": mid, "guild_id": GUILD, "channel_id": CHANNEL,
            "author_id": OWNER, "content": "hello", "received_at": time.time()}


def subscription():
    return {"name": "message.created", "arguments": {"channel_id": CHANNEL},
            "delivery": {"mode": "webhook", "url": "https://callbacks.example.com/event", "secret": SECRET}}


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_channel_conversation_context_and_silence(self):
        settings = dict(SETTINGS, listen="channels", audience="channel")
        relay = Relay(settings, self.store)
        relay.on_received = Mock()
        relay._connection.user = SimpleNamespace(id=int(BOT))
        m = SimpleNamespace(id=100000000000000020, guild=SimpleNamespace(id=int(GUILD)),
            channel=SimpleNamespace(id=int(CHANNEL)), author=SimpleNamespace(id=100000000000000099, bot=False, display_name="Guest"),
            webhook_id=None, mentions=[], content="Could someone explain this?", reference=None)
        self.assertTrue(relay.intents.message_content)
        await relay.on_message(m)
        relay.on_received.assert_called_once_with()
        first = self.store.get(str(m.id))
        self.assertEqual(first["mentioned"], 0)
        self.assertEqual(first["author_name"], "Guest")
        self.store.skip(str(m.id))
        self.store.skip(str(m.id))
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):
            self.store.reply(str(m.id), "too late")
        m.id += 1
        m.content = "Here is the missing detail."
        m.reference = SimpleNamespace(message_id=int(first["id"]))
        await relay.on_message(m)
        self.assertEqual(len(self.store.context(str(m.id))), 2)
        self.assertEqual(self.store.get(str(m.id))["reply_to"], first["id"])
        for field, value in (("author", SimpleNamespace(id=int(BOT), bot=True)),
                             ("channel", SimpleNamespace(id=999)), ("guild", None), ("webhook_id", 123)):
            with patch.object(m, field, value):
                m.id += 1
                await relay.on_message(m)
        self.assertEqual(len(self.store.list("all")), 2)
        self.assertFalse(config.allowed(SETTINGS, first["author_id"], GUILD, CHANNEL))

    async def test_setup_keeps_credentials_private_and_lock_exclusive(self):
        directory = self.path / "new-install"
        token_file = self.path / "input-token"
        token_file.write_text("test-secret-not-a-real-token")
        args = parser().parse_args(["--data-dir", str(directory), "setup", "--owner", OWNER,
            "--guilds", GUILD, "--channels", CHANNEL, "--token-file", str(token_file)])
        output = setup(args)
        self.assertNotIn("test-secret", json.dumps(output))
        self.assertNotIn("test-secret", (directory / "config.json").read_text())
        self.assertEqual(config.token(directory), "test-secret-not-a-real-token")
        self.assertEqual(config.load(directory)["listen"], "channels")
        if os.name != "nt":
            self.assertEqual((directory / "token").stat().st_mode & 0o777, 0o600)
        with relay_lock(directory):
            with self.assertRaises(ValueError):
                with relay_lock(directory):
                    pass
        with self.assertRaises(ValueError):
            setup(args)

    async def test_local_mcp_context_skip_and_origin_boundary(self):
        auth = Auth(self.path, local=True)
        events = Events(self.store, SETTINGS, auth.principal)
        typing = Mock()
        client = TestClient(TestServer(make_app(SETTINGS, self.store, events, auth, begin_reply=typing)))
        try:
            await client.start_server()
            self.store.receive(message())
            async def rpc(name, origin=None):
                method = "tools/call"
                params = {"name": name, "arguments": {"message_id": message()["id"]}, "_meta": {
                    PREFIX + "protocolVersion": VERSION, PREFIX + "clientInfo": {}, PREFIX + "clientCapabilities": {}}}
                hs = {"MCP-Protocol-Version": VERSION, "Mcp-Method": method, "Mcp-Name": name}
                if origin:
                    hs["Origin"] = origin
                return await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=hs)
            self.assertEqual((await rpc("skip", "https://untrusted.example")).status, 403)
            response = await rpc("get_context")
            self.assertEqual(len((await response.json())["result"]["structuredContent"]["messages"]), 1)
            typing.assert_not_called()
            response = await rpc("begin_reply")
            started = (await response.json())["result"]["structuredContent"]["reply_started_at"]
            self.assertIsNotNone(started)
            typing.assert_called_once()
            response = await rpc("begin_reply")
            self.assertEqual((await response.json())["result"]["structuredContent"]["reply_started_at"], started)
            response = await rpc("skip")
            self.assertEqual((await response.json())["result"]["structuredContent"]["status"], "skipped")
            response = await rpc("begin_reply")
            self.assertTrue((await response.json())["result"]["isError"])
            self.assertEqual(typing.call_count, 2)
            self.assertIsNone(self.store.claim())
        finally:
            await client.close()

    async def test_restart_deduplication_and_uncertain_send(self):
        m = message()
        self.assertTrue(self.store.receive(m))
        self.assertFalse(self.store.receive(m))
        self.store.reply(m["id"], "reply")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.reply(m["id"], "reply")["status"], "queued")
        with self.assertRaises(ValueError):
            self.store.reply(m["id"], "different")
        other = Store(self.path)
        try:
            self.assertIsNotNone(self.store.claim())
            self.assertIsNone(other.claim())
        finally:
            other.close()
        self.store.recover()
        self.assertEqual(self.store.get(m["id"])["status"], "uncertain")
        with self.assertRaises(ValueError):
            self.store.retry(m["id"])
        self.store.retry(m["id"], True)
        self.assertIsNotNone(self.store.claim())

    async def test_receive_filters_and_reply_destination(self):
        relay = Relay(SETTINGS, self.store)
        relay._connection.user = SimpleNamespace(id=int(BOT))
        m = SimpleNamespace(id=int(message()["id"]), guild=SimpleNamespace(id=int(GUILD)),
            channel=SimpleNamespace(id=int(CHANNEL)), author=SimpleNamespace(id=int(OWNER), bot=False),
            webhook_id=None, mentions=[SimpleNamespace(id=int(BOT))], content=f"<@{BOT}> hi")
        for field, value in (("author", SimpleNamespace(id=999, bot=False)),
                             ("author", SimpleNamespace(id=int(OWNER), bot=True)),
                             ("channel", SimpleNamespace(id=999)), ("guild", None),
                             ("webhook_id", 99), ("mentions", [])):
            with patch.object(m, field, value):
                await relay.on_message(m)
                self.assertEqual(self.store.list(), [])
        await relay.on_message(m)
        self.assertEqual(self.store.list()[0]["content"], "hi")
        self.store.reply(str(m.id), "@everyone hello")
        outgoing = self.store.claim()
        channel = SimpleNamespace(guild=m.guild, send=AsyncMock(return_value=SimpleNamespace(id=123)))
        relay.get_channel = Mock(return_value=channel)
        await relay.send_request(outgoing)
        self.assertEqual(self.store.get(str(m.id))["status"], "sent")
        kwargs = channel.send.call_args.kwargs
        self.assertEqual(kwargs["nonce"], str(m.id))
        self.assertEqual(kwargs["reference"].message_id, m.id)
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertEqual(self.store.reply(str(m.id), "@everyone hello")["status"], "sent")
        self.assertIsNone(self.store.claim())
        self.assertEqual(channel.send.call_count, 1)

    async def test_network_failure_does_not_requeue_reply(self):
        self.store.receive(message())
        self.store.reply(message()["id"], "answer")
        relay = Relay(SETTINGS, self.store)
        relay.get_channel = Mock(return_value=SimpleNamespace(guild=SimpleNamespace(id=int(GUILD)),
            send=AsyncMock(side_effect=asyncio.TimeoutError())))
        await relay.send_request(self.store.claim())
        self.assertEqual(self.store.get(message()["id"])["status"], "uncertain")
        self.assertIsNone(self.store.claim())

    async def test_delivery_error_preserves_diagnostics_without_credentials(self):
        relay = Relay(SETTINGS, self.store)
        relay.http.token = "private-bot-credential"
        for index, (kind, status, code, expected) in enumerate([
            (discord.NotFound, 404, 10008, "failed"),
            (discord.Forbidden, 403, 50013, "failed"),
            (discord.HTTPException, 400, 50035, "uncertain"),
        ]):
            with self.subTest(status=status):
                m = message(str(100000000000000030 + index))
                self.store.receive(m)
                self.store.reply(m["id"], "private reply text")
                exc = kind(SimpleNamespace(status=status, reason="Rejected"), {
                    "code": code, "message": "Unknown Message\nprivate-bot-credential private reply text "
                    "https://example.com/private?token=secret Bearer secret-key"})
                relay.get_channel = Mock(return_value=SimpleNamespace(guild=SimpleNamespace(id=int(GUILD)),
                    send=AsyncMock(side_effect=exc)))
                with self.assertLogs("dotbot", level="WARNING") as logs:
                    await relay.send_request(self.store.claim())
                saved = self.store.get(m["id"])
                self.assertEqual(saved["status"], expected)
                for output in (saved["error"], " ".join(logs.output)):
                    self.assertIn(f"status={status} code={code}", output)
                    self.assertIn("Unknown Message", output)
                    for secret in (relay.http.token, "private reply text", "example.com", "secret-key", "\n"):
                        self.assertNotIn(secret, output)
                self.assertIsNone(self.store.claim())

    async def test_subscription_verification_delivery_restart_and_unsubscribe(self):
        events = Events(self.store, SETTINGS, "owner")
        async def receive(sub, event_id, payload):
            if payload.get("type") == "verification":
                return 200, json.dumps({"challenge": payload["challenge"]}).encode()
            return 200, b"{}"
        with patch("dotbot.events.post_signed", side_effect=receive) as post:
            first = await events.subscribe(subscription())
            second = await events.subscribe(subscription())
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(post.call_count, 1)
            self.store.receive(message())
            # Reopen the database to simulate a host process restart.
            self.store.close()
            self.store = Store(self.path)
            events = Events(self.store, SETTINGS, "owner")
            await events.tick()
            await events.tick()
            self.assertEqual(post.call_count, 2)
            payload = post.call_args.args[2]
            self.assertEqual(payload["eventId"], "discord_" + message()["id"])
            self.assertEqual(payload["data"]["channel_id"], CHANNEL)
            self.assertNotIn("content", payload["data"])
            await events.unsubscribe(subscription())
            await events.unsubscribe(subscription())
            self.store.receive(message("100000000000000011"))
            await events.tick()
            self.assertEqual(post.call_count, 2)

    async def test_bad_verification_and_private_callbacks(self):
        events = Events(self.store, SETTINGS, "owner")
        with patch("dotbot.events.post_signed", return_value=(200, b'{"challenge":"wrong"}')):
            with self.assertRaises(CallbackError):
                await events.subscribe(subscription())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0], 0)
        for url in ("http://example.com", "https://127.0.0.1", "https://[::1]", "https://169.254.169.254",
                    "https://user:pass@example.com", "https://example.com:22"):
            with self.assertRaises(ValueError):
                callback_url(url)
        resolver = PublicResolver()
        with patch("aiohttp.resolver.ThreadedResolver.resolve", return_value=[{"host": "10.0.0.1"}]):
            with self.assertRaises(ValueError):
                await resolver.resolve("public-looking.example.com", 443)
        await resolver.close()

    async def test_callback_wakes_immediately(self):
        events = Events(self.store, SETTINGS, "owner")
        async def verify(sub, event_id, payload):
            return 200, json.dumps({"challenge": payload["challenge"]}).encode()
        with patch("dotbot.events.post_signed", side_effect=verify):
            await events.subscribe(subscription())
        posted = asyncio.Event()
        async def accept(*args):
            posted.set()
            return 200, b""
        with patch("dotbot.events.post_signed", side_effect=accept):
            worker = asyncio.create_task(events.run())
            try:
                await asyncio.sleep(0)  # Let the worker find an empty queue and wait.
                self.store.receive(message())
                events.wake()
                await asyncio.wait_for(posted.wait(), timeout=0.25)
                self.assertIsNone(self.store.get(message()["id"])["reply_started_at"])
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_typing_coalesces_requests_and_stops_after_skip_or_timeout(self):
        relay = Relay(SETTINGS, self.store)
        first, second = message(), message("100000000000000011")
        self.store.receive(first)
        self.store.receive(second)
        pulse = asyncio.Event()
        channel = SimpleNamespace(guild=SimpleNamespace(id=int(GUILD)), typing=AsyncMock(side_effect=lambda: pulse.set()))
        relay.get_channel = Mock(return_value=channel)
        relay.start_typing(first)
        task = relay.typing_tasks[CHANNEL]
        relay.start_typing(second)
        self.assertIs(relay.typing_tasks[CHANNEL], task)
        await asyncio.wait_for(pulse.wait(), timeout=0.25)
        self.store.skip(first["id"])
        relay.typing_requests[CHANNEL][second["id"]] = 0
        await asyncio.wait_for(task, timeout=1.5)
        channel.typing.assert_awaited_once()
        self.assertFalse(relay.typing_tasks)
        relay.start_typing(first)  # Already handled messages must not show typing.
        await relay.typing_tasks[CHANNEL]
        channel.typing.assert_awaited_once()

    async def test_standard_webhooks_signature_and_rotation(self):
        from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac
        second = "whsec_" + base64.b64encode(b"t" * 32).decode()
        body = b'{"eventId":"evt_1"}'
        signed = headers({"id": "sub_1", "secret": SECRET, "old_secret": second, "rotate_until": 101},
                         "evt_1", body, 100)
        signatures = signed["webhook-signature"].split()
        self.assertEqual(len(signatures), 2)
        for raw_key, signature in zip((b"s" * 32, b"t" * 32), signatures):
            verifier = crypto_hmac.HMAC(raw_key, hashes.SHA256())
            verifier.update(b'evt_1.100.{"eventId":"evt_1"}')
            verifier.verify(base64.b64decode(signature.split(",")[1]))
        self.assertEqual(signed["X-MCP-Subscription-Id"], "sub_1")

    async def test_webhook_retries_preserve_event_id_and_stop_at_410(self):
        events = Events(self.store, SETTINGS, "owner")
        async def verify(sub, event_id, payload):
            return 200, json.dumps({"challenge": payload["challenge"]}).encode()
        with patch("dotbot.events.post_signed", side_effect=verify):
            await events.subscribe(subscription())
        self.store.receive(message())
        with patch("dotbot.events.post_signed", side_effect=[asyncio.TimeoutError("private callback URL"), (503, b""), (410, b"")]) as post:
            await events.tick()
            self.store.db.execute("UPDATE deliveries SET next_attempt=0")
            self.store.db.commit()
            await events.tick()
            self.store.db.execute("UPDATE deliveries SET next_attempt=0")
            self.store.db.commit()
            await events.tick()
            await events.tick()
            self.assertEqual(post.call_count, 3)
            self.assertEqual(post.call_args_list[0].args[1], post.call_args_list[1].args[1])
            self.assertEqual(self.store.db.execute("SELECT expires FROM subscriptions").fetchone()[0], 0)
        saved = self.store.get(message()["id"])
        attempts = saved["callback_attempts"]
        self.assertEqual([a["http_status"] for a in attempts], [None, 503, 410])
        self.assertEqual(attempts[0]["error_type"], "TimeoutError")
        self.assertNotIn("private callback URL", json.dumps(saved))
        self.assertNotIn(SECRET, json.dumps(saved))
        for attempt in attempts:
            self.assertEqual(attempt["event_id"], "discord_" + message()["id"])
            self.assertGreaterEqual(attempt["started_at"], saved["received_at"])
            self.assertGreaterEqual(attempt["completed_at"], attempt["started_at"])
            self.assertGreaterEqual(attempt["duration_ms"], 0)

    async def test_mcp_auth_discovery_event_and_idempotent_reply(self):
        with patch.dict(os.environ, {"DOTBOT_MCP_TOKEN": "x" * 32}):
            auth = Auth(self.path, dev=True)
        events = Events(self.store, SETTINGS, auth.principal)
        client = TestClient(TestServer(make_app(SETTINGS, self.store, events, auth)))
        try:
            await client.start_server()
            response = await client.post("/mcp", json={})
            self.assertEqual(response.status, 401)
            async def rpc(method, params=None, extra_headers=None):
                params = dict(params or {})
                params["_meta"] = {PREFIX + "protocolVersion": VERSION,
                    PREFIX + "clientInfo": {"name": "test", "version": "1"}, PREFIX + "clientCapabilities": {}}
                hs = {"Authorization": "Bearer " + "x" * 32, "MCP-Protocol-Version": VERSION, "Mcp-Method": method,
                      "Accept": "application/json, text/event-stream"}
                if method == "tools/call":
                    hs["Mcp-Name"] = params["name"]
                hs.update(extra_headers or {})
                response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, headers=hs)
                return response, await response.json()
            response, body = await rpc("server/discover")
            self.assertEqual(response.status, 200)
            self.assertIn("events", body["result"]["capabilities"])
            response, body = await rpc("events/list")
            self.assertEqual(body["result"]["events"][0]["name"], "message.created")
            self.store.receive(message())
            params = {"name": "reply", "arguments": {"message_id": message()["id"], "text": "hi"}}
            for _ in range(2):
                response, body = await rpc("tools/call", params)
                self.assertEqual(body["result"]["structuredContent"]["status"], "queued")
            response, body = await rpc("tools/call", params, {"Mcp-Name": "wrong"})
            self.assertEqual(response.status, 400)
            self.assertEqual(body["error"]["code"], -32020)
        finally:
            await client.close()

    async def test_oauth_rejects_wrong_identity_audience_scope_and_expiration(self):
        settings = {"resource": "https://relay.example.com/mcp", "issuer": "https://auth.example.com/",
                    "jwks_url": "https://auth.example.com/jwks", "owner_subject": "owner"}
        (self.path / "mcp.json").write_text(json.dumps(settings))
        auth = Auth(self.path)
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        auth.jwks = SimpleNamespace(get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private.public_key()))
        claims = {"sub": "owner", "iss": settings["issuer"], "aud": settings["resource"], "exp": time.time() + 60, "scope": "dotbot"}
        async def check(fields):
            token = jwt.encode(fields, private, algorithm="RS256")
            return await auth.check(SimpleNamespace(headers={"Authorization": "Bearer " + token}))
        self.assertGreater(await check(claims), time.time())
        for field, value in (("sub", "stranger"), ("aud", "elsewhere"), ("iss", "elsewhere"), ("scope", "other"), ("exp", 0)):
            with self.assertRaises((ValueError, jwt.PyJWTError)):
                await check(dict(claims, **{field: value}))


if __name__ == "__main__":
    unittest.main()
