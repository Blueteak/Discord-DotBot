"""Regressions using the pinned discord.py gateway parsers and real caches."""
import asyncio
import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord.gateway import ReconnectWebSocket

from dotbot.events import Events
from dotbot.relay import Relay
from dotbot.store import Store

SETTINGS = dict(owner_id='123456789012345', scope='accessible', listen='channels', audience='channel')
STAMP = '2026-01-01T00:00:00+00:00'


class GatewayCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.relay = Relay(SETTINGS, self.store)
        await self.relay._async_setup_hook()
        self.state = self.relay._connection
        self.state.user = discord.ClientUser(state=self.state, data=dict(id='999', username='bot', discriminator='0', avatar=None))
        permissions = discord.Permissions(view_channel=True, read_message_history=True, send_messages=True, send_messages_in_threads=True)
        self.guild = discord.Guild(state=self.state, data=dict(id='100', name='test', owner_id='111',
            roles=[dict(id='100', name='@everyone', permissions=str(permissions.value), position=0, color=0, hoist=False, managed=False)],
            members=[dict(user=dict(id='999', username='bot', discriminator='0', avatar=None), roles=[], joined_at=STAMP, flags=0)],
            channels=[dict(id='200', type=0, name='parent', position=0, permission_overwrites=[])]))
        self.state._guilds[100] = self.guild
        self.relay._ready.set()
        await self.relay.on_ready()

    async def asyncTearDown(self):
        await self.relay.close()
        self.store.close()
        self.temp.cleanup()

    def member(self):
        return dict(id='300', user_id='999', join_timestamp=STAMP, flags=0)

    def thread(self, member=True):
        data = dict(id='300', guild_id='100', parent_id='200', owner_id='111', name='private', type=12,
            message_count=0, member_count=int(member), thread_metadata=dict(archived=False, locked=False,
            archive_timestamp=STAMP, auto_archive_duration=60))
        if member:
            data['member'] = self.member()
        return data

    async def receive(self, channel, mid=1):
        await self.relay.on_message(SimpleNamespace(id=mid, guild=self.guild, channel=channel,
            author=SimpleNamespace(id=500, bot=False, display_name='guest'), webhook_id=None,
            mentions=[], content='please explain', reference=None))
        return self.store.get(str(mid))

    async def test_disconnect_real_connect_and_delivery_tick(self):
        row = await self.receive(self.guild.get_channel(200))
        reconnecting = asyncio.Event()
        first = SimpleNamespace(poll_event=AsyncMock(side_effect=ReconnectWebSocket(None)), sequence=1, session_id='offline', gateway='offline')
        calls = 0
        async def factory(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first
            reconnecting.set()
            await asyncio.Future()
        with patch('discord.client.DiscordWebSocket.from_client', side_effect=factory):
            connect = asyncio.create_task(self.relay.connect())
            await asyncio.wait_for(reconnecting.wait(), 1)
            await asyncio.sleep(0)
            self.assertTrue(self.relay.is_ready())
            self.assertFalse(self.store.connected())
            worker = asyncio.create_task(self.relay.deliver())
            await asyncio.sleep(0)  # actual refresh/heartbeat/claim iteration
            self.assertFalse(self.store.connected())
            self.assertFalse(self.store.visible(SETTINGS, row))
            self.assertEqual(self.store.accessible_ids(), [])
            for task in (worker, connect):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.relay.ws = None
        await self.relay.on_resumed()
        self.assertTrue(self.store.visible(SETTINGS, row))

    async def test_private_sync_removal_refresh_and_member_update_rejoin(self):
        self.state.parse_thread_list_sync(dict(guild_id='100', threads=[self.thread(False)], members=[self.member()]))
        await asyncio.sleep(0)
        thread = self.guild.get_thread(300)
        self.assertIsInstance(thread, discord.Thread)
        self.assertFalse(thread.permissions_for(self.guild.me).manage_threads)
        self.assertTrue(self.relay.channel_accessible(thread))
        row = await self.receive(thread)
        self.assertTrue(self.store.visible(SETTINGS, row))
        self.state.parse_thread_members_update(dict(guild_id='100', id='300', member_count=0, removed_member_ids=['999']))
        await asyncio.sleep(0)
        self.assertIs(self.guild.get_thread(300), thread)
        self.assertIsNone(thread.me)
        self.assertNotIn(999, [member.id for member in thread.members])
        self.relay.refresh_access()
        self.assertFalse(self.store.visible(SETTINGS, row))
        await self.relay.on_resumed()
        self.assertFalse(self.store.visible(SETTINGS, row))
        self.state.parse_thread_member_update(dict(self.member(), guild_id='100'))
        self.relay.refresh_access()
        self.assertTrue(self.store.visible(SETTINGS, row))
        self.state.parse_thread_members_update(dict(guild_id='100', id='300', member_count=0, removed_member_ids=['999']))
        await asyncio.sleep(0)
        self.state.parse_thread_members_update(dict(guild_id='100', id='300', member_count=1, added_members=[self.member()]))
        await asyncio.sleep(0)
        self.assertTrue(self.store.visible(SETTINGS, row))
        self.state.parse_thread_list_sync(dict(guild_id='100', threads=[self.thread(False)], members=[]))
        self.relay.refresh_access()
        self.assertFalse(self.store.visible(SETTINGS, row))

    async def test_persisted_callback_waits_for_connection_and_snapshot(self):
        events = Events(self.store, SETTINGS, 'owner')
        params = dict(name='message.created', arguments=dict(channel_id='200'), delivery=dict(mode='webhook', url='https://example.com/callback', secret='whsec_' + 'c' * 32))
        async def verify(sub, event, payload):
            import json
            return 200, json.dumps(dict(challenge=payload['challenge'])).encode()
        with patch('dotbot.events.post_signed', side_effect=verify):
            await events.subscribe(params)
        row = await self.receive(self.guild.get_channel(200))
        await self.relay.on_disconnect()
        # Reopen the DB and event worker as on a serve restart.
        self.store.close()
        self.store = Store(Path(self.temp.name))
        self.relay.store = self.store
        events = Events(self.store, SETTINGS, 'owner')
        with patch('dotbot.events.post_signed', new_callable=AsyncMock, return_value=(200, b'')) as callback:
            for _ in range(2):
                self.assertFalse(await events.tick())
            delivery = self.store.db.execute('SELECT status, attempts FROM deliveries').fetchone()
            self.assertEqual(tuple(delivery), ('pending', 0))
            callback.assert_not_awaited()
            await self.relay.on_ready()
            self.store.db.execute('UPDATE channel_access SET checked_at=0')
            self.store.db.commit()
            self.assertFalse(await events.tick())
            callback.assert_not_awaited()
            self.relay.refresh_access()
            self.assertTrue(await events.tick())
            callback.assert_awaited_once()
            self.assertEqual(callback.call_args.args[2]['data']['channel_id'], '200')
            self.assertEqual(self.store.db.execute('SELECT status FROM deliveries').fetchone()[0], 'delivered')
            await self.receive(self.guild.get_channel(200), 2)
            self.store.revoke_access(200)
            self.assertTrue(await events.tick())
            self.assertEqual(callback.await_count, 1)
            self.assertEqual(self.store.db.execute("SELECT status FROM deliveries WHERE request_id='2'").fetchone()[0], 'stopped')
