import asyncio
import contextlib
import logging
import re
import signal
import time

import discord
from discord.state import ConnectionState

from .config import allowed
from .locking import relay_lock

log = logging.getLogger("dotbot")


def delivery_error(exc, request, token=None, secrets=()):
    # Only exception text, never request/response headers or bodies. Redact before
    # truncation so a long credential cannot leave a prefix in the saved error.
    text = str(exc.text if isinstance(exc, discord.HTTPException) else exc)
    for value in (token, request.get("reply"), request.get("content"), *secrets):
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


class RelayState(ConnectionState):
    """Normalize self membership in the pinned discord.py gateway cache.

    discord.py 2.6.4 stores sync membership separately from Thread.me and
    leaves self membership cached after removal. Apply authoritative payloads
    synchronously, before dispatched handlers or delivery can inspect the cache.
    """

    def parse_thread_list_sync(self, data):
        super().parse_thread_list_sync(data)
        guild = self._get_guild(int(data["guild_id"]))
        if guild:
            for item in data.get("threads", []):
                thread = guild.get_thread(int(item["id"]))
                if thread:
                    thread.me = thread._members.get(self.self_id) or thread.me

    def parse_thread_members_update(self, data):
        super().parse_thread_members_update(data)
        if self.self_id in {int(mid) for mid in data.get("removed_member_ids", [])}:
            guild = self._get_guild(int(data["guild_id"]))
            thread = guild and guild.get_thread(int(data["id"]))
            if thread:
                thread.me = None
                thread._pop_member(self.self_id)


