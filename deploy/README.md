# Two generated Railway domains, three services

This layout uses only generated `*.up.railway.app` domains. It is local deployment
preparation, not a claim that resources, credentials or a working login exist.

## Traffic and trust boundaries

1. Private **bridge**: existing root `Dockerfile`, one replica, no public domain
   or TCP proxy. Limit: 1 CPU / 1 GB RAM.
2. Public **Pomerium**: `deploy/pomerium/Dockerfile`, one replica, generated MCP
   origin, port 8080. Limit: 0.5 CPU / 1 GB RAM. Persistent `/data` volume.
3. Public **auth**: `deploy/auth/Dockerfile`, Dex plus a small nginx router in one
   container, one replica, second generated origin, port 8080. Limit: 0.5 CPU /
   0.5 GB RAM. Persistent `/data` volume for Dex SQLite.

The three service limits are an upper-bound resource plan, not a billing cap.
Keep the separately approved $75/month total budget visible in Railway. Configure
volume ownership for UID/GID 10001 before starting either auth container.

MCP clients connect to Pomerium. Pomerium owns MCP discovery, client registration,
PKCE and client tokens. It delegates OIDC to Dex, which uses GitHub OAuth. Pomerium
replaces upstream Authorization with the separate private bridge bearer token.
The bridge validates that token and accepts only its configured public MCP Host.
Do not also enable FastMCP GitHubProvider or Pomerium `upstream_oauth2`.

Auth ingress maps `/dex` and `/dex/*` unchanged to loopback Dex. A pinned, exact
allowlist of Pomerium authentication paths goes to private Pomerium. Everything
else is denied, including MCP, MCP discovery, proxy callbacks, admin and metrics.
The authenticate origin is not a Pomerium route: its only route is the distinct
MCP origin. There is no Dex-through-Pomerium loop.

Railway terminates HTTPS. The auth router validates the incoming Host and fixes
upstream Host and forwarded scheme/host to the configured external auth origin.
It clears caller-supplied reproxy/identity headers. Keep Pomerium's cookie domain
unset so unrelated generated domains get host-only Secure cookies. Dex shares the
authentication cookie security boundary because it shares that hostname.

## Runtime inputs

Pomerium service:

- `MCP_ORIGIN=https://<MCP-generated-host>`
- `AUTH_ORIGIN=https://<auth-generated-host>`; must differ from MCP origin
- `BACKEND_ORIGIN=http://<bridge>.railway.internal:8000`
- `OWNER_EMAIL`: exact owner email returned by Dex; verify the live claim
- `MCP_AUTH_TOKEN`: at least 32 cryptographically random URL-safe characters,
  identical to the bridge token
- `SHARED_SECRET`, `COOKIE_SECRET`, `SIGNING_KEY`: Pomerium's documented key formats
- `IDP_CLIENT_SECRET`: same value as auth's `POMERIUM_CLIENT_SECRET`

Auth service:

- `AUTH_ORIGIN`: same auth generated origin as above
- `POMERIUM_INTERNAL_ORIGIN=http://<pomerium>.railway.internal:8080`
- `PORT=8080`
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`: dedicated GitHub OAuth app
- `POMERIUM_CLIENT_SECRET`: same as Pomerium's `IDP_CLIENT_SECRET`

Bridge service:

- `MCP_TRANSPORT=http`, `PORT=8000`, `MCP_AUTH_TOKEN`
- `MCP_ALLOWED_HOSTS=<MCP-generated-host>`
- `MCP_HOST=::` for Railway private IPv6; verify resolution and bind reachability
  from Pomerium before activation. Older Railway networks may be IPv6-only.
- The documented `IRC_*` settings. Use an isolated nickname/profile and only
  allowlisted channels. One process/replica; profile and chat history are ephemeral.

Allocate both generated domains before registering the dedicated GitHub app:

- MCP endpoint: `https://<MCP-generated-host>/mcp`
- Dex issuer: `https://<auth-generated-host>/dex`
- GitHub OAuth app callback: `https://<auth-generated-host>/dex/callback`
- Dex's Pomerium client callback: `https://<auth-generated-host>/oauth2/callback`

The client's final OAuth callback is managed by Pomerium and is different from
both service callbacks. `chatgpt.com` and `claude.ai` are allowed client-metadata
domains, not user access policies. Dynamic registration grants no chat access.

## Secret handling and lifecycle

Each image renders only its required runtime files into a private temporary
0700 directory, with atomically replaced 0600 files. No production secrets belong
in source, image layers, build arguments or CI. The image build context explicitly
excludes generated files. Manual preparation, if needed:

```sh
python deploy/render_config.py --service pomerium
python deploy/render_config.py --service auth
```

`--service all` renders both using both sets of inputs. Generated files are ignored.
The auth supervisor starts real Dex and nginx, stops both if either exits, handles
SIGTERM, escalates stuck children, and reaps them. Dex binds 5556/5558 only on
loopback; Pomerium health binds 28080 only on loopback. Only 8080 is exposed in the
auth images. Do not add public port mappings for internal/admin services.

The router preserves query/body, Cookie/Set-Cookie/Location, standard methods and
streaming headers; it disables response/request buffering and caching. Its DNS
resolver is derived from runtime `/etc/resolv.conf`, supports IPv6 and refreshes
private upstream addresses. Pomerium's MCP route has no total or idle timeout.
Railway and client stream limits still require live validation.

nginx request/access and error logs are suppressed: upstream errors otherwise log
OAuth codes in URLs even with access logs disabled. Syntax checks and supervisor
failure notices remain visible. Do not enable verbose/request logging with real
credentials. Inspect configuration locally with synthetic values instead.

## Validation status and commands

See [VALIDATION.md](VALIDATION.md) for exact evidence and remaining gates.

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check --error-on-warning
uv run --locked python -m unittest discover -s . -v
# Linux Docker; builds and runs actual auth images behind a local TLS fixture:
python3 deploy/container_auth_test.py
# Host-only real nginx/Dex plus an explicitly mocked Pomerium upstream:
python3 deploy/auth_router_test.py --nginx /path/to/nginx \
  --dex /path/to/dex --dex-web /path/to/dex/web
```

The Docker test is required before activating the source images. It does not
replace live owner and other-user OAuth tests. Before exposing real chat data:

1. Verify private backend missing/wrong token rejection and no public bypass.
2. Verify public unauthenticated MCP returns 401/discovery, with no tool data.
3. Complete owner GitHub login, inspect exact email/subject, exchange PKCE token,
   initialize MCP and list tools. Verify an unapproved account is denied.
4. Verify client identity/backend headers cannot override Pomerium's injected token.
5. Exercise actual subscriptions, idle/reconnect, methods and session headers
   through Railway and the target client.
6. Restart both auth services; verify durable OAuth state and volume ownership.
7. Verify all public auth/MCP domains reject access to metrics/admin/private data.

## Sources

- [Pomerium authentication origin separation](https://www.pomerium.com/docs/capabilities/authentication)
- [Pomerium v0.33.3 authenticate routes](https://github.com/pomerium/pomerium/blob/v0.33.3/authenticate/handlers.go)
- [Pomerium MCP reference](https://www.pomerium.com/docs/capabilities/mcp/reference)
- [Pomerium headers](https://www.pomerium.com/docs/reference/routes/headers)
- [Dex GitHub connector and callback](https://dexidp.io/docs/connectors/github/)
- [nginx proxy behavior](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
- [Railway networking and domain behavior](https://docs.railway.com/networking/domains/working-with-domains)
