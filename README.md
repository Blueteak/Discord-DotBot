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

## Planned setup

1. Create a Discord application and bot, and provide its token to the local deployment.
2. Ask Dot to install this repository and start the relay in its environment.
3. Configure the owner's Discord identity and permitted servers and channels.
4. Invite the bot and send a message for Dot to answer.

These are the intended setup steps. Runnable installation instructions will follow the first working milestone.

Community project. Not affiliated with OpenAI or Discord.
