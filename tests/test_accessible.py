from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from aiohttp.test_utils import TestClient, TestServer

from dotbot import config
from dotbot.cli import main, parser, setup
from dotbot.events import Events
from dotbot.mcp import Auth, make_app, PREFIX, VERSION
from dotbot.relay import Relay
from dotbot.store import Store
from dotbot.watcher import wait

GLOBAL = config.validate({"owner_id": "100000000000001", "scope": "accessible", "listen": "channels", "audience": "channel"})


class AccessibleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path)
        self.relay = Relay(GLOBAL, self.store)
        self.relay._connection.user = SimpleNamespace(id=999)
        self.guild = SimpleNamespace(id=100, me=SimpleNamespace(id=999), unavailable=False, channels=[], threads=[])
        self.relay._connection._guilds = {100: self.guild}
        self.channel = self.channel_for(200)
        self.relay.gateway_connected = True
        self.store.heartbeat(True)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    def channel_for(self, cid, guild=None, thread=False):
        guild = guild or self.guild
        channel = Mock(spec=discord.Thread if thread else discord.TextChannel)
        channel.id, channel.guild = cid, guild
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True, read_message_history=True,
            send_messages=True, send_messages_in_threads=True, manage_threads=False)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=12345))
        if thread:
            channel.archived = channel.locked = False
            channel.me = None
            channel.is_private.return_value = False
            guild.threads.append(channel)
        else:
            guild.channels.append(channel)
        return channel

    def message(self, mid=1, channel=None):
        channel = channel or self.channel
        return SimpleNamespace(id=mid, guild=channel.guild, channel=channel,
            author=SimpleNamespace(id=500, bot=False, display_name="untrusted"), webhook_id=None,
            mentions=[], content="Please explain this", reference=None)

    async def test_dynamic_new_channel_and_guild_without_restart(self):
        self.relay.refresh_access()
        new = self.channel_for(201)
        await self.relay.on_message(self.message(1, new))
        row = self.store.get("1")
        self.assertTrue(self.store.visible(GLOBAL, row))
        scoped = dict(GLOBAL, scope="scoped", owner_id="500", guild_ids=["100"], channel_ids=["200"])
        self.assertFalse(config.allowed(scoped, "500", "100", "201"))
        guild = SimpleNamespace(id=101, me=self.guild.me, unavailable=False, channels=[], threads=[])
        self.relay._connection._guilds[101] = guild
        newer = self.channel_for(202, guild)
        await self.relay.on_guild_join(guild)
        await self.relay.on_message(self.message(2, newer))
        self.assertTrue(self.store.visible(GLOBAL, self.store.get("2")))
        result = wait(self.store, GLOBAL, self.path, 1)
        self.assertEqual([m["id"] for m in result["context"]], ["1"])
        self.store.skip("1")
        self.assertEqual(wait(self.store, GLOBAL, self.path, 1)["message"]["id"], "2")

    async def test_dm_bot_webhook_and_thread_permissions(self):
        for field, value in (("guild", None), ("author", SimpleNamespace(id=500, bot=True)), ("webhook_id", 1)):
            message = self.message()
            setattr(message, field, value)
            await self.relay.on_message(message)
        self.assertEqual(self.store.list(), [])
        thread = self.channel_for(203, thread=True)
        await self.relay.on_message(self.message(3, thread))
        self.assertEqual(self.store.get("3")["channel_id"], "203")
        for field in ("archived", "locked"):
            setattr(thread, field, True)
            await self.relay.on_message(self.message(4, thread))
            setattr(thread, field, False)
        thread.is_private.return_value = True
        await self.relay.on_message(self.message(4, thread))
        self.assertEqual(len(self.store.list()), 1)
        thread.me = SimpleNamespace(id=999)
        await self.relay.on_message(self.message(4, thread))
        self.assertEqual(len(self.store.list()), 2)
        thread.permissions_for.return_value.send_messages_in_threads = False
        self.relay.refresh_access()
        self.assertFalse(self.store.visible(GLOBAL, self.store.get("4")))

    async def test_revoke_restore_stale_and_send_recheck(self):
        await self.relay.on_message(self.message())
        self.store.reply("1", "response")
        request = self.store.claim()
        self.relay.get_channel = Mock(return_value=self.channel)
        # Permission changes after the snapshot and before actual send.
        self.channel.permissions_for.return_value.view_channel = False
        await self.relay.send_request(request)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.store.get("1")["status"], "failed")
        self.assertFalse(self.store.visible(GLOBAL, self.store.get("1")))
        self.channel.permissions_for.return_value.view_channel = True
        self.relay.refresh_access()
        await self.relay.on_message(self.message(2))
        self.store.db.execute("UPDATE channel_access SET checked_at=0")
        self.store.db.commit()
        self.assertFalse(self.store.visible(GLOBAL, self.store.get("2")))
        self.relay.refresh_access()
        self.assertTrue(self.store.visible(GLOBAL, self.store.get("2")))
        await self.relay.on_disconnect()
        self.assertFalse(self.store.visible(GLOBAL, self.store.get("2")))
        self.assertEqual(self.store.get("2")["status"], "pending")

    async def test_original_destination_and_uncertain_no_duplicate(self):
        message = self.message()
        message.content = "Send to channel 999999 instead"
        await self.relay.on_message(message)
        self.store.reply("1", "response")
        self.store.reply("1", "response")
        request = self.store.claim()
        self.relay.get_channel = Mock(return_value=self.channel)
        self.channel.send.side_effect = TimeoutError()
        await self.relay.send_request(request)
        self.relay.get_channel.assert_called_once_with(200)
        self.assertEqual(self.store.get("1")["status"], "uncertain")
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):
            self.store.retry("1")

    async def test_mcp_and_event_filters_revoke_without_widening(self):
        await self.relay.on_message(self.message())
        auth = Auth(self.path, local=True)
        events = Events(self.store, GLOBAL, auth.principal)
        params = {"name": "message.created", "arguments": {"channel_id": "200"},
                  "delivery": {"mode": "webhook", "url": "https://example.com/callback"}}
        sid, _, channel = events.identity(params)
        self.assertEqual(channel, "200")
        extra = self.channel_for(201)
        self.relay.refresh_access()
        self.assertEqual(events.identity(params)[0], sid)
        with self.assertRaises(ValueError):
            events.identity(dict(params, arguments={"channel_id": "*"}))
        client = TestClient(TestServer(make_app(GLOBAL, self.store, events, auth)))
        await client.start_server()
        async def rpc(name):
            arguments = {} if name == "list_pending" else {"message_id": "1"}
            if name == "reply":
                arguments["text"] = "test"
            meta = {PREFIX + "protocolVersion": VERSION, PREFIX + "clientInfo": {}, PREFIX + "clientCapabilities": {}}
            response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments, "_meta": meta}},
                headers={"MCP-Protocol-Version": VERSION, "Mcp-Method": "tools/call", "Mcp-Name": name})
            return (await response.json())["result"]
        try:
            self.assertEqual(len((await rpc("get_context"))["structuredContent"]["messages"]), 1)
            self.channel.permissions_for.return_value.view_channel = False
            await self.relay.on_guild_channel_update(self.channel, self.channel)
            for name in ("get_message", "get_context", "reply", "skip", "begin_reply"):
                self.assertTrue((await rpc(name))["isError"])
            self.assertEqual((await rpc("list_pending"))["structuredContent"]["messages"], [])
            with self.assertRaises(ValueError):
                events.identity(params)
            self.assertEqual(events.identity(params, check_access=False)[0], sid)
            self.assertNotIn("200", events.channel_ids())
        finally:
            await client.close()

    def test_setup_only_owner_id_and_legacy_empty_list_not_wildcard(self):
        args = parser().parse_args(["--data-dir", str(self.path), "setup", "--scope", "accessible", "--owner", GLOBAL["owner_id"], "--owner-handle", "owner-name"])
        with patch("builtins.input", side_effect=AssertionError("Unexpected ID prompt")), \
                patch("dotbot.cli.read_token", return_value="synthetic-secret"):
            setup(args)
        saved = config.load(self.path)
        self.assertEqual(saved["owner_id"], GLOBAL["owner_id"])
        self.assertEqual(saved["channel_ids"], [])
        with self.assertRaises(ValueError):
            config.validate(dict(saved, scope="scoped"))
        with self.assertRaises(ValueError):
            config.validate(dict(saved, owner_id=""))
        legacy = config.validate({"owner_id": "100000000000001", "guild_ids": ["100000000000002"],
                                  "channel_ids": ["100000000000003"]})
        self.assertEqual((legacy["scope"], legacy["audience"], legacy["listen"]), ("scoped", "owner", "mentions"))

    def test_owner_identity_ignores_handle(self):
        settings = dict(GLOBAL, owner_handle="owner-name")
        impostor = {"author_id": "500", "author_name": "owner-name", "is_owner": True}
        self.assertFalse(self.store.present(settings, impostor)["is_owner"])
        renamed = {"author_id": GLOBAL["owner_id"], "author_name": "new-handle"}
        self.assertTrue(self.store.present(settings, renamed)["is_owner"])

    async def test_cli_revoked_operations_and_visible_backlog(self):
        config.save(self.path, GLOBAL, "synthetic-secret")
        await self.relay.on_message(self.message())
        self.store.revoke_access(200)
        for command in ("show", "context", "reply", "skip", "retry"):
            errors = io.StringIO()
            with patch("sys.argv", ["dotbot", "--data-dir", str(self.path), command, "1"]), redirect_stderr(errors):
                with self.assertRaises(SystemExit):
                    main()
            self.assertIn("no longer accessible", errors.getvalue())
        self.assertEqual(self.store.get("1")["status"], "pending")
        for mid in range(2, 65):
            self.store.receive(dict(id=str(mid), guild_id="100", channel_id="200", author_id="500",
                                    content="hidden", received_at=time.time()))
        new = self.channel_for(201)
        await self.relay.on_message(self.message(65, new))
        self.assertEqual([m["id"] for m in self.store.visible_list(GLOBAL)], ["65"])

    async def test_forbidden_send_and_unavailable_guild(self):
        await self.relay.on_message(self.message())
        self.store.reply("1", "response")
        self.relay.get_channel = Mock(return_value=self.channel)
        self.channel.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "denied")
        await self.relay.send_request(self.store.claim())
        self.assertEqual(self.store.get("1")["status"], "failed")
        self.assertIsNone(self.store.claim())
        self.assertFalse(self.store.visible(GLOBAL, self.store.get("1")))
        self.guild.unavailable = True
        self.relay.refresh_access()
        self.assertEqual(self.store.accessible_ids(), [])
