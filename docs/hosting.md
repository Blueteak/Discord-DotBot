# Hosting and connecting Dot

Use an always-on Linux or macOS host with durable storage. Python 3.9+ is supported; use a maintained Python release for a new host. Dot can install this project on a host it has access to, but its disposable cloud workspace must not be the only copy of the queue.

See the [Sites capability test](sites-probe.md) for the managed-hosting experiment. Sites passed storage and outbound Discord handshake checks, but request-scoped background work stopped before 40 seconds.

## Local computer or private server

Run `dotbot serve --local`. Both Discord and the MCP endpoint run in the foreground; Ctrl+C or SIGTERM stops them. The bot connects outbound to Discord. No inbound Discord endpoint is needed.

The MCP endpoint listens on `127.0.0.1:8765`. This mode intentionally trusts processes on the same machine and omits OAuth discovery. Keep it on a trusted host. `--local --host 0.0.0.0` is rejected.

OpenAI's [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) can connect private MCP servers using outbound HTTPS. It requires a Platform tunnel, a runtime API key, and access for the target ChatGPT workspace. Configure its local HTTP target as `http://127.0.0.1:8765/mcp`. Run the tunnel client alongside DotBot, then select that tunnel when creating the private plugin. Keep tunnel credentials outside this repository.

This project's tunnel and Dot event integration have not been verified live. Secure MCP Tunnel is for private connections and does not meet public plugin submission requirements. Sharing this GitHub application for individual self-deployment is separate from submitting a public plugin.

For unattended use, let your operating system's service manager start the CLI and restart it on failure. For example, on Linux, adapt this systemd unit to your account and checkout:

```ini
[Unit]
Description=Discord DotBot
After=network-online.target
Wants=network-online.target

[Service]
User=YOUR_USER
ExecStart=/absolute/path/Discord-DotBot/.venv/bin/dotbot --data-dir /absolute/private/path/dotbot-data serve --local
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

The service user must own the data directory. On macOS use launchd; on Windows use a service manager or Task Scheduler. No service is installed automatically. Prevent sleep if the device should stay available.

## Add authenticated MCP access

ChatGPT requires OAuth for private tool access. Configure an OAuth 2.1 authorization provider with authorization-code flow, PKCE S256, client registration compatible with ChatGPT, and a `dotbot` scope. Configure it to issue RS256 or ES256 JWT access tokens whose audience matches the relay's public MCP URL. This repository does not provision the provider.

Create `mcp.json` in the relay data directory:

```json
{
  "resource": "https://your-relay.example.com/mcp",
  "issuer": "https://your-auth-provider.example.com/",
  "jwks_url": "https://your-auth-provider.example.com/.well-known/jwks.json",
  "owner_subject": "your-exact-provider-account-subject"
}
```

Use the provider's actual issuer and key URL. The provider must preserve the requested resource/audience. Only tokens for the configured subject, issuer, audience, and scope are accepted. Tokens must be unexpired. JWT revocation is bounded by token expiry; use short-lived access tokens. To revoke the relay immediately, stop it.

Start the combined service:

```sh
.venv/bin/dotbot serve --host 127.0.0.1 --port 8765
```

Put a trusted HTTPS reverse proxy in front of port 8765. Forward `/mcp` and `/.well-known/oauth-protected-resource` to the service, preserving authorization and MCP headers. Keep the backend port private. The service publishes protected resource metadata pointing ChatGPT to the configured provider.

The service needs outbound access to Discord, the provider's public keys, and verified HTTPS event callbacks. Do not log authorization headers or request bodies at the proxy.

## Connect and test with Dot

1. Add the hosted HTTPS MCP URL and complete OAuth, or select the configured Secure MCP Tunnel for a private connection.
2. Rescan tools and events. Confirm `get_message`, `get_context`, `list_pending`, `reply`, `skip`, and `message.created` are visible.
3. Tell Dot to monitor `message.created` for the private test channel, read context, and reply when useful. Ask it to skip messages when it has nothing to add. Channel participants do not inherit access to your private data or other tools.
4. Confirm subscription callback verification succeeds.
5. Send a message without mentioning the bot. Dot should receive an event, fetch the message and context, and either reply or skip. Also test an explicit @mention.
6. Inspect `dotbot show MESSAGE_ID` to confirm delivery, then check the Discord reply.
7. Stop monitoring and confirm further messages produce no events for that subscription.

Tool discovery, OAuth linking, and event delivery have not yet been validated against a live Dot. Workspace plugin and event-task controls still apply.

## Local MCP development

For a local MCP test client only, set a fresh `DOTBOT_MCP_TOKEN` of at least 32 characters and run:

```sh
.venv/bin/dotbot serve --dev
```

This mode binds to `127.0.0.1` only. Send the token as an Authorization bearer header. It is not supported as ChatGPT plugin authentication. Requests use MCP `2026-07-28` metadata and headers; older initialize-based MCP clients are not supported by this endpoint.