class Relay(discord.Client):
    def _get_state(self, **options):
        return RelayState(dispatch=self.dispatch, handlers=self._handlers, hooks=self._hooks, http=self.http, **options)

    def __init__(self, config, store, proxy_from_env=False):
        from .proxy import from_environment
        proxy_options = from_environment() if proxy_from_env else {}
        auth = proxy_options.get("proxy_auth")
        self.proxy_secrets = (auth.encode(), auth.login, auth.password) if auth else ()
        super().__init__(intents=discord.Intents(guilds=True, guild_messages=True,
                         message_content=config.get("listen", "mentions") == "channels"),
                         allowed_mentions=discord.AllowedMentions.none(), max_messages=None, **proxy_options)
        self.gateway_connected = False
        self.config = config
        self.store = store
        self.worker = None
        self.on_received = None
        self.typing_tasks = {}
        self.typing_requests = {}

    def dispatch(self, event, /, *args, **kwargs):
        # is_ready remains set during resumable reconnects. Invalidate before
        # scheduling asynchronous handlers so no intervening tick can restore access.
        if event == "disconnect":
            self.gateway_connected = False
            self.store.heartbeat(False)
            self.store.set_access([])
        elif event in ("ready", "resumed"):
            self.gateway_connected = True
        super().dispatch(event, *args, **kwargs)

    def channel_accessible(self, channel):
        if not self.gateway_connected:
            return False
        guild = getattr(channel, "guild", None)
        member = getattr(guild, "me", None)
        if not guild or not member or getattr(guild, "unavailable", False):
            return False
        try:
            permissions = channel.permissions_for(member)
            thread = isinstance(channel, discord.Thread)
            if thread and (channel.archived or channel.locked
                           or (channel.is_private() and channel.me is None and not permissions.manage_threads)):
                return False
            return bool(permissions.view_channel and permissions.read_message_history
                        and (permissions.send_messages_in_threads if thread else permissions.send_messages)
                        and isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread)))
        except (AttributeError, discord.ClientException):
            return False

    def refresh_access(self):
        self.store.set_access((guild.id, channel.id) for guild in self.guilds
                              for channel in [*guild.channels, *guild.threads]
                              if self.channel_accessible(channel))

    async def on_guild_channel_update(self, before, after):
        self.refresh_access()

    async def on_guild_channel_create(self, channel):
        self.refresh_access()

    async def on_guild_channel_delete(self, channel):
        self.refresh_access()

    async def on_thread_join(self, thread):
        self.refresh_access()

    async def on_thread_update(self, before, after):
        self.refresh_access()

    async def on_raw_thread_delete(self, payload):
        self.store.revoke_access(payload.thread_id)

    async def on_guild_role_update(self, before, after):
        self.refresh_access()

    async def on_guild_role_delete(self, role):
        self.refresh_access()

    async def on_member_update(self, before, after):
        if self.user and after.id == self.user.id:
            self.refresh_access()

    async def on_guild_unavailable(self, guild):
        self.refresh_access()

    async def on_resumed(self):
        self.gateway_connected = True
        self.refresh_access()
        self.store.heartbeat(True)

    async def on_thread_remove(self, thread):
        self.store.revoke_access(thread.id)

    async def on_guild_remove(self, guild):
        self.refresh_access()

    async def on_guild_join(self, guild):
        self.refresh_access()

    async def setup_hook(self):
        self.worker = asyncio.create_task(self.deliver())
        def worker_done(task):
            if not task.cancelled() and task.exception():
                log.error("Reply worker stopped; exiting relay.")
                asyncio.create_task(self.close())
        self.worker.add_done_callback(worker_done)

    async def on_ready(self):
        self.gateway_connected = True
        self.refresh_access()
        self.store.heartbeat(True)
        log.info("Connected as %s", self.user)

    async def on_disconnect(self):
        self.gateway_connected = False
        self.store.heartbeat(False)
        self.store.set_access([])

    async def on_message(self, message):
        if not allowed(self.config, message.author.id, message.guild.id if message.guild else None,
                       message.channel.id, message.author.bot, bool(message.webhook_id)):
            return
        if self.config.get("scope") == "accessible":
            if not self.channel_accessible(message.channel):
                self.store.revoke_access(message.channel.id)
                return
            with self.store.db:
                self.store.db.execute("INSERT OR REPLACE INTO channel_access VALUES (?, ?, ?)",
                                      (str(message.channel.id), str(message.guild.id), time.time()))
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
            if self.on_received:
                self.on_received()

    def start_typing(self, request):
        # Only Dot's explicit intent to reply starts the visible indicator.
        channel_id = request["channel_id"]
        requests = self.typing_requests.setdefault(channel_id, {})
        requests.setdefault(request["id"], time.monotonic() + 120)
        if channel_id not in self.typing_tasks:
            self.typing_tasks[channel_id] = asyncio.create_task(self.keep_typing(channel_id))

    async def keep_typing(self, channel_id):
        request = None
        try:
            channel = None
            next_pulse = 0
            while True:
                requests = self.typing_requests[channel_id]
                now = time.monotonic()
                for mid, deadline in list(requests.items()):
                    request = self.store.get(mid)
                    if (now >= deadline or request["status"] not in ("pending", "queued", "sending")
                            or not self.store.visible(self.config, request)):
                        requests.pop(mid)
                if not requests:
                    return
                if now >= next_pulse:
                    channel = channel or self.get_channel(int(channel_id)) or await self.fetch_channel(int(channel_id))
                    if str(getattr(getattr(channel, "guild", None), "id", None)) != request["guild_id"]:
                        return
                    await asyncio.wait_for(channel.typing(), timeout=10)
                    log.info("Typing indicator sent in channel %s", channel_id)
                    next_pulse = time.monotonic() + 7
                await asyncio.sleep(1)
        except Exception as exc:
            log.warning("Typing indicator failed in channel %s: %s", channel_id,
                        delivery_error(exc, request or {}, self.http.token, self.proxy_secrets))
        finally:
            self.typing_requests.pop(channel_id, None)
            self.typing_tasks.pop(channel_id, None)

    async def send_request(self, request):
        def finish(status, **kwargs):
            self.store.finish(request["id"], status, operation_id=request.get("operation_id"), **kwargs)

        if request.get("id") is None:
            try:
                destination = self.store.destination(self.config, request["channel_id"])
                if destination["guild_id"] != request["guild_id"]:
                    raise ValueError("Destination server changed.")
            except ValueError:
                finish("failed", error="Destination no longer permitted or permission snapshot stale.")
                return
        if not self.store.visible(self.config, request):
            finish("failed", error="Destination no longer permitted by configuration.")
            return
        try:
            channel = self.get_channel(int(request["channel_id"])) or await self.fetch_channel(int(request["channel_id"]))
            if str(getattr(getattr(channel, "guild", None), "id", None)) != request["guild_id"]:
                finish("failed", error="Channel does not belong to the stored server.")
                return
            if (request.get("operation_id") or self.config.get("scope") == "accessible") and not self.channel_accessible(channel):
                self.store.revoke_access(channel.id)
                finish("failed", error="Channel access is no longer available.")
                return
            reference = discord.MessageReference(message_id=int(request["id"]),
                channel_id=int(request["channel_id"]), guild_id=int(request["guild_id"]), fail_if_not_exists=True) if request["id"] else None
            sent = await channel.send(request["reply"], reference=reference, nonce=request.get("operation_id", request["id"])[:25],
                                      allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            # A network failure can happen after Discord accepted the message.
            # Do not blindly resend on the next tick or restart.
            status = "failed" if isinstance(exc, (discord.Forbidden, discord.NotFound)) else "uncertain"
            if isinstance(exc, (discord.Forbidden, discord.NotFound)):
                self.store.revoke_access(request["channel_id"])
            details = delivery_error(exc, request, self.http.token, self.proxy_secrets)
            error = details if status == "failed" else f"Delivery not confirmed. {details}. Check Discord before retrying."
            finish( status, error=error)
            log.warning("Reply %s for request %s in channel %s: %s", status,
                        request["id"], request["channel_id"], details)
        else:
            finish("sent", reply_id=str(sent.id))
            log.info("Delivered reply for %s", request["id"])

    async def deliver(self):
        while not self.is_closed():
            if self.gateway_connected:
                self.refresh_access()
            self.store.heartbeat(self.gateway_connected)
            if self.gateway_connected:
                request = self.store.claim()
                if request:
                    await self.send_request(request)
            await asyncio.sleep(1)

    async def close(self):
        self.gateway_connected = False
        tasks = list(self.typing_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.worker:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.worker
        self.store.heartbeat(False)
        self.store.set_access([])
        await super().close()


def run(config, store, directory, token, serve=None, proxy_from_env=False, *, _lock_held=False):
    # Native launcher holds this same lock across configuration and startup.
    with contextlib.nullcontext() if _lock_held else relay_lock(directory):
        store.heartbeat(False)
        store.set_access([])
        store.recover()
        client = Relay(config, store, proxy_from_env=proxy_from_env)
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
                client.on_received = events.wake
                runner = web.AppRunner(make_app(config, store, events, auth, begin_reply=client.start_typing), access_log=None)
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
