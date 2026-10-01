import argparse
import getpass
import json
import logging
import os
from pathlib import Path
import sys

from . import config
from .store import Store


def parser():
    root = argparse.ArgumentParser(description="Local inbox and replies for Discord DotBot.")
    root.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DOTBOT_DATA_DIR", "data")),
                      help="State directory; default: ./data or DOTBOT_DATA_DIR. Use the same directory for every command.")
    commands = root.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Save local configuration and a secret bot token.")
    setup.add_argument("--owner", help="Your Discord user ID")
    setup.add_argument("--guilds", help="Comma-separated server IDs")
    setup.add_argument("--channels", help="Comma-separated channel or thread IDs")
    setup.add_argument("--token-file", type=Path, help="Read token from a private file instead of prompting")
    commands.add_parser("run", help="Run the Discord connection in the foreground.")
    serve = commands.add_parser("serve", help="Run Discord and the MCP Events endpoint together.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--dev", action="store_true", help="Loopback-only bearer auth for local tests; not a ChatGPT connection.")
    inbox = commands.add_parser("inbox", help="Read requests as JSON without consuming them.")
    inbox.add_argument("--status", choices=["pending", "queued", "sending", "sent", "failed", "uncertain", "all"], default="pending")
    inbox.add_argument("--limit", type=int, choices=range(1, 501), default=50, metavar="1..500")
    show = commands.add_parser("show", help="Inspect one request and its delivery state.")
    show.add_argument("id")
    reply = commands.add_parser("reply", help="Queue one reply. Read text from stdin or --file.")
    reply.add_argument("id")
    reply.add_argument("--file", type=Path)
    retry = commands.add_parser("retry", help="Retry a failed reply.")
    retry.add_argument("id")
    retry.add_argument("--accept-duplicate-risk", action="store_true")
    commands.add_parser("status", help="Show relay connection and queue counts.")
    return root


def setup(args):
    if (args.data_dir / "config.json").exists() or (args.data_dir / "token").exists():
        raise ValueError("Setup already exists. Edit config.json or the token file directly; stop the relay first.")
    settings = config.validate({
        "owner_id": args.owner or input("Your Discord user ID: "),
        "guild_ids": (args.guilds or input("Server IDs, comma-separated: ")).split(","),
        "channel_ids": (args.channels or input("Channel or thread IDs, comma-separated: ")).split(","),
    })
    secret = args.token_file.read_text().strip() if args.token_file else os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not secret:
        if not sys.stdin.isatty():
            raise ValueError("Use --token-file or DISCORD_BOT_TOKEN for noninteractive setup.")
        secret = getpass.getpass("Bot token (hidden): ").strip()
    if not secret or any(c.isspace() for c in secret):
        raise ValueError("Bot token cannot be empty or contain whitespace.")
    config.save(args.data_dir, settings, secret)
    return {"configured": True, "data_dir": str(args.data_dir.resolve())}


def main():
    args = parser().parse_args()
    store = None
    try:
        if args.command == "setup":
            result = setup(args)
        else:
            settings = config.load(args.data_dir)
            store = Store(args.data_dir)
            if args.command in ("run", "serve"):
                from .relay import run
                logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
                # Library logs may include request details. Keep routine logs to our own IDs/status.
                logging.getLogger("discord").setLevel(logging.CRITICAL)
                run(settings, store, args.data_dir, config.token(args.data_dir), args if args.command == "serve" else None)
                return
            if args.command == "inbox":
                result = store.list(args.status, args.limit)
            elif args.command == "show":
                result = store.get(args.id)
            elif args.command == "reply":
                text = args.file.read_text() if args.file else sys.stdin.read()
                row = store.reply(args.id, text)
                result = {"id": args.id, "status": row["status"]}
            elif args.command == "retry":
                store.retry(args.id, args.accept_duplicate_risk)
                result = {"id": args.id, "status": "queued"}
            else:
                result = store.health()
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError, EOFError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        # Do not expose tokens or message bodies via third-party exception strings.
        print(json.dumps({"error": f"{type(exc).__name__}: relay failed. Check token, permissions, and network."}), file=sys.stderr)
        sys.exit(1)
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
