# Sites capability test

Tested October 1, 2026 on the private [Discord DotBot Probe](https://discord-dotbot-probe.blueteak.chatgpt.site). Source is independently versioned by Sites; the local checkout is `experiments/sites-probe/` and is excluded from this GitHub repository.

## Observed

- Published a private Site using the existing OpenAI account; no separate hosting account was created.
- The deployed Worker opened an outbound WebSocket to Discord Gateway and received `HELLO`, with a 41,250 ms heartbeat interval. No bot token was supplied; the socket was deliberately closed after the handshake.
- D1 stored the run and its observations, which were retrieved in a later HTTP request after more than 90 seconds without probe requests.
- A task attached to `ctx.waitUntil()` recorded checkpoints 5 and 20 seconds after the initial response. Its planned 40- and 70-second checkpoints were absent.
- Sites provisioned a private MCP plugin and an HTTPS `/mcp` endpoint. The owner reported completing roughly five approval steps. A service-credential call to `/mcp` returned HTTP 401; that credential worked for the private diagnostic API, but does not establish MCP OAuth access. Authenticated plugin tool use, Dot event subscription, and a real Discord exchange remain untested.

Run: `a36b6174-03d1-4cb8-9f9e-2c4da77d9696`. Published source commit: `4603c5e31ece109afa0301dbb6d48c792039415a` in the Site's source repository.

## What this means

The background observation matches Cloudflare's documented [30-second post-response limit](https://developers.cloudflare.com/workers/runtime-apis/context/#waituntil). This request-scoped approach cannot supervise an always-on bot. The test did not establish whether other Sites capabilities can support a persistent listener, nor did it test an authenticated Discord session or restart recovery.

Sites is a candidate for durable queues, settings, and authenticated MCP tools. A reliable Discord Gateway listener still needs an always-on host or a separately verified persistent execution capability. An open browser connection would not meet the requirement for unattended operation.

## Conversation behavior

The intended experience is participation in selected channels, with no mandatory tag or slash command. The listener forwards permitted conversation context; Dot decides whether it can usefully contribute and may remain silent. An @mention signals a direct request. Ordinary channel text requires Discord's [Message Content Intent](https://docs.discord.com/developers/events/gateway#message-content-intent), enabled in both the portal and relay code.

This is the target behavior. The existing Python tracer bullet still restricts requests to owner messages that mention the bot. The probe does not change those restrictions or read any server messages.

## Setup friction

The owner reported about five approvals when allowing the probe plugin. Do not describe Sites setup as one-click. Reuse the same Site and plugin when updating it, and document the actual connection screens once verified with Dot. No evidence yet establishes whether all those prompts recur.
