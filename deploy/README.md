# Pomerium + Dex GitHub authentication

These are **deployment templates, not a running deployment**. No OAuth app,
Railway service, public domain, credentials, or ingress has been provisioned.

## Architecture and trust

MCP client → public HTTPS ingress → Pomerium → private WeeChat bridge.
Pomerium delegates interactive login to Dex, which uses GitHub OAuth. Pomerium
owns MCP OAuth discovery, client registration, PKCE and client tokens. Do not
also enable FastMCP GitHubProvider or Pomerium `upstream_oauth2`: those are other
flows with different callback URLs.

The route permits only `OWNER_EMAIL`, the exact email returned by Dex for the
owner's GitHub account. Verify that claim before rollout. Dex normally returns
the public GitHub email, or the primary email when no public email exists.
Do not copy computer-use's `authenticated_user: true`: that application checks
session ownership separately; this bridge controls one user's WeeChat session.

Pomerium replaces the upstream Authorization header with the private
`MCP_AUTH_TOKEN`, which the bridge already validates. Clients receive Pomerium
OAuth tokens, never this backend token. This is service authentication, not
per-user JWT verification. Do not use unverified identity headers in backend
access decisions. Keep the bridge reachable only from Pomerium: no public
Railway domain, TCP proxy, or Docker published port. Possession of the backend
token bypasses Pomerium policy, so isolate it and rotate it as a credential.

For a future identity-aware backend, validate `X-Pomerium-Jwt-Assertion` using a
fixed configured JWKS URL (`https://<MCP_HOST>/.well-known/pomerium/jwks.json`),
allowed signature algorithms, expiration, and exact configured audience and
issuer. Never derive trust from caller-controlled Host or a token's key URL.
The inspected computer-use verifier checks signature, expiration and audience;
it does not explicitly check issuer, so it should not be copied unchanged.

## Inputs required before rendering

Set these in the trusted configuration-rendering environment, not in source:

- `MCP_ORIGIN`: `https://<MCP_HOST>`
- `AUTH_ORIGIN`: `https://<AUTH_HOST>`
- `DEX_ORIGIN`: `https://<DEX_HOST>`
- `BACKEND_ORIGIN`: private bridge origin, e.g. `http://<BRIDGE_PRIVATE_HOST>:8000`
- `DEX_INTERNAL_ORIGIN`: private Dex origin, e.g. `http://<DEX_PRIVATE_HOST>:5556`
- `OWNER_EMAIL`: exact approved owner identity
- `MCP_AUTH_TOKEN`: a cryptographically random URL-safe value of at least 32
  characters, identical to the bridge's environment variable

Run `python deploy/render_config.py`. It writes mode-0600 files under ignored
`deploy/generated/`; those files contain the backend credential. Mount or deliver
them privately to their containers. Do not commit them, print them in build logs,
bake them into a public image, or run the renderer with production secrets in CI.
The templates deliberately have no real hosts or credentials. `@@...@@` fields
are replaced by the renderer with JSON-quoted YAML scalar values. Dex's `$...`
connector references remain for Dex's environment substitution.

## Runtime services

The inspected reference pins `pomerium/pomerium:v0.33.3` and
`ghcr.io/dexidp/dex:v2.45.1`; these are reproducible reference versions, not a
claim that they are the latest security release. Validate upgrades before use.

1. Pomerium: start with `--config /etc/pomerium/config.yaml`, mounting the rendered
   Pomerium file there. It listens on 8080 behind an HTTPS-terminating ingress.
   Supply `SHARED_SECRET`, `COOKIE_SECRET`, `SIGNING_KEY`, `IDP_CLIENT_SECRET`
   through the runtime secret store. Generate keys in Pomerium's documented
   formats. Persistent writable volume: `/data`; run one replica with file
   databroker storage. Health endpoint: private port 28080 (`/readyz`).
