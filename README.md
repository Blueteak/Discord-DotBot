# Discord DotBot

A self-hosted Discord bot for talking to your OpenAI Dot from the Discord servers you choose.

Each user asks their Dot to install and run this project in its environment, then invites their Discord bot to their servers. A local relay receives Discord messages, makes them available to the Dot, and sends the Dot's replies back to the originating conversation.

## Status

Project setup. No working bot or deployment package yet.

The starting assumption is that Dot can install this GitHub project and run a local process in its environment. The relay will expose a local inbox and reply interface for Dot to use. Process lifetime and how Dot is notified of incoming messages remain to be verified during the first working exchange.

## Intended flow

```text
Discord user <-> Discord bot <-> Relay in Dot's environment <-> User's Dot
```

- One deployment connects one owner's Discord bot and Dot.
- The owner chooses the servers, channels, and people allowed to use it.
- Requests and replies stay associated with their original conversation.
- Credentials and relay state remain in the owner's deployment.

## First milestone

Prove one complete exchange: the owner sends a message in one permitted Discord channel, their existing Dot reads it through the local relay, and the bot posts the Dot's reply back to that conversation.

See [the development plan](docs/development.md) for the next steps and unresolved integration details.

## Discord setup

Complete these steps yourself in Discord. Dot will handle the local relay installation once it is available.

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

Not available yet. The planned automated setup will install the relay in Dot's environment, securely configure the bot token, restrict access to the owner and selected channels, and start the process. A test exchange will confirm that Dot can read requests and send replies.

Community project. Not affiliated with OpenAI or Discord.
