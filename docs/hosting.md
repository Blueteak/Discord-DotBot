# Hosting and connecting Dot

Use an always-on Linux or macOS host with durable storage. Python 3.9+ is supported; use a maintained Python release for a new host. Dot can install this project on a host it has access to, but its disposable cloud workspace must not be the only copy of the queue.

## Run the relay

Follow the [README installation steps](../README.md#relay-installation). Run `dotbot run` under your host's process supervisor. All commands must use the same data directory. Set `DOTBOT_DATA_DIR` to an absolute path if commands run from different working directories.

The Discord connection is outbound. The local CLI does not expose an HTTP server.

Alternatively, use Docker with a persistent named volume:

```sh
docker build -t discord-dotbot .
docker volume create dotbot-data
docker run --rm -it -v dotbot-data:/data discord-dotbot setup
docker run -d --name discord-dotbot --restart unless-stopped \
  -v dotbot-data:/data discord-dotbot
```

The image runs as a non-root user. Keep the volume when replacing the container. This runs the Discord relay; MCP connectivity requires the additional setup below. The Docker image has not been built locally because the Docker daemon is unavailable. The native Python installation and local tests pass.

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

In a container, run `serve --host 0.0.0.0 --port 8765`, publish the port only to the host's loopback interface, and keep `mcp.json` in the persistent volume.

The service needs outbound access to Discord, the provider's public keys, and verified HTTPS event callbacks. Do not log authorization headers or request bodies at the proxy.

## Connect and test with Dot

1. Add the hosted HTTPS MCP URL to a private ChatGPT plugin and complete its OAuth connection.
2. Rescan tools and events. Confirm `get_message`, `list_pending`, `reply`, and `message.created` are visible.
3. Tell Dot to monitor `message.created` for the private test channel and respond to your requests using the read/reply tools.
4. Confirm subscription callback verification succeeds.
5. Mention your bot in that channel. Dot should receive an event, fetch the message, and queue one reply.
6. Inspect `dotbot show MESSAGE_ID` to confirm delivery, then check the Discord reply.
7. Stop monitoring and confirm further messages produce no events for that subscription.

Tool discovery, OAuth linking, and event delivery have not yet been validated against a live Dot. Workspace plugin and event-task controls still apply.

## Local MCP development

For a local MCP test client only, set a fresh `DOTBOT_MCP_TOKEN` of at least 32 characters and run:

```sh
.venv/bin/dotbot serve --dev
```

This mode binds to `127.0.0.1` only. Send the token as an Authorization bearer header. It is not supported as ChatGPT plugin authentication. Requests use MCP `2026-07-28` metadata and headers; older initialize-based MCP clients are not supported by this endpoint.
