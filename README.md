# Discord DotBot

A self-hosted Discord bot for talking to your OpenAI Dot from the Discord servers you choose.

Each user runs their own relay on an always-on machine and invites their Discord bot to their servers. The relay stores incoming messages, notifies Dot through MCP Events, and sends Dot's replies back to the originating conversation.

## Status

Initial relay implemented. Local checks pass; a live Discord → Dot → Discord exchange is not yet verified.

The queue, bot process, and subscriptions live on the relay host. Dot connects through authenticated MCP tools and events. Hosting, OAuth configuration, and plugin connection are still required. Do not use Dot's disposable cloud workspace as the persistent host.

## Intended flow

```text
Discord user <-> Discord bot <-> Persistent relay host <-> MCP tools/events <-> Dot
```

- One deployment connects one owner's Discord bot and Dot.
- The owner chooses the servers, channels, and people allowed to use it.
- Requests and replies stay associated with their original conversation.
- Credentials and relay state remain in the owner's deployment.

## First milestone

Prove one complete exchange: the owner sends a message in one permitted Discord channel, their existing Dot reads it through the local relay, and the bot posts the Dot's reply back to that conversation.

See [the development plan](docs/development.md) for the next steps and unresolved integration details.

## Discord setup

Complete these steps yourself in Discord. Dot can then install the relay on an always-on host it has access to.

### 1. Create your bot

Open the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**. Choose any name you like, such as **Dot**, **DotBot**, **Dotty**, or your Dot's own name. Accept the terms and click **Create**.

### 2. Save the bot token

Open **Bot** in the left sidebar. Under **Token**, click **Reset Token** and complete any verification. Copy the token.

Save it somewhere safe for use later on - this is a secret key, so be careful with it! Discord will not show it again after you leave. Keep it out of chat messages and GitHub.

### 3. Configure installation

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

### 4. Add the bot to your server

Copy the **Install Link** from the Installation page and open it. Select your server, continue, and authorize the bot. You need **Manage Server** permission for the selected server.

Confirm the bot appears in the server's member list. It will remain offline until the relay is running.

## Relay installation

On your relay host, install Python 3.9+ and Git, then run:

```sh
git clone https://github.com/Blueteak/Discord-DotBot.git
cd Discord-DotBot
./setup.sh
.venv/bin/dotbot setup
```

Setup asks for your Discord user ID, server ID, channel ID, and saved bot token. To copy IDs, enable **User Settings → Advanced → Developer Mode** in Discord, then right-click your user, server, and chosen channel and select **Copy ID**. Start with one private test channel. The token prompt is hidden.

Configuration and the queue are stored in `data/`, which Git ignores. Keep this directory on persistent storage. Setup will not overwrite existing credentials. Multiple IDs can be entered separated by commas. Thread IDs must be allowed explicitly.

Start the Discord relay:

```sh
.venv/bin/dotbot run
```

Mention your bot with a text message in the allowed channel. Only your configured user can queue requests. No privileged Discord intents are required. DMs, attachments, and slash commands are not implemented yet.

In another terminal on the same host:

```sh
.venv/bin/dotbot inbox
.venv/bin/dotbot reply MESSAGE_ID --file reply.txt
.venv/bin/dotbot show MESSAGE_ID
.venv/bin/dotbot status
```

Replace `MESSAGE_ID` with an ID from the inbox and put the reply text in `reply.txt`. Replies are limited to 2,000 Discord characters. The reply command queues delivery; `show` confirms whether Discord received it. Repeating the same reply does not queue a duplicate. Remove the temporary reply file when finished.

For automated setup, supply `--owner`, `--guilds`, `--channels`, and `--token-file` to `dotbot setup`. Keep the token in a private file or the `DISCORD_BOT_TOKEN` environment variable, never a command argument. Always use the same working directory, or set `DOTBOT_DATA_DIR` to an absolute path.

To connect Dot and receive push notifications, follow [hosting and MCP setup](docs/hosting.md). The relay includes MCP tools and signed events; a public HTTPS endpoint and external OAuth provider are required before connecting a real Dot.

Community project. Not affiliated with OpenAI or Discord.
