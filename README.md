# Discord DotBot

A self-hosted Discord bot for talking to your OpenAI Dot from the Discord servers you choose.

Each user runs their own relay on an always-on machine and invites their Discord bot to their servers. The relay stores incoming messages and sends Dot's replies back to the originating conversation. One active Dot watcher waits on the local queue; MCP Events are an optional separate connection path.

## How it works

```text
Discord ↔ relay + SQLite ↔ one active Dot watcher
```

Choose the servers and channels the bot may read. New setups receive messages from people in those channels without requiring @mentions. Dot can read recent stored context, reply, or skip a message. Bot and webhook messages are ignored. In scoped mode, threads must be enabled by their own channel ID. Accessible mode follows current Discord permissions.

In the optional MCP Events mode, new messages notify the callback worker immediately; this does not guarantee immediate or durable Dot wake-up. Dot reads the message and context to decide whether it is directed at it. For those messages, Dot calls `begin_reply` before doing further work, showing the bot's typing indicator for up to two minutes. Other messages are skipped silently. Typing stops refreshing after a reply, skip, or delivery failure; Discord lets the indicator expire naturally after refreshes stop.

The computer must stay awake and connected. State survives relay restarts. Messages sent while the bot is offline are not backfilled.

## Discord setup

Complete these steps yourself in Discord. Dot can then install the relay on an always-on host it has access to.

### 1. Create your bot

Open the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**. Choose any name you like, such as **Dot**, **DotBot**, **Dotty**, or your Dot's own name. Accept the terms and click **Create**.

### 2. Save the bot token

Open **Bot** in the left sidebar. Under **Token**, click **Reset Token** and complete any verification. Copy the token.

Save it somewhere safe for use later on - this is a secret key, so be careful with it! Discord will not show it again after you leave. Keep it out of chat messages and GitHub.

### 3. Enable conversation access

On the **Bot** page, enable **Message Content Intent** under **Privileged Gateway Intents** and save. This lets the relay read ordinary messages in the channels you enable, even without a mention. Large or verified apps may need Discord approval for this intent.

This toggle is separate from the **Bot Permissions** checklist farther down the page. If you are looking at permission checkboxes, scroll up to **Privileged Gateway Intents**. After enabling the intent, restart the relay if it previously failed to connect.

For a mentions-only relay, leave it off and use `dotbot setup --listen mentions`.

### 4. Configure installation

Open **Installation** in the sidebar:

1. Under **Installation Contexts**, select **Guild Install** only.
2. Set **Install Link** to **Discord Provided Link**.
3. Under **Default Install Settings → Guild Install**, add the scopes `bot` and `applications.commands`.
4. Add the following permissions, listed alphabetically:

   - Read Message History
   - Send Messages
   - Send Messages in Threads
   - View Channels

5. Save changes.

### 5. Add the bot to your server

Copy the **Install Link** from the Installation page and open it. Select your server, continue, and authorize the bot. You need **Manage Server** permission for the selected server.

Confirm the bot appears in the server's member list. It will remain offline until the relay is running.

## Install and run

Install Python 3.9+ and Git. Use a maintained Python version for a new installation.

```sh
git clone https://github.com/Blueteak/Discord-DotBot.git
cd Discord-DotBot
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
dotbot setup
dotbot run
```

On Windows, use `py -m venv .venv` and `.venv\Scripts\Activate.ps1` instead of the two environment commands. The Windows path is implemented but has not been tested on Windows. On macOS/Linux, `./setup.sh` also installs the app.

Setup asks for your Discord user ID, server IDs, channel IDs, and bot token. The token prompt is hidden. It stores configuration, the token, and SQLite data in `~/.discord-dotbot/`, outside the repo. The app does not print the token or accept it as a command-line value. On macOS/Linux, the directory and secret files are restricted to your user.

To copy IDs, enable **User Settings → Advanced → Developer Mode** in Discord, then right-click the item and choose **Copy ID**. Start with a private test channel. Use `--audience owner` during setup to listen only to your messages.

`serve --local` runs Discord and MCP at `http://127.0.0.1:8765/mcp`. It trusts local clients, including a locally running secure tunnel client. It cannot bind to a public interface. Do not expose this unauthenticated mode through a public reverse proxy. Dot connectivity is a separate step; see [connecting Dot](docs/hosting.md).

Use `dotbot run` for Discord and local CLI access without an HTTP endpoint. Stop either command with Ctrl+C. The relay reconnects after network interruptions and prevents a second process from using the same data directory.

## Connect your active Dot

On a host where Dot can run local commands, use [the single-watcher guide](docs/watcher.md). After setup, keep `dotbot run` running and have one active Dot session call `dotbot watch`, handle the result, and repeat. No tunnel, public endpoint, OpenAI API key, or separate response model is needed for this local path.

A local process cannot by itself wake a dormant Dot. If your Dot cannot access this host or remain in an active waiting session, this path is unavailable; use the separately configured MCP path and its platform-dependent lifecycle.

## All accessible guild channels (opt in)

For a new installation, use `dotbot setup --scope accessible`, then `dotbot run` and the [active watcher](docs/watcher.md). This mode asks for your Discord handle (optional, display only), your stable Discord user ID, and the bot token privately; it does not ask for server/channel IDs. Handles cannot be safely resolved offline: enable Discord Developer Mode and use Copy User ID, rather than guessing a handle-to-ID mapping. Agents may append `--token-file /private/path/bot-token`. Message Content Intent must be enabled. For noninteractive setup, use `dotbot setup --scope accessible --owner YOUR_ID --owner-handle YOUR_HANDLE --token-file /private/path/bot-token`. Omit the optional handle if unknown. Add `--audience owner` for owner-only listening. Message outputs include `is_owner`, derived strictly from the stored author ID and configured owner ID; copied or changed handles do not affect it. A verified author ID is not permission to disclose private information to the channel.

