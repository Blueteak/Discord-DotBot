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
    root = argparse.ArgumentParser(description="Discord relay server for your Dot.")
    root.add_argument("--version", action="version", version="%(prog)s 0.2.0")
    root.add_argument("--data-dir", type=Path, default=Path(os.environ.get("DOTBOT_DATA_DIR", str(Path.home() / ".discord-dotbot"))),
                      help="State directory; default: ~/.discord-dotbot or DOTBOT_DATA_DIR.")
    commands = root.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Save local configuration and a secret bot token.")
    setup.add_argument("--owner", help="Your Discord user ID")
    setup.add_argument("--guilds", help="Comma-separated server IDs")
    setup.add_argument("--channels", help="Comma-separated channel or thread IDs")
    setup.add_argument("--token-file", type=Path, help="Read token from a private file instead of prompting")
    setup.add_argument("--listen", choices=["channels", "mentions"], default="channels")
    setup.add_argument("--audience", choices=["channel", "owner"], default="channel",
                       help="Whose messages to receive inside allowed channels; default: channel")
    configure = commands.add_parser("configure", help="Change listening scope. Stop the server first.")
    configure.add_argument("--listen", choices=["channels", "mentions"])
    configure.add_argument("--audience", choices=["channel", "owner"])
    credential = commands.add_parser("token", help="Replace the bot token using a hidden prompt or private file.")
    credential.add_argument("--token-file", type=Path)
    commands.add_parser("run", help="Run the Discord connection in the foreground.")
    serve = commands.add_parser("serve", help="Run Discord and the MCP Events endpoint together.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    mode = serve.add_mutually_exclusive_group()
    mode.add_argument("--dev", action="store_true", help="Loopback-only bearer auth for local tests.")
    mode.add_argument("--local", action="store_true", help="Loopback-only MCP for a trusted local client or secure tunnel. Local processes can access it.")
    inbox = commands.add_parser("inbox", help="Read requests as JSON without consuming them.")
    inbox.add_argument("--status", choices=["pending", "queued", "sending", "sent", "failed", "uncertain", "skipped", "all"], default="pending")
    inbox.add_argument("--limit", type=int, choices=range(1, 501), default=50, metavar="1..500")
    show = commands.add_parser("show", help="Inspect one request and its delivery state.")
    show.add_argument("id")
    context = commands.add_parser("context", help="Read up to 30 stored messages from the same conversation.")
    context.add_argument("id")
    skip = commands.add_parser("skip", help="Mark a message handled without sending a reply.")
    skip.add_argument("id")
    reply = commands.add_parser("reply", help="Queue one reply. Read text from stdin or --file.")
    reply.add_argument("id")
    reply.add_argument("--file", type=Path)
    retry = commands.add_parser("retry", help="Retry a failed reply.")
    retry.add_argument("id")
    retry.add_argument("--accept-duplicate-risk", action="store_true")
    commands.add_parser("status", help="Show relay connection and queue counts.")
    return root


def read_token(args):
    secret = args.token_file.expanduser().read_text().strip() if args.token_file else os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not secret:
        if not sys.stdin.isatty():
            raise ValueError("Use --token-file or DISCORD_BOT_TOKEN for noninteractive setup.")
        secret = getpass.getpass("Bot token (hidden): ").strip()
    if not secret or any(c.isspace() for c in secret):
        raise ValueError("Bot token cannot be empty or contain whitespace.")
    return secret


def setup(args):
    if (args.data_dir / "config.json").exists() or (args.data_dir / "token").exists():
        raise ValueError("Setup already exists. Edit config.json or the token file directly; stop the relay first.")
    settings = config.validate({
        "owner_id": args.owner or input("Your Discord user ID: "),
        "guild_ids": (args.guilds or input("Server IDs, comma-separated: ")).split(","),
        "channel_ids": (args.channels or input("Channel or thread IDs, comma-separated: ")).split(","),
        "listen": args.listen,
        "audience": args.audience,
    })
    config.save(args.data_dir, settings, read_token(args))
    return {"configured": True, "data_dir": str(args.data_dir.resolve()),
            "listen": settings["listen"], "audience": settings["audience"],
            "next": "Enable Message Content Intent in Discord, then run dotbot serve --local."
                    if settings["listen"] == "channels" else "Run dotbot serve --local."}


def main():
    args = parser().parse_args()
    args.data_dir = args.data_dir.expanduser().resolve()
    store = None
    try:
        if args.command == "setup":
            result = setup(args)
        else:
            settings = config.load(args.data_dir)
            if args.command in ("configure", "token"):
                from .locking import relay_lock
                with relay_lock(args.data_dir):
                    if args.command == "token":
                        config.private_write(args.data_dir / "token", read_token(args) + "\n")
                        print(json.dumps({"token_saved": True, "data_dir": str(args.data_dir)}))
                        return
                    for key in ("listen", "audience"):
                        if getattr(args, key) is not None:
                            settings[key] = getattr(args, key)
                    path = args.data_dir / "config.json"
                    config.private_write(path, json.dumps(config.validate(settings), indent=2) + "\n")
                print(json.dumps(settings, indent=2))
                return
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
            elif args.command == "context":
                result = store.context(args.id)
            elif args.command == "skip":
                result = store.skip(args.id)
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
    except KeyboardInterrupt:
        pass
    except FileNotFoundError as exc:
        missing = Path(exc.filename).name if exc.filename else "configuration"
        print(json.dumps({"error": f"Missing {missing} in {args.data_dir}. Run dotbot setup first; use serve --local for local MCP or configure mcp.json for OAuth."}), file=sys.stderr)
        sys.exit(1)
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
