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
    setup.add_argument("--scope", choices=["scoped", "accessible"], default="scoped")
    setup.add_argument("--owner", help="Your Discord user ID")
    setup.add_argument("--owner-handle", help="Optional Discord handle for display only; never used for authorization")
    setup.add_argument("--guilds", help="Comma-separated server IDs")
    setup.add_argument("--channels", help="Comma-separated channel or thread IDs")
    setup.add_argument("--token-file", type=Path, help="Read token from a private file instead of prompting")
    setup.add_argument("--listen", choices=["channels", "mentions"], default="channels")
    setup.add_argument("--audience", choices=["channel", "owner"], default="channel",
                       help="Whose messages to receive inside allowed channels; default: channel")
    configure = commands.add_parser("configure", help="Change listening scope. Stop the server first.")
    configure.add_argument("--scope", choices=["scoped", "accessible"])
    configure.add_argument("--owner")
    configure.add_argument("--owner-handle")
    configure.add_argument("--guilds")
    configure.add_argument("--channels")
    configure.add_argument("--listen", choices=["channels", "mentions"])
    configure.add_argument("--audience", choices=["channel", "owner"])
    credential = commands.add_parser("token", help="Replace the bot token using a hidden prompt or private file.")
    credential.add_argument("--token-file", type=Path)
    runner = commands.add_parser("run", help="Run the Discord connection in the foreground.")
    serve = commands.add_parser("serve", help="Run Discord and the MCP Events endpoint together.")
    for command in (runner, serve):
        command.add_argument("--proxy-from-env", action="store_true",
                             help="Use HTTPS_PROXY or https_proxy for Discord REST and Gateway connections.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    mode = serve.add_mutually_exclusive_group()
    mode.add_argument("--dev", action="store_true", help="Loopback-only bearer auth for local tests.")
    mode.add_argument("--local", action="store_true", help="Loopback-only MCP for a trusted local client or secure tunnel. Local processes can access it.")
    watch = commands.add_parser("watch", help="Wait for pending work in an active local assistant session.")
    watch.add_argument("--timeout", type=int, choices=range(1, 61), default=50, metavar="1..60")
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
    followup = commands.add_parser("followup", help="Queue an intentional follow-up after a confirmed reply.")
    followup.add_argument("id")
    followup.add_argument("--key", required=True, help="Stable operation key; reuse for the same follow-up, never for different text.")
    followup.add_argument("--file", type=Path)
    send = commands.add_parser("send", help="Queue an owner-authorized standalone message to an explicit allowed channel.")
    send.add_argument("channel_id")
    send.add_argument("--key", required=True)
    send.add_argument("--file", type=Path)
    operation = commands.add_parser("operation", help="Inspect an outbound operation.")
    operation.add_argument("operation_id")
    retry_operation = commands.add_parser("retry-operation", help="Retry a failed outbound operation.")
    retry_operation.add_argument("operation_id")
    retry_operation.add_argument("--accept-duplicate-risk", action="store_true")
    retry = commands.add_parser("retry", help="Retry a failed reply.")
    retry.add_argument("id")
    retry.add_argument("--followup-key", help="Retry this follow-up instead of the original reply.")
    retry.add_argument("--accept-duplicate-risk", action="store_true")
    commands.add_parser("gui", help="Open the native setup and relay launcher; never auto-connects.")
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
    if args.scope == "accessible" and (args.guilds or args.channels):
        raise ValueError("Accessible scope uses Discord permissions; omit --guilds and --channels.")
    settings = config.validate({
        "owner_id": args.owner or input("Your Discord user ID (Copy User ID): "),
        "owner_handle": args.owner_handle or "",
        "guild_ids": (args.guilds or input("Server IDs, comma-separated: ")).split(",") if args.scope == "scoped" else [],
        "channel_ids": (args.channels or input("Channel or thread IDs, comma-separated: ")).split(",") if args.scope == "scoped" else [],
        "scope": args.scope,
        "listen": args.listen,
        "audience": args.audience,
    })
    config.save(args.data_dir, settings, read_token(args))
    return {"configured": True, "data_dir": str(args.data_dir.resolve()),
            "listen": settings["listen"], "audience": settings["audience"],
            "next": "Enable Message Content Intent in Discord, then run dotbot run."
                    if settings["listen"] == "channels" else "Run dotbot run."}


def main():
    args = parser().parse_args()
    args.data_dir = args.data_dir.expanduser().resolve()
    store = None
    try:
        if args.command == "gui":
            from .gui import main as gui_main
            sys.exit(gui_main(args.data_dir))
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
                    for key in ("listen", "audience", "scope"):
                        if getattr(args, key) is not None:
                            settings[key] = getattr(args, key)
                    if settings.get("scope") == "accessible" and (args.guilds or args.channels):
                        raise ValueError("Accessible scope uses Discord permissions; omit --guilds and --channels.")
                    for option, key in (("owner", "owner_id"), ("owner_handle", "owner_handle"), ("guilds", "guild_ids"), ("channels", "channel_ids")):
                        value = getattr(args, option)
                        if value is not None:
                            settings[key] = value if option in ("owner", "owner_handle") else value.split(",")
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
                run(settings, store, args.data_dir, config.token(args.data_dir), args if args.command == "serve" else None,
                    proxy_from_env=args.proxy_from_env)
                return
            if hasattr(args, "id") and not store.visible(settings, store.get(args.id)):
                raise ValueError("Request is no longer accessible.")
            if args.command == "watch":
                from .watcher import wait
                result = wait(store, settings, args.data_dir, args.timeout)
            elif args.command == "inbox":
                result = store.visible_list(settings, args.status, args.limit)
            elif args.command == "show":
                result = store.present(settings, store.get(args.id))
            elif args.command == "context":
                result = [store.present(settings, m) for m in store.context(args.id) if store.visible(settings, m)]
            elif args.command == "skip":
                result = store.present(settings, store.skip(args.id))
            elif args.command == "reply":
                text = args.file.read_text() if args.file else sys.stdin.read()
                row = store.reply(args.id, text)
                result = {"id": args.id, "status": row["status"]}
            elif args.command == "send":
                text = args.file.read_text() if args.file else sys.stdin.read()
                result = store.send(settings, args.channel_id, args.key, text)
            elif args.command == "operation":
                result = store.operation(settings, args.operation_id)
            elif args.command == "retry-operation":
                store.retry_operation(settings, args.operation_id, args.accept_duplicate_risk)
                result = store.operation(settings, args.operation_id)
            elif args.command == "followup":
                text = args.file.read_text() if args.file else sys.stdin.read()
                result = store.followup(args.id, args.key, text)
            elif args.command == "retry":
                if args.followup_key:
                    store.retry_followup(args.id, args.followup_key, args.accept_duplicate_risk)
                else:
                    store.retry(args.id, args.accept_duplicate_risk)
                result = {"id": args.id, "status": "queued"}
            else:
                result = store.health()
        print(json.dumps(result, indent=2))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        if args.command in ("run", "serve"):
            # Startup/network exceptions can embed proxy credentials, even in
            # OSError, ValueError, or FileNotFoundError.filename. Never echo them.
            error = "Relay failed. Check configuration, token, permissions, proxy, and network."
        elif isinstance(exc, FileNotFoundError):
            missing = Path(exc.filename).name if exc.filename else "configuration"
            error = f"Missing {missing} in {args.data_dir}. Run dotbot setup first; use serve --local for local MCP or configure mcp.json for OAuth."
        elif isinstance(exc, (ValueError, KeyError, OSError, EOFError)):
            error = str(exc)
        else:
            error = f"{type(exc).__name__}: relay failed. Check token, permissions, and network."
        print(json.dumps({"error": error}), file=sys.stderr)
        sys.exit(1)
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
