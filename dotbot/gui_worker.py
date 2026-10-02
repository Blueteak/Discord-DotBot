"""Private launcher child. No secrets or raw exceptions are emitted."""
import argparse
import json
import logging
import os
import signal
import sys
import threading

from . import config
from .launcher import data_directory, validate_input
from .locking import relay_lock
from .relay import run
from .store import Store


def launch(directory, payload):
    directory = data_directory(directory)
    settings = validate_input(payload['settings'], payload.get('secret', ''), (directory / 'token').is_file())
    config.private_directory(directory)
    # Own the existing relay lock before saving anything; hold it through run().
    with relay_lock(directory):
        # Protect the entire dedicated runtime folder, including new SQLite
        # sidecars and atomic-write temporary files, before saving any secret.
        config.private_write(directory / '.gitignore', '*\n')
        secret = payload.pop('secret', '')
        if secret:
            config.private_write(directory / 'token', secret + '\n')
        config.private_write(directory / 'config.json', json.dumps(settings, indent=2) + '\n')
        secret = config.token(directory)
        store = Store(directory)
        try:
            run(settings, store, directory, secret, proxy_from_env=bool(payload.get('proxy')), _lock_held=True)
        finally:
            store.heartbeat(False)
            store.set_access([])
            store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    os.environ.pop('DISCORD_BOT_TOKEN', None)
    try:
        line = sys.stdin.readline(8193)
        if len(line) > 8192 or not line.endswith('\n'):
            return 1
        payload = json.loads(line)
        del line
        # The parent's pipe stays open for its lifetime. A killed/crashed GUI
        # cannot leave an unattended child relay running indefinitely.
        def parent_closed():
            # Avoid a daemon holding Python’s buffered-stdin lock at exit.
            while os.read(sys.stdin.fileno(), 4096):
                pass
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=parent_closed, daemon=True).start()
        launch(args.data_dir, payload)
        return 0
    except ValueError as exc:
        return 20 if str(exc).startswith('A relay is already running') else 1
    except BaseException:
        return 1


if __name__ == '__main__':
    sys.exit(main())
