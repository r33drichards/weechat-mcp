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
Keep the separately approved $75/month total budget visible in Railway. Mount a
separate persistent volume at `/data` for each auth service. Use the image's default
entrypoint and user: a short root initializer accepts a root-owned empty volume,
sets only `/data` to UID/GID 10001 and mode 0700, then clears supplementary groups
and permanently drops all root UID/GID identities before rendering config or
starting services. No manual volume ownership step is required.

State paths remain `/data/dex.db` (including SQLite sidecars) and
`/data/databroker`, so existing correctly owned state is reused on restart. Startup
fails closed on symlinks, unsafe ownership/permissions, or unwritable storage.
Existing state must belong to UID/GID 10001 and not be group/world-writable; regular
state files must not be hard links. The initializer never recursively changes
ownership or modifies unrelated volume contents. For older root-owned state,
stop the service and have an administrator validate and repair only its state
files from a backup before restarting. Do not mount one volume in both services.

MCP clients connect to Pomerium. Pomerium owns MCP discovery, client registration,
PKCE and client tokens. It delegates OIDC to Dex, which uses GitHub OAuth. Pomerium
replaces upstream Authorization with the separate private bridge bearer token by
default. Explicit `railway-private` mode removes that extra bearer and strips
Authorization before proxying instead. The bridge keeps its Host/Origin allowlist
in both modes. Public Pomerium owner-only GitHub/Dex authentication is unchanged.
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
- Default `MCP_AUTH_MODE=bearer`: `MCP_AUTH_TOKEN` is at least 32 cryptographically
  random URL-safe characters, identical to the bridge token. For the private-only
  bootstrap below, set `MCP_AUTH_MODE=railway-private` and omit `MCP_AUTH_TOKEN`.
- `POMERIUM_LOCAL_KEYS=true`: generate separate session keys once on the private volume.
  Omit `SHARED_SECRET`, `COOKIE_SECRET`, `SIGNING_KEY`, `IDP_CLIENT_SECRET` and their
  `_FILE` overrides in this managed mode; see [key lifecycle](LOCAL_KEYS.md).
- The Dex OIDC client is public and requires S256 PKCE; it has no shared client secret.

Auth service:

- `AUTH_ORIGIN`: same auth generated origin as above
- `POMERIUM_INTERNAL_ORIGIN=http://<pomerium>.railway.internal:8080`
- `PORT=8080`
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`: dedicated GitHub OAuth app
- Do not set `POMERIUM_CLIENT_SECRET`: Dex 2.46.0 enforces S256 PKCE for the exact
  registered Pomerium HTTPS callback, rejecting missing PKCE and plain challenges.

Bridge service:

- `MCP_TRANSPORT=http`, `PORT=8000`; bearer/private-mode settings as below
- `MCP_ALLOWED_HOSTS=<MCP-generated-host>`
- Bearer mode: `MCP_AUTH_TOKEN`, `MCP_HOST=::` for Railway private IPv6.
- Private-only mode: `MCP_AUTH_MODE=railway-private`,
  `MCP_HOST=${{RAILWAY_PRIVATE_DOMAIN}}`, and no `MCP_AUTH_TOKEN`. This binds the
  service's own private DNS address rather than a wildcard/public interface.
  Verify DNS resolution and bind reachability from Pomerium before activation.
- The documented `IRC_*` settings. Use an isolated nickname/profile and only
  allowlisted channels. One process/replica; profile and chat history are ephemeral.

### Private-only bootstrap prerequisite

Set `MCP_AUTH_MODE=railway-private` on **both bridge and Pomerium**. Both require
Railway's runtime `RAILWAY_ENVIRONMENT_ID` and `RAILWAY_SERVICE_ID`. Pomerium accepts
only a `*.railway.internal` backend in this mode. The bridge requires its exact
`RAILWAY_PRIVATE_DOMAIN` as `MCP_HOST` and a nonempty, wildcard-free
`MCP_ALLOWED_HOSTS` list. It refuses startup when `RAILWAY_PUBLIC_DOMAIN` or
`RAILWAY_TCP_PROXY_DOMAIN` is present.

Before enabling this mode, verify in Railway that the bridge has **no generated
domain, custom domain, or TCP proxy**. Code cannot prove remote ingress settings;
runtime variables are only guardrails. Do not add public ingress later. All
services in the Railway environment are inside the trust boundary and can reach
this tokenless bridge if they supply an allowed Host. Keep that environment
restricted to trusted services. The Host allowlist is not authentication.

The public MCP endpoint remains Pomerium, with the same exact owner-email policy
and GitHub/Dex OAuth. Three services, replicas, resource limits and the $75/month
budget are unchanged. Outside this explicit mode, missing/invalid bearer tokens
still fail closed. Pomerium's optional local session keys never leave its service.
There is no cross-service secret exchange.

Allocate both generated domains before registering the dedicated GitHub app:

- MCP endpoint: `https://<MCP-generated-host>/mcp`
- Dex issuer: `https://<auth-generated-host>/dex`
- GitHub OAuth app callback: `https://<auth-generated-host>/dex/callback`
- Dex's Pomerium client callback: `https://<auth-generated-host>/oauth2/callback`

The client's final OAuth callback is managed by Pomerium and is different from
both service callbacks. `chatgpt.com` and `claude.ai` are allowed client-metadata
domains, not user access policies. Dynamic registration grants no chat access.

## Secret handling and lifecycle

Optional [volume-local Pomerium keys](LOCAL_KEYS.md) are available with explicit
`POMERIUM_LOCAL_KEYS=true`; the default remains externally supplied keys.

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

1. Verify the bridge has no public domains or TCP proxy and no public bypass.
   In bearer mode, also verify missing/wrong token rejection. In private mode,
   verify private-only bind/reachability, allowed-Host access without a token,
   and wrong-Host rejection.
2. Verify public unauthenticated MCP returns 401/discovery, with no tool data.
3. Complete owner GitHub login, inspect exact email/subject, exchange PKCE token,
   initialize MCP and list tools. Verify an unapproved account is denied.
4. Verify client identity/backend headers cannot bypass Pomerium: Authorization
   is replaced in bearer mode and absent at the bridge in private mode.
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
- [Railway private networking](https://docs.railway.com/networking/private-networking)
- [Railway runtime variables](https://docs.railway.com/variables/reference)
