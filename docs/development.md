# Development plan

## Run alongside Dot

The initial design assumes Dot can install this GitHub project and run a local process in its environment. That process connects to Discord using the owner's bot token and exposes an inbox and reply interface that Dot can access locally.

Dot reads incoming requests and submits its own replies. A public Dot messaging API is not a prerequisite for this design.

Start with a small local command interface backed by durable message storage. An inbox command returns pending messages with their Discord sender, server, channel, thread, and message IDs. A reply command takes the original request ID and response text. The relay resolves the destination from the stored request, sends the reply, and records delivery.

The remaining integration questions are how Dot learns that a request is waiting, whether the process stays running between Dot tasks, and which persistence and networking facilities its environment provides. Verify these in the first working deployment.

## Build one complete exchange

1. Dot installs and starts the relay with the owner's Discord bot token.
2. The owner sends an explicit request in one permitted Discord channel.
3. The relay stores the request in the local inbox.
4. Dot reads the request and submits a reply referencing its request ID.
5. The relay posts the reply to the originating conversation and records completion.

Keep access owner-only for this first milestone. Ignore bot messages to avoid reply loops. Check access before queueing requests and suppress automatic mentions in outgoing replies.

## Make it self-deployable

After the exchange works, document bot creation, installation by Dot, configuration, invitation, and process startup. Include an example configuration with no real credentials. Keep tokens out of source control and message content out of routine logs.

Then add multiple permitted servers and channels, separate conversation histories, timeouts, and restart recovery. Persist Discord message IDs to detect duplicates, and account for uncertain delivery before retrying replies.

Optional access for other Discord users comes after owner-only operation works. The Dot may have personal context and tools, so requests must preserve the actual Discord sender and conversation audience.

## Completion check

A new user can ask their Dot to install the relay, invite their bot to a chosen server, and exchange messages. Unauthorized users and channels cannot queue requests. Replies return only to the conversation that requested them, including after a process restart.
