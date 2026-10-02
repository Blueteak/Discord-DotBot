import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dotbot.locking import relay_lock
from dotbot.store import Store
from dotbot.watcher import wait


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path)
        self.settings = {"owner_id": "1", "guild_ids": ["2"], "channel_ids": ["3"],
                         "listen": "channels", "audience": "owner"}

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def receive(self, mid, **overrides):
        row = dict(id=str(mid), guild_id="2", channel_id="3", author_id="1",
                   content="untrusted", received_at=time.time())
        row.update(overrides)
        self.store.receive(row)

    def watch(self):
        return wait(self.store, self.settings, self.path, 1)

    def test_resume_backlog_without_cursor_or_snowflake_order(self):
        for mid in range(150):
            self.receive(mid, channel_id="disallowed")
        self.receive(999, received_at=1)
        self.receive(500, received_at=2)
        self.assertEqual(self.watch()["message"]["id"], "999")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.watch()["message"]["id"], "999")
        self.store.skip("999")
        self.assertEqual(self.watch()["message"]["id"], "500")
        self.store.reply("500", "reply")
        self.receive(400, received_at=3)
        self.assertEqual(self.watch()["message"]["id"], "400")

    def test_context_and_read_scope_and_mentions(self):
        self.receive(1, guild_id="other")
        self.receive(2, author_id="other")
        self.receive(3, channel_id="other")
        self.receive(4, mentioned=0)
        self.receive(5, mentioned=1)
        self.settings["listen"] = "mentions"
        result = self.watch()
        self.assertEqual(result["message"]["id"], "5")
        self.assertEqual([r["id"] for r in result["context"]], ["5"])
        self.assertIn("untrusted", result["policy"])
        self.assertFalse(result["relay"]["connected"])

    def test_channel_audience_supports_other_users(self):
        self.receive(1, author_id="another-user")
        self.settings["audience"] = "channel"
        self.assertEqual(self.watch()["message"]["id"], "1")

    def test_timeout_lock_and_cancel_cleanup(self):
        with patch("dotbot.watcher.time.monotonic", side_effect=[10, 11]):
            self.assertEqual(self.watch()["event"], "timeout")
        with relay_lock(self.path):
            with relay_lock(self.path, "watcher"):
                with self.assertRaisesRegex(ValueError, "watcher"):
                    self.watch()
            with patch("dotbot.watcher.time.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.watch()
            self.receive(1)
            self.assertEqual(self.watch()["message"]["id"], "1")

    def test_reply_skip_races_and_uncertain_not_replayed(self):
        self.receive(1)
        self.receive(2)
        second = Store(self.path)
        try:
            self.store.reply("1", "hello")
            self.assertEqual(second.reply("1", "hello")["status"], "queued")
            with self.assertRaises(ValueError):
                second.reply("1", "different")
            with self.assertRaises(ValueError):
                second.skip("1")
            self.store.skip("2")
            with self.assertRaises(ValueError):
                second.reply("2", "too late")
            self.assertEqual(self.store.claim()["id"], "1")
            self.assertIsNone(second.claim())
            second.recover()
            self.assertEqual(self.store.get("1")["status"], "uncertain")
            with self.assertRaises(ValueError):
                self.store.retry("1")
            with patch("dotbot.watcher.time.monotonic", side_effect=[10, 11]):
                self.assertEqual(self.watch()["event"], "timeout")
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()
