# Discord DotBot

A self-hosted Discord bot for talking to your OpenAI Dot from the Discord servers you choose.

Each user runs their own relay on an always-on machine and invites their Discord bot to their servers. The relay stores incoming messages, notifies Dot through MCP Events, and sends Dot's replies back to the originating conversation.

## Status

A Python CLI server with a persistent inbox, Discord replies, and MCP tools/events for Dot. Runs directly on a local computer or server. No Docker or Sites dependency.

The relay receives conversation text; Dot decides when to contribute. A live Discord → Dot → Discord exchange and the Secure MCP Tunnel connection still need verification.

## How it works

```text
Discord ↔ CLI server + SQLite ↔ MCP tools/events ↔ Dot
```

Choose the servers and channels the bot may read. New setups receive messages from people in those channels without requiring @mentions. Dot can read recent stored context, reply, or skip a message. Bot and webhook messages are ignored. Threads must be enabled by their own channel ID.

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
dotbot serve --local
```

On Windows, use `py -m venv .venv` and `.venv\Scripts\Activate.ps1` instead of the two environment commands. The Windows path is implemented but has not been tested on Windows. On macOS/Linux, `./setup.sh` also installs the app.

Setup asks for your Discord user ID, server IDs, channel IDs, and bot token. The token prompt is hidden. It stores configuration, the token, and SQLite data in `~/.discord-dotbot/`, outside the repo. The app does not print the token or accept it as a command-line value. On macOS/Linux, the directory and secret files are restricted to your user.

To copy IDs, enable **User Settings → Advanced → Developer Mode** in Discord, then right-click the item and choose **Copy ID**. Start with a private test channel. Use `--audience owner` during setup to listen only to your messages.

`serve --local` runs Discord and MCP at `http://127.0.0.1:8765/mcp`. It trusts local clients, including a locally running secure tunnel client. It cannot bind to a public interface. Do not expose this unauthenticated mode through a public reverse proxy. Dot connectivity is a separate step; see [connecting Dot](docs/hosting.md).

Use `dotbot run` for Discord and local CLI access without an HTTP endpoint. Stop either command with Ctrl+C. The relay reconnects after network interruptions and prevents a second process from using the same data directory.

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
