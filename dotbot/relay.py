import asyncio
import contextlib
import logging
import re
import signal
import time

import discord

from .config import allowed
from .locking import relay_lock

log = logging.getLogger("dotbot")


def delivery_error(exc, request, token=None):
    # Only exception text, never request/response headers or bodies. Redact before
    # truncation so a long credential cannot leave a prefix in the saved error.
    text = str(exc.text if isinstance(exc, discord.HTTPException) else exc)
    for value in (token, request.get("reply"), request.get("content")):
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"https?://[^\s]+", "[redacted URL]", text)
    text = re.sub(r"(?i)\b(?:authorization|token|secret|api[_-]?key)\s*[:=]\s*\S+", "[redacted credential]", text)
    text = re.sub(r"(?i)\b(?:Bot|Bearer)\s+\S+", "[redacted credential]", text)
    text = re.sub(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}|\b(?:sk-|whsec_)[A-Za-z0-9_-]+", "[redacted credential]", text)
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())[:500]
    details = type(exc).__name__
    if isinstance(exc, discord.HTTPException):
        details += f" status={exc.status} code={exc.code}"
    return f"{details}: {text or '(no error text)'}"


class Relay(discord.Client):
    def __init__(self, config, store):
        super().__init__(intents=discord.Intents(guilds=True, guild_messages=True,
                         message_content=config.get("listen", "mentions") == "channels"),
                         allowed_mentions=discord.AllowedMentions.none(), max_messages=None)
        self.config = config
        self.store = store
        self.worker = None

    async def setup_hook(self):
        self.worker = asyncio.create_task(self.deliver())
        def worker_done(task):
            if not task.cancelled() and task.exception():
                log.error("Reply worker stopped; exiting relay.")
                asyncio.create_task(self.close())
        self.worker.add_done_callback(worker_done)

    async def on_ready(self):
        self.store.heartbeat(True)
        log.info("Connected as %s", self.user)

    async def on_disconnect(self):
        self.store.heartbeat(False)

    async def on_message(self, message):
        if not allowed(self.config, message.author.id, message.guild.id if message.guild else None,
                       message.channel.id, message.author.bot, bool(message.webhook_id)):
            return
        if not self.user:
            return
        mentioned = any(user.id == self.user.id for user in message.mentions)
        if self.config.get("listen", "mentions") == "mentions" and not mentioned:
            return
        content = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
        if not content:
            return
        if self.store.receive({"id": str(message.id), "guild_id": str(message.guild.id),
                               "channel_id": str(message.channel.id), "author_id": str(message.author.id),
                               "content": content, "received_at": time.time(),
                               "author_name": getattr(message.author, "display_name", str(message.author.id)),
                               "mentioned": int(mentioned),
                               "reply_to": str(message.reference.message_id) if getattr(message, "reference", None) else None}):
            log.info("Queued request %s", message.id)

    async def send_request(self, request):
        if not allowed(self.config, request["author_id"], request["guild_id"], request["channel_id"]):
            self.store.finish(request["id"], "failed", error="Destination no longer permitted by configuration.")
            return
        try:
            channel = self.get_channel(int(request["channel_id"])) or await self.fetch_channel(int(request["channel_id"]))
            if str(getattr(getattr(channel, "guild", None), "id", None)) != request["guild_id"]:
                self.store.finish(request["id"], "failed", error="Channel does not belong to the stored server.")
                return
            reference = discord.MessageReference(message_id=int(request["id"]),
                channel_id=int(request["channel_id"]), guild_id=int(request["guild_id"]), fail_if_not_exists=True)
            sent = await channel.send(request["reply"], reference=reference, nonce=request["id"],
                                      allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            # A network failure can happen after Discord accepted the message.
            # Do not blindly resend on the next tick or restart.
            status = "failed" if isinstance(exc, (discord.Forbidden, discord.NotFound)) else "uncertain"
            details = delivery_error(exc, request, self.http.token)
            error = details if status == "failed" else f"Delivery not confirmed. {details}. Check Discord before retrying."
            self.store.finish(request["id"], status, error=error)
            log.warning("Reply %s for request %s in channel %s: %s", status,
                        request["id"], request["channel_id"], details)
        else:
            self.store.finish(request["id"], "sent", reply_id=str(sent.id))
            log.info("Delivered reply for %s", request["id"])

    async def deliver(self):
        while not self.is_closed():
            self.store.heartbeat(self.is_ready())
            if self.is_ready():
                request = self.store.claim()
                if request:
                    await self.send_request(request)
            await asyncio.sleep(1)

    async def close(self):
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.worker
        self.store.heartbeat(False)
        await super().close()


def run(config, store, directory, token, serve=None):
    with relay_lock(directory):
        store.recover()
        client = Relay(config, store)
        async def start():
            runner = None
            worker = None
            loop = asyncio.get_running_loop()
            current = asyncio.current_task()
            # asyncio.run handles Ctrl+C; service managers normally send SIGTERM.
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signal.SIGTERM, current.cancel)
            if serve:
                from aiohttp import web
                from .events import Events
                from .mcp import Auth, make_app
                if (serve.dev or serve.local) and serve.host != "127.0.0.1":
                    raise ValueError("Local MCP modes are restricted to 127.0.0.1.")
                auth = Auth(directory, dev=serve.dev, local=serve.local)
                if serve.dev or serve.local:
                    auth.resource = f"http://127.0.0.1:{serve.port}/mcp"
                events = Events(store, config, auth.principal)
                runner = web.AppRunner(make_app(config, store, events, auth), access_log=None)
                await runner.setup()
                await web.TCPSite(runner, serve.host, serve.port).start()
                worker = asyncio.create_task(events.run())
                log.info("MCP endpoint listening on %s:%s/mcp", serve.host, serve.port)
            try:
                async with client:
                    discord_task = asyncio.create_task(client.start(token))
                    tasks = [discord_task] + ([worker] if worker else [])
                    # If either worker dies, stop so the process supervisor can restart it.
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        task.result()
                    if client.worker and client.worker.done() and not client.worker.cancelled():
                        client.worker.result()
            finally:
                if worker:
                    worker.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker
                if runner:
                    await runner.cleanup()
        try:
            asyncio.run(start())
        except asyncio.CancelledError:
            pass
        except discord.PrivilegedIntentsRequired:
            raise ValueError("Enable Message Content Intent in Discord Developer Portal > Bot, or configure --listen mentions.") from None
        except discord.LoginFailure:
            raise ValueError("Discord rejected the bot token. Update the token file or DISCORD_BOT_TOKEN.") from None
