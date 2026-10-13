# Local validation: two-origin authentication layout

Status as of 2026-10-13: locally prepared, not live-OAuth validated. No deployment,
GitHub application creation, production credential use or publication is represented
by this change.

## Passed

- Real Dex v2.45.1, official static binary, SHA256
  `217aa675738d1cbaf25fe8d7bc04cb00dbc60f91d8465b76b8d8af963d571337`.
  Real nginx 1.22.1-9+deb12u10 from official Debian bookworm-security, SHA256
  `62b20544dc6f509184ab550d15658bbc9fd18ca720235fda14eb8ef91e09d595`.
- `deploy/auth_router_test.py` uses the actual templates, renderer, supervisor,
  nginx and Dex with isolated ports/storage and synthetic connector secrets.
  Discovery advertises the external auth origin `/dex` issuer. Real Dex produces
  the GitHub authorization URL with the exact auth-origin `/dex/callback`; the
  test stops before contacting GitHub.
- The actual nginx process rejects wrong Hosts, MCP/discovery/admin/unknown paths,
  encoded path bypasses, duplicate slashes and dot segments. It forwards exact
  callbacks with unchanged query/body and fixes Host/HTTPS forwarding headers.
- IPv6 public listening and private Pomerium-upstream dialing work in the test.
- Cookie/Set-Cookie/Location and MCP protocol/session header passthrough plus
  immediate streaming were tested against an explicitly **mocked Pomerium**.
  These results do not prove Pomerium's own cookie or MCP stream behavior.
- Actual SIGTERM stops real nginx and Dex, including all test listeners. Synthetic
  credentials and successful callback query values were absent from logs.
- An independent negative test found nginx error logs leaked OAuth query values
  on 502. Router error logging is therefore disabled; a regression test covers
  the failed-upstream path as well.
- Unit regressions cover strict interpolation, two distinct canonical origins,
  private/atomic outputs, symlink refusal, split service inputs/secret filtering,
  real subprocess failure/shutdown and mocked bounded kill escalation.

## Verified source behavior

Pinned Pomerium v0.33.3 commit `76042ed40db3bab9506b689f436716b6df624f23`
mounts authentication by actual HTTP Host, derives the IdP callback from the
configured external auth origin, and defaults to host-only Secure cookies.
Its exact auth routes/assets are enumerated in the router. The proxy-side
`/.pomerium/callback/` and MCP OAuth endpoints remain on the MCP origin.
Sources: `authenticate/handlers.go`, `authenticate/state.go`,
`internal/controlplane/server.go`, `internal/urlutil/url.go`,
`config/envoyconfig/routes.go` in the pinned upstream release.

A stable GitHub binding is source-supported but not assumed from the numeric ID
alone: Dex defaults GitHub identity to the numeric ID, then encodes its subject
as base64url (no padding) of protobuf `IDTokenSubject{user_id, conn_id:"github"}`.
Pomerium copies this subject into session user_id; its PPL `user` criterion can
match that value. Before changing the policy, derive and verify the actual claim.
The default policy here remains the exact owner-email allowlist.

## Blocked or not run

Official Pomerium v0.33.3 binary SHA256
`b33f6bbb57c5c724f99d7d84f17e6f06e07e96b33b9bb239427e0e096e112b05`
parsed the rendered config and started control-plane services locally. Bundled
Envoy then failed to bind its hot-restart Unix socket (`errno=9`) in this sandbox.
The released Pomerium CLI has no supported switch to disable that socket. There
is no Docker/podman runtime in this environment. This is a test-environment
blocker, not evidence that Railway will fail or succeed.

`deploy/container_auth_test.py` and the CI job are prepared but **not run here**.
They build both actual images and use local TLS plus generated-style hostnames
to exercise actual Pomerium/Envoy startup, MCP 401/discovery/DCR/PKCE start, initial
OIDC redirects, secure host-only cookies, route isolation and shutdown. The
script must pass in Docker-capable CI before source activation.

Real GitHub consent/login, live identity claims, owner-vs-other-user authorization,
OAuth token exchange, authorized backend header replacement, end-to-end MCP
streaming, Railway ingress/private DNS, persistence, and hard resource caps remain
live acceptance gates. Do not describe the topology as fully validated until
those gates pass.
