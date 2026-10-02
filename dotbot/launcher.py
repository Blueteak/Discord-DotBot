"""Native launcher lifecycle, kept independent of Tk and secret display."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from . import config

DEFAULTS = dict(owner_id='', owner_handle='', scope='accessible', guild_ids=[], channel_ids=[],
                listen='channels', audience='channel')
FAILURE = 'Could not connect. Check the token, Discord permissions, Message Content Intent, proxy and network.'


class LauncherError(ValueError):
    """Only fixed, safe messages may be shown by the native window."""


def data_directory(path):
    directory = Path(path).expanduser().resolve()
    if any((parent / '.git').exists() for parent in (directory, *directory.parents)):
        raise LauncherError('Choose a private data folder outside any Git checkout with --data-dir.')
    return directory


def validate_input(settings, secret, saved_token=False):
    try:
        settings = config.validate(settings)
    except (ValueError, KeyError, TypeError):
        raise LauncherError('Check the owner ID, channel/server IDs and message preferences. IDs must be 15–20 digits.') from None
    if not isinstance(secret, str) or len(secret) > 512 or any(c.isspace() for c in secret):
        raise LauncherError('The token must be one nonempty value without spaces or newlines.')
    if not secret and not saved_token:
        raise LauncherError('Paste your Discord bot token to continue.')
    return settings


class Launcher:
    def __init__(self, directory, spawn=subprocess.Popen, clock=time.monotonic):
        self.directory = data_directory(directory)
        self.spawn, self.clock = spawn, clock
        self.process = None
        self.stopping = False
        self.stop_at = None
        self.started_at = 0
        self.status = 'Not connected'
        self.settings = dict(DEFAULTS)
        self.load_error = False
        path = self.directory / 'config.json'
        if path.exists():
            try:
                self.settings = config.load(self.directory)
            except Exception:
                self.load_error = True
                self.status = 'Saved settings could not be loaded. Repair them before connecting.'
        # Only test for presence. The GUI never reads an existing token.
        self.saved_token = (self.directory / 'token').is_file()

    @property
    def busy(self):
        return self.process is not None

    def connect(self, settings, secret='', proxy=False):
        if self.busy:
            return False
        if self.load_error:
            raise LauncherError('Saved settings could not be loaded. Repair config.json before connecting.')
        settings = validate_input(settings, secret, self.saved_token)
        env = dict(os.environ)
        env.pop('DISCORD_BOT_TOKEN', None)  # Never silently override the user's saved/entered token.
        command = [sys.executable, '-m', 'dotbot.gui_worker', '--data-dir', str(self.directory)]
        payload = json.dumps(dict(settings=settings, secret=secret, proxy=bool(proxy))) + '\n'
        if len(payload) > 8192:
            raise LauncherError('Too many channel IDs for the launcher. Use fewer IDs or the CLI.')
        self.started_at = time.time()
        try:
            self.process = self.spawn(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, text=True, env=env,
                                      cwd=str(Path(__file__).resolve().parent.parent))
            # A private pipe, never argv, environment, console output or a log.
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except Exception:
            error = 'Could not start the relay. Check Python installation and the private data folder.'
            self.status = error
            if self.process is not None:
                self.stop()
            raise LauncherError(error) from None
        self.settings = settings
        self.status = 'Connecting…'
        return True

    def poll(self):
        if self.process is None:
            return self.status
        code = self.process.poll()
        if code is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
            self.process = None
            self.saved_token = (self.directory / 'token').is_file()
            self.status = 'Stopped' if self.stopping else (
                'Another relay is already running. Stop it before connecting here.' if code == 20 else FAILURE)
            self.stopping = False
            self.stop_at = None
        elif self.stopping:
            if self.clock() - self.stop_at >= 5:
                self.process.kill()
            self.status = 'Stopping…'
        else:
            connected = False
            try:
                uri = (self.directory / 'relay.sqlite').as_uri() + '?mode=ro'
                with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as db:
                    row = db.execute('SELECT connected, updated_at FROM health WHERE singleton=1').fetchone()
                connected = bool(row and row[0] and row[1] >= self.started_at and time.time() - row[1] < 15)
            except sqlite3.Error:
                pass
            self.status = 'Discord relay connected' if connected else 'Connecting / reconnecting…'
        return self.status

    def stop(self):
        if self.process is not None and not self.stopping:
            self.stopping = True
            self.stop_at = self.clock()
            self.status = 'Stopping…'
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
