import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from dotbot import config
from dotbot.cli import parser
from dotbot.gui import Window
from dotbot.gui_worker import launch
from dotbot.launcher import Launcher, LauncherError, FAILURE
from dotbot.locking import relay_lock
from dotbot.store import Store

SETTINGS = dict(owner_id='123456789012345', owner_handle='display only', scope='accessible',
                guild_ids=[], channel_ids=[], listen='channels', audience='channel')
SECRET = 'synthetic-not-a-real-token'


class Process:
    def __init__(self):
        self.stdin = io.StringIO()
        self.code = None
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1
        self.code = -9


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / 'private'
        self.process = Process()
        self.spawn = Mock(return_value=self.process)

    def tearDown(self):
        self.tmp.cleanup()

    def controller(self):
        return Launcher(self.directory, spawn=self.spawn)

    def test_open_never_creates_data_or_connects(self):
        app = self.controller()
        self.assertFalse(self.directory.exists())
        self.assertFalse(app.busy)
        self.assertEqual(app.status, 'Not connected')
        app.poll()
        self.spawn.assert_not_called()
        self.assertEqual(parser().parse_args(['gui']).command, 'gui')

    def test_existing_config_preserves_legacy_and_never_reads_token(self):
        config.save(self.directory, dict(SETTINGS, scope='scoped', guild_ids=['123456789012346'],
                    channel_ids=['123456789012347'], audience='owner', listen='mentions'), SECRET)
        original = Path.read_text
        def safe_read(path, *args, **kwargs):
            self.assertNotEqual(path.name, 'token')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', safe_read):
            app = self.controller()
            self.assertTrue(app.saved_token)
            self.assertEqual(app.settings['scope'], 'scoped')
            self.assertEqual(app.settings['audience'], 'owner')
            app.connect(app.settings)
        payload = json.loads(self.process.stdin.getvalue())
        self.assertEqual(payload['secret'], '')
        self.spawn.assert_called_once()

    def test_invalid_owner_and_token_never_spawn_or_save(self):
        app = self.controller()
        for owner in ('', 'handle', '123', '１２３４５６７８９０１２３４５'):
            with self.assertRaises(LauncherError):
                app.connect(dict(SETTINGS, owner_id=owner), SECRET)
        for secret in ('', 'contains space', 'newline\n', 'x' * 513):
            with self.assertRaises(LauncherError):
                app.connect(SETTINGS, secret)
        self.spawn.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_double_click_secret_transport_and_proxy_opt_in(self):
        app = self.controller()
        with patch.dict(os.environ, DISCORD_BOT_TOKEN='unrelated-environment-secret'):
            self.assertTrue(app.connect(SETTINGS, SECRET, proxy=True))
            self.assertFalse(app.connect(SETTINGS, SECRET))
        self.spawn.assert_called_once()
        args, kwargs = self.spawn.call_args
        self.assertNotIn(SECRET, str(args))
        self.assertNotIn('DISCORD_BOT_TOKEN', kwargs['env'])
        self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertTrue(json.loads(self.process.stdin.getvalue())['proxy'])
        self.assertFalse(self.process.stdin.closed)

    def test_worker_first_setup_permissions_and_lock_held(self):
        def relay(settings, store, directory, secret, **options):
            self.assertEqual(secret, SECRET)
            self.assertEqual(settings, SETTINGS)
            self.assertTrue(options['_lock_held'])
            self.assertFalse(options['proxy_from_env'])
            with self.assertRaises(ValueError):
                with relay_lock(directory):
                    pass
        with patch('dotbot.gui_worker.run', side_effect=relay) as run:
            launch(self.directory, dict(settings=SETTINGS, secret=SECRET, proxy=False))
        run.assert_called_once()
        self.assertEqual(config.load(self.directory), SETTINGS)
        if os.name != 'nt':
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
            for name in ('config.json', 'token', 'relay.sqlite'):
                self.assertEqual((self.directory/name).stat().st_mode & 0o777, 0o600)
        with relay_lock(self.directory):
            pass

    def test_lock_conflict_does_not_change_saved_settings_or_token(self):
        config.save(self.directory, SETTINGS, SECRET)
        with relay_lock(self.directory), patch('dotbot.gui_worker.run') as run:
            with self.assertRaises(ValueError):
                launch(self.directory, dict(settings=dict(SETTINGS, owner_handle='changed'), secret='replacement'))
        self.assertEqual(config.load(self.directory), SETTINGS)
        self.assertEqual((self.directory/'token').read_text().strip(), SECRET)
        run.assert_not_called()
        app = self.controller()
        app.connect(SETTINGS)
        self.process.code = 20
        self.assertIn('already running', app.poll())
        self.assertFalse(app.busy)

    def test_worker_failure_clears_access_and_releases_lock(self):
        def fail(settings, store, *args, **kwargs):
            store.heartbeat(True)
            store.set_access([(1, 2)])
            raise OSError(SECRET)
        with patch('dotbot.gui_worker.run', side_effect=fail):
            with self.assertRaises(OSError):
                launch(self.directory, dict(settings=SETTINGS, secret=SECRET))
        with StoreContext(self.directory) as store:
            self.assertFalse(store.connected())
            self.assertEqual(store.accessible_ids(), [])
        with relay_lock(self.directory):
            pass

    def test_spawn_failure_redacted_and_partial_child_stopped(self):
        app = Launcher(self.directory, spawn=Mock(side_effect=OSError(SECRET)))
        with self.assertRaises(LauncherError) as error:
            app.connect(SETTINGS, SECRET)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertFalse(app.busy)
        self.process.stdin = Mock()
        self.process.stdin.write.side_effect = BrokenPipeError(SECRET)
        app = self.controller()
        with self.assertRaises(LauncherError) as error:
            app.connect(SETTINGS, SECRET)
        self.assertNotIn(SECRET, str(error.exception))
        self.assertEqual(self.process.terminated, 1)
        self.process.code = 1
        app.poll()
        self.assertFalse(app.busy)

    def test_stop_restart_and_stale_health(self):
        config.save(self.directory, SETTINGS, SECRET)
        with StoreContext(self.directory) as store:
            store.heartbeat(True)
        app = self.controller()
        app.connect(SETTINGS)
        self.assertNotEqual(app.poll(), 'Discord relay connected')
        with StoreContext(self.directory) as store:
            store.heartbeat(True)
        config.private_write(self.directory / '.gui-session', app.session)
        self.assertEqual(app.poll(), 'Discord relay connected')
        app.stop()
        app.stop()
        self.assertEqual(self.process.terminated, 1)
        self.process.code = 0
        self.assertEqual(app.poll(), 'Stopped')
        self.assertTrue(self.process.stdin.closed)
        self.spawn.return_value = Process()
        self.assertTrue(app.connect(SETTINGS))
        self.assertEqual(self.spawn.call_count, 2)

    def test_existing_healthy_relay_cannot_claim_new_window_ownership(self):
        config.save(self.directory, SETTINGS, SECRET)
        config.private_write(self.directory / '.gui-session', 'old-session')
        app = self.controller()
        with relay_lock(self.directory), StoreContext(self.directory) as store:
            app.connect(SETTINGS)
            store.heartbeat(True)  # Existing relay refreshed after Connect.
            self.assertNotEqual(app.poll(), 'Discord relay connected')
            self.assertTrue(store.connected())
            app.stop()
            self.assertEqual(self.process.terminated, 1)  # Only our new child.
            self.assertTrue(store.connected())
        self.process.code = 20
        app.poll()
        self.assertFalse(app.busy)

    def test_worker_publishes_ownership_only_after_lock_and_cleared_health(self):
        session = 'a' * 32
        def relay(settings, store, directory, secret, **kwargs):
            self.assertEqual((directory / '.gui-session').read_text(), session)
            self.assertFalse(store.connected())
            with self.assertRaises(ValueError):
                with relay_lock(directory):
                    pass
        with patch('dotbot.gui_worker.run', side_effect=relay):
            launch(self.directory, dict(settings=SETTINGS, secret=SECRET, session=session))
        with relay_lock(self.directory), patch('dotbot.gui_worker.run'):
            with self.assertRaises(ValueError):
                launch(self.directory, dict(settings=SETTINGS, secret=SECRET, session='b' * 32))
        self.assertEqual((self.directory / '.gui-session').read_text(), session)

    def test_stop_timeout_kills_and_reopen_does_not_connect(self):
        app = Launcher(self.directory, spawn=self.spawn, clock=Mock(return_value=0))
        app.connect(SETTINGS, SECRET)
        app.stop()
        app.clock.return_value = 6
        app.poll()
        self.assertEqual(self.process.killed, 1)
        self.assertEqual(app.poll(), 'Stopped')
        reopened = self.controller()
        self.assertFalse(reopened.busy)
        self.assertEqual(self.spawn.call_count, 1)

    def test_account_next_clears_error_without_saving_or_connecting(self):
        window = Window.__new__(Window)
        window.controller = self.controller()
        window.page = 0
        window.closing = False
        window.connect_after = 0
        window.status = Mock()
        window.secret = Mock(get=Mock(return_value=SECRET))
        window.owner = Mock(get=Mock(return_value='invalid'))
        window.values = Mock(return_value=dict(SETTINGS, owner_id='invalid'))
        window.render = Mock()
        window.next_or_connect()
        self.assertEqual(window.page, 0)
        self.assertIn('15 to 20', window.status.set.call_args.args[0])
        window.values.return_value = SETTINGS
        window.owner.get.return_value = SETTINGS['owner_id']
        window.next_or_connect()
        self.assertEqual(window.page, 1)
        window.status.set.assert_called_with('Not connected')
        window.next_or_connect()  # Immediate second click cannot Connect.
        self.spawn.assert_not_called()
        self.assertFalse(self.directory.exists())
        window.page = 3
        window.connect_after = 0
        window.next_or_connect()
        self.assertEqual(window.page, 1)
        self.spawn.assert_not_called()

    def test_gui_owner_id_blocks_handles_and_non_ascii_digits(self):
        window = Window.__new__(Window)
        window.controller = self.controller()
        window.closing = False
        window.status = Mock()
        window.secret = Mock(get=Mock(return_value=SECRET))
        window.owner = Mock()
        window.values = Mock(return_value=SETTINGS)
        window.render = Mock()
        for value in ('@someone', 'someone', '123abc456789012', '１２３４５６７８９０１２３４５',
                      '1' * 14, '1' * 21, ''):
            window.page = 0
            window.connect_after = 0
            window.owner.get.return_value = value
            window.next_or_connect()
            self.assertEqual(window.page, 0)
            self.assertEqual(window.status.set.call_args.args[0], 'Use a 15 to 20 digit user ID, not your @handle.')
        for value in ('1' * 15, '1' * 20, ' 123456789012345 '):
            window.page = 0
            window.connect_after = 0
            window.owner.get.return_value = value
            window.next_or_connect()
            self.assertEqual(window.page, 1)
        self.spawn.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_gui_values_preserve_existing_handle_without_a_field(self):
        window = Window.__new__(Window)
        window.controller = self.controller()
        window.controller.settings = dict(SETTINGS)
        for name, value in (("owner", SETTINGS['owner_id']), ("scope", 'accessible'),
                            ("guilds", ''), ("channels", ''), ("listen", False), ("audience", False)):
            setattr(window, name, Mock(get=Mock(return_value=value)))
        self.assertFalse(hasattr(window, 'handle'))
        self.assertEqual(window.values()['owner_handle'], SETTINGS['owner_handle'])
        self.assertEqual(window.values()['owner_id'], SETTINGS['owner_id'])

    def test_window_close_requests_stop_and_waits(self):
        window = Window.__new__(Window)
        window.controller = self.controller()
        window.controller.connect(SETTINGS, SECRET)
        window.root = Mock()
        window.secret = Mock()
        window.status = Mock()
        window.set_busy = Mock()
        window.close()
        window.secret.set.assert_called_with('')
        self.assertEqual(self.process.terminated, 1)
        window.root.destroy.assert_not_called()
        self.process.code = 0
        window.tick()
        window.root.destroy.assert_called_once()

    def test_repository_data_and_corrupt_config_rejected(self):
        (Path(self.tmp.name)/'dotbot').mkdir()
        (Path(self.tmp.name)/'dotbot'/'cli.py').touch()
        (Path(self.tmp.name)/'pyproject.toml').touch()
        with self.assertRaises(LauncherError):
            self.controller()
        (Path(self.tmp.name)/'pyproject.toml').unlink()
        self.directory.mkdir()
        (self.directory/'config.json').write_text('invalid ' + SECRET)
        app = self.controller()
        self.assertNotIn(SECRET, app.status)
        with self.assertRaises(LauncherError):
            app.connect(SETTINGS, SECRET)
        self.spawn.assert_not_called()

    def test_platform_git_parent_allows_external_ignored_runtime(self):
        repository = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        app = self.controller()
        self.assertFalse(self.directory.exists())  # Opening still has no writes.
        with patch('dotbot.gui_worker.run'):
            launch(self.directory, dict(settings=SETTINGS, secret=SECRET))
        paths = ['token', 'config.json', 'relay.sqlite', 'relay.sqlite-wal', '.dotbot-future', 'relay.lock', '.gitignore']
        result = subprocess.run(['git', '-C', str(repository), 'check-ignore', '--stdin'],
            input='\n'.join('private/' + name for name in paths) + '\n', text=True, capture_output=True, check=True)
        self.assertEqual(len(result.stdout.splitlines()), len(paths))
        subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
        tracked = subprocess.run(['git', '-C', str(repository), 'ls-files'], capture_output=True, text=True, check=True)
        self.assertEqual(tracked.stdout, '')
        self.assertTrue((repository/'.git').is_dir())
        self.assertFalse((repository/'.gitignore').exists())
        self.assertTrue(self.controller().saved_token)

    def test_platform_parent_rejects_previously_tracked_runtime(self):
        repository = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        self.directory.mkdir()
        (self.directory/'config.json').write_text('{}')
        subprocess.run(['git', '-C', str(repository), 'add', 'private/config.json'], check=True)
        with self.assertRaises(LauncherError):
            self.controller()
        with self.assertRaises(LauncherError), patch('dotbot.gui_worker.run'):
            launch(self.directory, dict(settings=SETTINGS, secret=SECRET))
        self.assertFalse((self.directory/'token').exists())

    def test_placeholder_git_parent_is_not_a_repository(self):
        placeholder = Path(self.tmp.name) / '.git'
        placeholder.mkdir()
        result = subprocess.run(['git', '-C', self.tmp.name, 'rev-parse', '--show-toplevel'], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        app = self.controller()
        self.assertFalse(self.directory.exists())
        with patch('dotbot.gui_worker.run'):
            launch(self.directory, dict(settings=SETTINGS, secret=SECRET))
        self.assertTrue(app.directory.joinpath('.gitignore').is_file())
        self.assertEqual(list(placeholder.iterdir()), [])

    def test_real_repository_probe_and_index_errors_fail_closed(self):
        repository = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        original = subprocess.run
        def failed_probe(command, **kwargs):
            if 'rev-parse' in command:
                return subprocess.CompletedProcess(command, 128, b'', b'fatal: dubious ownership')
            return original(command, **kwargs)
        with patch('dotbot.launcher.subprocess.run', side_effect=failed_probe):
            with self.assertRaises(LauncherError):
                self.controller()
        (repository / '.git' / 'index').write_bytes(b'invalid-index')
        with self.assertRaises(LauncherError):
            self.controller()
        self.assertFalse(self.directory.exists())

    def test_nested_repository_cannot_hide_outer_tracked_token(self):
        repository = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(repository)], check=True)
        self.directory.mkdir()
        token = self.directory / 'token'
        token.write_text(SECRET)
        subprocess.run(['git', '-C', str(repository), 'add', 'private/token'], check=True)
        subprocess.run(['git', 'init', '-q', str(self.directory)], check=True)
        with self.assertRaises(LauncherError):
            self.controller()
        with patch('dotbot.gui_worker.run') as run:
            with self.assertRaises(LauncherError):
                launch(self.directory, dict(settings=SETTINGS, secret='synthetic-replacement'))
        run.assert_not_called()
        self.assertEqual(token.read_text(), SECRET)
        self.assertFalse((self.directory / '.gitignore').exists())
        result = subprocess.run(['git', '-C', str(repository), 'diff', '--name-only'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, '')

    def test_failure_exit_status_is_generic(self):
        app = self.controller()
        app.connect(SETTINGS, SECRET)
        self.process.code = 1
        self.assertEqual(app.poll(), FAILURE)
        self.assertNotIn(SECRET, app.status)

    def test_scoped_mode_requires_ids(self):
        app = self.controller()
        with self.assertRaises(LauncherError):
            app.connect(dict(SETTINGS, scope='scoped'), SECRET)
        app.connect(dict(SETTINGS, scope='scoped', guild_ids=['123456789012346'], channel_ids=['123456789012347']), SECRET)
        self.assertEqual(json.loads(self.process.stdin.getvalue())['settings']['scope'], 'scoped')

    def test_missing_tk_and_no_display_are_friendly(self):
        from dotbot.gui import main
        with patch.dict(sys.modules, tkinter=None), patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(main(self.directory), 1)
            self.assertIn('Tkinter is missing', error.getvalue())

    def test_no_display_error_does_not_include_exception_details(self):
        from dotbot.gui import main
        from types import SimpleNamespace
        tk = SimpleNamespace(TclError=RuntimeError, Tk=Mock(side_effect=RuntimeError(SECRET)), ttk=object())
        with patch.dict(sys.modules, tkinter=tk), patch('sys.stderr', new_callable=io.StringIO) as error:
            self.assertEqual(main(self.directory), 1)
            self.assertIn('graphical desktop', error.getvalue())
            self.assertNotIn(SECRET, error.getvalue())

    def test_real_worker_failure_exits_silently_with_parent_pipe_open(self):
        code = ('import dotbot.gui_worker as w; '
                'w.run=lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic-private-error")); '
                'raise SystemExit(w.main())')
        child = subprocess.Popen([sys.executable, '-c', code, '--data-dir', str(self.directory)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            child.stdin.write(json.dumps(dict(settings=SETTINGS, secret=SECRET)) + '\n')
            child.stdin.flush()
            self.assertEqual(child.wait(timeout=5), 1)
            self.assertEqual(child.stdout.read(), '')
            self.assertEqual(child.stderr.read(), '')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()

    def test_parent_pipe_eof_force_exits_stuck_actual_relay_cleanup(self):
        code = """
import asyncio
from pathlib import Path
import sys
import dotbot.relay as relay
import dotbot.gui_worker as worker
folder = Path(sys.argv[2])
class FakeDiscord:
    def __init__(self, *args, **kwargs):
        self.worker = None
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        (folder / 'cleanup-started').touch()
        await asyncio.Future()
    async def start(self, token):
        (folder / 'transport-ready').touch()
        await asyncio.Future()
relay.Relay = FakeDiscord
raise SystemExit(worker.main())
"""
        child = subprocess.Popen([sys.executable, '-c', code, '--data-dir', str(self.directory)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            child.stdin.write(json.dumps(dict(settings=SETTINGS, secret=SECRET)) + '\n')
            child.stdin.flush()
            deadline = time.monotonic() + 5
            while not (self.directory/'transport-ready').exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.directory/'transport-ready').exists())
            start = time.monotonic()
            child.stdin.close()
            self.assertEqual(child.wait(timeout=8), 1)
            self.assertLess(time.monotonic() - start, 7)
            self.assertTrue((self.directory/'cleanup-started').exists())
            self.assertEqual(child.stdout.read(), '')
            self.assertEqual(child.stderr.read(), '')
            with relay_lock(self.directory):
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()
            child.stderr.close()

    def test_real_child_pipe_death_exits_without_authentication(self):
        # Run the actual private worker, replacing only authenticated relay.run.
        code = ('import time; import dotbot.gui_worker as w; '
                'w.run=lambda *a, **k: time.sleep(30); raise SystemExit(w.main())')
        child = subprocess.Popen([sys.executable, '-c', code, '--data-dir', str(self.directory)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            child.stdin.write(json.dumps(dict(settings=SETTINGS, secret=SECRET)) + '\n')
            child.stdin.flush()
            deadline = time.monotonic() + 5
            while not (self.directory/'relay.sqlite').exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue((self.directory/'relay.sqlite').exists())
            child.stdin.close()  # Simulate GUI process crash or death.
            child.wait(timeout=5)
            self.assertEqual(child.stdout.read(), '')
            self.assertEqual(child.stderr.read(), '')
            with relay_lock(self.directory):
                pass
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()
            child.stderr.close()


class StoreContext:
    def __init__(self, directory):
        self.directory = directory
    def __enter__(self):
        self.store = Store(self.directory)
        return self.store
    def __exit__(self, *args):
        self.store.close()