This explicit scope includes every guild the bot joins and every supported channel it can currently view, read history in, and send to. New guilds, newly accessible channels, and active threads become eligible while the relay is running; no channel enumeration is frozen at startup. Discord permissions are the boundary. Public threads need Send Messages in Threads; private threads also need bot membership or Manage Threads. Archived/locked threads are excluded; the relay does not join or unarchive threads or change permissions. DMs, bots, and webhooks remain excluded. Mentions are not required: Dot should respond when requested, including natural-language address, and skip unrelated chatter.

Existing installations remain scoped to their explicit nonempty server/channel lists. Empty lists never enable global scope. To opt an existing installation in, stop the relay, run `dotbot configure --scope accessible --listen channels --audience channel`, then restart. To return to scoped mode, stop it and use `dotbot configure --scope scoped --owner OWNER_ID --guilds GUILD_ID --channels CHANNEL_ID`. Existing lists are retained when switching modes but are ignored in accessible mode.

Accessible-mode reads and reply/skip commands require a connected relay and a fresh permission snapshot (up to 15 seconds old). The snapshot is refreshed from Gateway state on changes and each relay tick; it is not a live Discord authorization request per read. Unknown, stale, disconnected, or revoked access hides stored work. Pending messages remain stored and can return if access is restored; review stale requests before responding. Context stays in the original guild and channel, and responses target only the stored request's channel. Before sending, permissions are rechecked; denied sends fail, and ambiguous delivery remains uncertain without automatic retry. A permission change can race an in-flight read/send, and already-returned context cannot be retracted. Local database access remains trusted.

MCP Events subscriptions stay pinned to individual channel IDs. Discovery lists currently accessible channels, but existing subscriptions never expand to newly accessible channels or guilds. Subscribe explicitly for each new channel if using MCP Events; the local watcher sees eligible pending work automatically. Revoked subscriptions can still be unsubscribed. No subscription, tunnel, or connected-app permission is changed by opting in.

## Hosts requiring an outbound proxy

If your host already provides an HTTP CONNECT proxy in `HTTPS_PROXY` (or lowercase `https_proxy`), opt in with `dotbot run --proxy-from-env`. The flag also works with `serve`. It passes the proxy through discord.py's public `proxy` and `proxy_auth` options for both Discord REST and Gateway WebSocket connections. Normal launches ignore these variables and keep the direct-connection default.

The variable must contain an `http://` proxy URL; optional URL-encoded Basic Auth credentials are supported. Supply it through the host's private environment, never chat or command-line arguments. A nonempty uppercase value takes precedence; an empty or missing uppercase value falls back to lowercase. Missing or invalid values fail without printing the value. HTTPS-to-proxy and SOCKS URLs are unsupported; the HTTP proxy must permit CONNECT to Discord HTTPS and WSS destinations. This flag does not configure MCP callback traffic, honor `NO_PROXY`, or change system networking, certificates, or security settings.

For a cloud/shared workspace, the relay and watcher must run in that same accessible environment. Keep credentials and the SQLite data directory outside the checkout, and use the same `--data-dir` for every command. Shared workspace availability does not guarantee persistent processes or storage: on session shutdown, automatic replies stop; queued state survives only if its storage persists. Confirm no other host is running the same bot before starting. A cloud trial is not an always-on deployment.

## Agent setup and token replacement

Agents can configure the app without an interactive prompt:

```sh
dotbot setup --owner OWNER_ID --guilds SERVER_ID --channels CHANNEL_ID --token-file /private/path/bot-token
```

Use a private token file outside the checkout or supply `DISCORD_BOT_TOKEN` through the host's secret environment. Never put token values in chat, command arguments, scripts, or Git. `DISCORD_BOT_TOKEN` overrides the saved token when running.

To replace a token, stop the relay and run `dotbot token` for a hidden prompt, or `dotbot token --token-file /private/path/new-token`. Then restart. If an environment override is set, update it too.

Setup refuses to overwrite an existing installation. Listening settings can be changed while the server is stopped:

```sh
dotbot configure --listen channels --audience channel
```

Existing configurations retain their original mentions-only, owner-only behavior. Installations previously using `./data` must pass `dotbot --data-dir /absolute/path/to/data …`, or set `DOTBOT_DATA_DIR`. Use the same data directory for every command. Back it up privately; it contains messages and credentials.

## Inspect and reply

```sh
dotbot status
dotbot inbox
dotbot context MESSAGE_ID
dotbot reply MESSAGE_ID --file reply.txt
dotbot skip MESSAGE_ID
dotbot show MESSAGE_ID
```

`reply` and `skip` are alternatives. Replies are limited to 2,000 Discord characters. Repeating an identical reply does not queue a duplicate. `show` reports delivery; `skip` marks a message handled without posting. Context contains up to 30 stored messages from the same channel, including recorded bot replies.

Attachments, edits, direct messages, history import, and slash commands are not supported. Choosing helpful moments to reply belongs to Dot, not a separate model inside this server.

See [hosting](docs/hosting.md), [implementation notes](docs/development.md), and the earlier [Sites experiment](docs/sites-probe.md).

Community project. Not affiliated with OpenAI or Discord.
