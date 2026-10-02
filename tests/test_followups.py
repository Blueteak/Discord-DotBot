import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from dotbot.relay import Relay
from dotbot.store import Store
from test_relay import SETTINGS, GUILD, CHANNEL, message


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path)
        self.mid = message()['id']
        self.store.receive(message())
        self.store.reply(self.mid, 'Acknowledged')
        self.store.claim()
        self.store.finish(self.mid, 'sent', reply_id='101')
        self.channel = SimpleNamespace(id=int(CHANNEL), guild=SimpleNamespace(id=int(GUILD)),
                                       send=AsyncMock(return_value=SimpleNamespace(id=102)))
        self.relay = Relay(SETTINGS, self.store)
        self.relay.get_channel = Mock(return_value=self.channel)
        self.relay.channel_accessible = Mock(return_value=True)

    async def asyncTearDown(self):
        self.store.close()
        self.temp.cleanup()

    async def test_distinct_followups_and_same_key_never_requeue(self):
        ids = []
        for key in ('result', 'confirmation'):
            op = self.store.followup(self.mid, key, 'Done')
            ids.append(op['operation_id'])
            self.assertEqual(self.store.followup(self.mid, key, 'Done'), op)
            request = self.store.claim()
            self.assertEqual(self.store.followup(self.mid, key, 'Done')['status'], 'sending')
            self.assertIsNone(self.store.claim())
            await self.relay.send_request(request)
            self.assertEqual(self.store.followup(self.mid, key, 'Done')['status'], 'sent')
            self.assertIsNone(self.store.claim())
            with self.assertRaises(ValueError):
                self.store.followup(self.mid, key, 'Changed')
        self.assertNotEqual(*ids)
        self.assertEqual(self.channel.send.call_count, 2)
        calls = self.channel.send.call_args_list
        self.assertNotEqual(calls[0].kwargs['nonce'], calls[1].kwargs['nonce'])
        for call in calls:
            self.assertEqual(call.kwargs['reference'].message_id, int(self.mid))
            self.assertEqual(call.kwargs['reference'].channel_id, int(CHANNEL))
            self.assertEqual(call.kwargs['allowed_mentions'].to_dict()['parse'], [])
        parent = self.store.get(self.mid)
        self.assertEqual((parent['status'], parent['reply'], parent['reply_id']), ('sent', 'Acknowledged', '101'))
        self.assertEqual(len(parent['followups']), 2)
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(ValueError):
            self.store.skip(self.mid)
        with self.assertRaises(ValueError):
            self.store.reply(self.mid, 'Changed')

    async def test_queued_restart_and_uncertain_recovery(self):
        op = self.store.followup(self.mid, 'result', 'Done')
        self.store.close()
        self.store = Store(self.path)
        self.store.recover()
        self.assertEqual(self.store.followup(self.mid, 'result', 'Done')['status'], 'queued')
        self.store.claim()
        self.store.recover()
        self.assertEqual(self.store.followup(self.mid, 'result', 'Done')['status'], 'uncertain')
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):
            self.store.retry_followup(self.mid, 'result')
        self.store.retry_followup(self.mid, 'result', True)
        self.assertEqual(self.store.claim()['operation_id'], op['operation_id'])
        self.assertIsNone(self.store.claim())

    async def test_timeout_is_uncertain_and_not_retried(self):
        self.store.followup(self.mid, 'result', 'Done')
        self.channel.send.side_effect = asyncio.TimeoutError()
        await self.relay.send_request(self.store.claim())
        self.assertEqual(self.store.get(self.mid)['followups'][0]['status'], 'uncertain')
        self.assertEqual(self.store.followup(self.mid, 'result', 'Done')['status'], 'uncertain')
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.get(self.mid)['status'], 'sent')

    async def test_scope_change_and_access_revocation_fail_closed(self):
        for key, settings in [('scope', dict(SETTINGS, channel_ids=[])),
                              ('revoked', dict(SETTINGS, scope='accessible'))]:
            self.store.followup(self.mid, key, 'Done')
            self.relay.config = settings
            self.store.set_access([(GUILD, CHANNEL)])
            self.store.heartbeat(True)
            if key == 'revoked':
                self.store.revoke_access(CHANNEL)
            await self.relay.send_request(self.store.claim())
            self.assertEqual(self.store.followup(self.mid, key, 'Done')['status'], 'failed')
            self.channel.send.assert_not_called()
        self.relay.config = SETTINGS
        self.store.retry_followup(self.mid, 'scope')
        await self.relay.send_request(self.store.claim())
        self.assertEqual(self.store.followup(self.mid, 'scope', 'Done')['status'], 'sent')

    async def test_requires_confirmed_initial_reply_and_valid_input(self):
        for status in ('pending', 'queued', 'sending', 'uncertain', 'failed', 'skipped'):
            self.store.db.execute('UPDATE requests SET status=? WHERE id=?', (status, self.mid))
            self.store.db.commit()
            with self.assertRaises(ValueError):
                self.store.followup(self.mid, 'result', 'Done')
        self.store.db.execute("UPDATE requests SET status='sent'")
        self.store.db.commit()
        for key, text in [('', 'Done'), ('bad key', 'Done'), ('x'*129, 'Done'), ('ok', ''), ('ok', '😀'*1001)]:
            with self.assertRaises(ValueError):
                self.store.followup(self.mid, key, text)

    async def test_standalone_idempotency_destination_and_uncertainty(self):
        self.store.set_access([(GUILD, CHANNEL)])
        self.store.heartbeat(True)
        op = self.store.send(SETTINGS, CHANNEL, 'progress', 'Working')
        self.assertIsNone(op['request_id'])
        self.assertEqual(self.store.send(SETTINGS, CHANNEL, 'progress', 'Working'), op)
        with self.assertRaises(ValueError):
            self.store.send(SETTINGS, CHANNEL, 'progress', 'Different')
        await self.relay.send_request(self.store.claim())
        self.assertIsNone(self.channel.send.call_args.kwargs['reference'])
        self.assertEqual(self.store.operation(SETTINGS, op['operation_id'])['status'], 'sent')
        self.assertEqual(self.store.send(SETTINGS, CHANNEL, 'progress', 'Working')['status'], 'sent')
        self.assertIsNone(self.store.claim())
        other = self.store.send(SETTINGS, CHANNEL, 'complete', 'Done')
        self.channel.send.side_effect = asyncio.TimeoutError()
        await self.relay.send_request(self.store.claim())
        self.assertEqual(self.store.operation(SETTINGS, other['operation_id'])['status'], 'uncertain')
        with self.assertRaises(ValueError):
            self.store.retry_operation(SETTINGS, other['operation_id'])
        self.assertIsNone(self.store.claim())
        self.store.retry_operation(SETTINGS, other['operation_id'], True)
        self.assertEqual(self.store.claim()['operation_id'], other['operation_id'])
        self.assertEqual(len(self.store.list('all')), 1)

    async def test_standalone_fails_closed_at_enqueue_and_send(self):
        with self.assertRaises(ValueError):
            self.store.send(SETTINGS, CHANNEL, 'result', 'Done')
        self.store.set_access([(GUILD, CHANNEL)])
        self.store.heartbeat(True)
        with self.assertRaises(ValueError):
            self.store.send(dict(SETTINGS, channel_ids=[]), CHANNEL, 'result', 'Done')
        self.store.db.execute('UPDATE channel_access SET checked_at=0')
        self.store.db.commit()
        with self.assertRaises(ValueError):
            self.store.send(SETTINGS, CHANNEL, 'result', 'Done')
        for key in ('revoked', 'scope', 'live'):
            self.store.set_access([(GUILD, CHANNEL)])
            op = self.store.send(SETTINGS, CHANNEL, key, 'Done')
            request = self.store.claim()
            self.relay.config = SETTINGS
            if key == 'revoked':
                self.store.revoke_access(CHANNEL)
            elif key == 'scope':
                self.relay.config = dict(SETTINGS, channel_ids=[])
            else:
                self.relay.channel_accessible.return_value = False
            await self.relay.send_request(request)
            saved = self.store.db.execute('SELECT status FROM outbound WHERE operation_id=?', (op['operation_id'],)).fetchone()
            self.assertEqual(saved['status'], 'failed')
            self.channel.send.assert_not_called()
        self.assertEqual(self.store.get(self.mid)['status'], 'sent')

    async def test_mcp_operations_and_visibility(self):
        from aiohttp.test_utils import TestClient, TestServer
        from dotbot.events import Events
        from dotbot.mcp import Auth, make_app, PREFIX, VERSION
        settings = dict(SETTINGS, scope='accessible')
        self.store.set_access([(GUILD, CHANNEL)])
        self.store.heartbeat(True)
        auth = Auth(self.path, local=True)
        client = TestClient(TestServer(make_app(settings, self.store, Events(self.store, settings, auth.principal), auth)))
        await client.start_server()
        async def call(name, args):
            response = await client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': args, '_meta': {PREFIX+'protocolVersion': VERSION,
                PREFIX+'clientInfo': {}, PREFIX+'clientCapabilities': {}}}},
                headers={'MCP-Protocol-Version': VERSION, 'Mcp-Method': 'tools/call', 'Mcp-Name': name})
            return (await response.json())['result']
        try:
            args = {'message_id': self.mid, 'operation_key': 'result', 'text': 'Done'}
            result = await call('followup', args)
            self.assertEqual(result['structuredContent']['status'], 'queued')
            self.assertEqual((await call('followup', args))['structuredContent'], result['structuredContent'])
            sent = await call('send', {'channel_id': CHANNEL, 'operation_key': 'progress', 'text': 'Working'})
            oid = sent['structuredContent']['operation_id']
            self.assertEqual((await call('get_operation', {'operation_id': oid}))['structuredContent']['status'], 'queued')
            self.store.revoke_access(CHANNEL)
            for name, args in [('followup', args), ('get_operation', {'operation_id': oid}),
                               ('send', {'channel_id': CHANNEL, 'operation_key': 'next', 'text': 'No'})]:
                self.assertTrue((await call(name, args))['isError'])
        finally:
            await client.close()
