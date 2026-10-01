# Relay design

```text
Discord Gateway -> relay host -> SQLite inbox
                               -> signed MCP event -> Dot
Dot -> MCP get_message / reply -> SQLite outbox -> Discord
```

Run the bot and MCP endpoint on an always-on machine with persistent storage. Dot's cloud workspace is disposable. It is not the storage or process host for this design.

## Current implementation

Python, discord.py, aiohttp, and SQLite. One owner and explicit server/channel allowlists. New setups use channel listening for all human participants in those channels. `--audience owner` restricts this to the owner; `--listen mentions` requires a mention and needs no privileged intent. Legacy configuration defaults remain owner/mentions. Channel listening enables Message Content Intent. Thread IDs must be allowed explicitly. Bots, webhooks, and DMs are ignored. Text only; attachments, edits, slash commands, and history import are not implemented.

The local CLI and remote MCP tools share one database. A Discord message ID is the request ID. Reading does not consume it. Each request accepts one reply of up to 2,000 Discord characters. Repeating identical reply text returns the existing state. Different text for the same request is rejected.

Reply states are `pending`, `queued`, `sending`, `sent`, `failed`, and `uncertain`, and `skipped`. A skipped message stays available as context and cannot later be replied to. The relay records `sending` before the Discord call and `sent` after confirmation. On restart, interrupted sends become `uncertain` and are not automatically resent. A stable Discord nonce also protects short-term library retries. This is not a claim of exactly-once delivery across arbitrary outages.

Use `show` to inspect a request. `retry` requeues a definite failure. For an uncertain send, inspect Discord first, then use `retry --accept-duplicate-risk` only if another send is appropriate. MCP does not expose this override.

## MCP Events

`dotbot serve` runs Discord and the authenticated `/mcp` endpoint in one process. It implements MCP version `2026-07-28`, including `server/discover`, `tools/list`, `tools/call`, `events/list`, `events/subscribe`, and `events/unsubscribe`.

The tools are `get_message`, `get_context`, `list_pending`, `reply`, and `skip`. Context returns up to 30 stored messages in the same channel through the anchor message, with reply text and send timestamps. Display names and message text are untrusted content. Dot must decide whether replying is useful; the relay does not run an LLM. The only event is `message.created`, filtered by an allowed `channel_id`. Events carry IDs and a Discord link; Dot retrieves message text through the read tool. Server-side audience and destination checks apply before enqueueing, reading, and sending.

Subscriptions and event delivery records persist in SQLite. Callback verification uses a fresh signed challenge. Each delivery uses Standard Webhooks HMAC signatures, validates public destination addresses at connection time, and blocks redirects. Subscription refresh is idempotent; changed keys have a five-minute dual-signature window.

Transient event failures retry with backoff, up to eight attempts. Permanent errors stop that event; HTTP 410 also expires the subscription. IDs stay stable across retries. Subscriptions last at most one day and no longer than the current OAuth access token. ChatGPT must refresh them with valid authorization. Expired and unsubscribed subscriptions stop delivery.

No protocol history replay is offered: cursors are null. Requests received during an active subscription survive relay restarts. Messages sent while the Discord Gateway is offline are not backfilled. If event delivery is exhausted or a subscription has lapsed, pending messages remain available through the inbox tools.

## Hosting and authentication

See [hosting](hosting.md). The service includes an OAuth resource server, not an OAuth authorization server. Direct public HTTPS access requires an external provider. Private `serve --local` access trusts local processes and is intended for a secure tunnel or a local MCP client. Tunnel compatibility with Dot still needs a live test. The local bearer-token mode is for development only and cannot be used as a ChatGPT plugin login.

On an always-on host, keep the process supervised and back up its persistent data directory. Configuration changes take effect on restart. Keep one process per data directory. Logs contain status and message IDs, not message text or tokens. The SQLite database itself contains private message text and subscription secrets.

## Verification

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The focused tests cover owner/channel restrictions, durable state, duplicate replies, interrupted delivery, OAuth identity checks, MCP discovery, callback verification, private-address rejection, subscription restart, and webhook retries. They use simulated Discord and callback delivery; a live Discord → Dot → Discord exchange still needs hosting and a connected plugin.

Protocol references: [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events), [OpenAI authentication](https://developers.openai.com/plugins/build/auth), [MCP 2.0 transport](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http).