2. Dex: run `dex serve /etc/dex/config.yaml` with the rendered Dex file mounted.
   Supply `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `POMERIUM_CLIENT_SECRET`.
   The latter must equal Pomerium's `IDP_CLIENT_SECRET`. Use a writable persistent
   `/data` volume for SQLite, one replica, and private port 5556. Health and
   metrics port 5558 stays private. No Kubernetes storage is needed here.
3. WeeChat bridge: existing container, one process and replica, matching
   `MCP_AUTH_TOKEN`, `MCP_TRANSPORT=http`, `MCP_HOST=0.0.0.0`, `PORT=8000`,
   and `MCP_ALLOWED_HOSTS=<MCP_HOST>` (the exact preserved public Host). Supply
   the documented `IRC_*` runtime settings. Its temporary WeeChat profile and
   history are ephemeral; restarting loses buffered history. Keep HTTP private.
   Do not run multiple workers against the single stateful relay. MCP transport
   stateless mode does not make the WeeChat runtime stateless.

The ingress must route all three public HTTPS origins to the Pomerium listener,
preserving the public Host and scheme. Pomerium routes the Dex host onward to
Dex. `insecure_server: true` is ONLY for the private HTTP hop behind that ingress;
never expose plain HTTP to the internet. Prefer TLS on internal hops where the
platform supports it. Restrict access to private health/admin ports.

## Railway/domain and OAuth checklist

A Railway-generated hostname can be filled in after it is allocated. This
reference topology requires **three distinct public origins** mapped to the
Pomerium ingress, not merely a hostname on the raw bridge. Verify Railway domain
mapping, private networking, volume permissions, and ingress streaming behavior
before deployment. A one-host path layout would need a separate validated design.

- MCP client URL: `https://<MCP_HOST>/mcp`
- GitHub OAuth app callback: `https://<DEX_HOST>/dex/callback`
- Dex issuer: `https://<DEX_HOST>/dex`
- Dex static Pomerium client callback: `https://<AUTH_HOST>/oauth2/callback`

Register the GitHub app only after the Dex origin is fixed. Use a new,
appropriately scoped OAuth app; do not reuse another project's secret or change
its callback blindly. The browser's final MCP-client callback is managed by
Pomerium's MCP OAuth flow and is not the GitHub app callback above. The template
allows known client-metadata domains `chatgpt.com` and `claude.ai`; this restricts
metadata-document origins, not the user allowlist. Dynamic registration does
not itself grant access to WeeChat.

## Streaming and acceptance tests

Preserve `/mcp` unchanged, Authorization, MCP session/protocol headers, and
Accept/Content-Type. Permit HTTP POST and GET and any SDK session DELETE method;
do not reduce the endpoint to POST-only. Avoid SSE response buffering and
caching; the Pomerium route disables total/idle upstream timeouts. The outer
platform ingress can still impose limits; verify those with the target client.

Before exposing real chat data, verify:

1. Direct backend request without or with a wrong token returns 401.
2. Unauthenticated public `/mcp` produces OAuth discovery/401, not tool results.
3. An authenticated but unapproved GitHub identity is denied.
4. The approved owner completes discovery → PKCE login → token → initialize →
   tools/list. Do not print access or refresh tokens.
5. A client-provided backend/identity header cannot override the proxy's header.
6. Streaming survives expected idle periods; session headers survive both hops.
7. Restarting Pomerium/Dex preserves necessary OAuth state via their volumes.
8. The bridge has no publicly reachable bypass; health checks disclose no data.

No end-to-end OAuth, container, or Railway validation is claimed by these files.

## Inspected sources

- [computer-use Pomerium config](https://github.com/r33drichards/computer-use/blob/main/deploy/gke/pomerium-config.yaml)
- [computer-use Dex config](https://github.com/r33drichards/computer-use/blob/main/deploy/gke/dex-config.yaml)
- [computer-use verifier](https://github.com/r33drichards/computer-use/blob/main/backend/internal/auth/auth.go)
- [computer-use network isolation](https://github.com/r33drichards/computer-use/blob/main/deploy/base/networkpolicy.yaml)
- [Pomerium headers](https://www.pomerium.com/docs/reference/routes/headers)
- [Pomerium MCP reference](https://www.pomerium.com/docs/capabilities/mcp/reference)
- [Pomerium identity validation](https://www.pomerium.com/docs/capabilities/getting-users-identity)
- [Pomerium timeouts](https://www.pomerium.com/docs/internals/connection)
- [Pomerium internal HTTP mode](https://www.pomerium.com/docs/reference/insecure-server)
- [Dex GitHub connector](https://dexidp.io/docs/connectors/github/)
- [Dex getting started](https://dexidp.io/docs/getting-started/)
- [FastMCP HTTP deployment](https://gofastmcp.com/deployment/http)
