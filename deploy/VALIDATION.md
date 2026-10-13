# Local validation: two-origin authentication layout

Status as of 2026-10-13: prepared for real-container CI, not live-OAuth validated.
This file records source/test evidence; it does not establish deployment or live
GitHub login. The PR checks must pass for the exact source revision being deployed.

## Passed

- Real Dex v2.46.0, official image binary, SHA256
  `77731f0dd0603c14ab363dc127687c6b388ffa828b6d46368c9f4009d80ff183`.
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

- `deploy/dex_pkce_test.py` retains the production Dex template's public client,
  exact HTTPS callback and mandatory S256 settings, replacing only the identity
  connector with a synthetic callback and using isolated listeners/storage. Real
  Dex rejects missing/plain PKCE, missing/wrong verifier and six incorrect callback
  variants; both form client ID and Pomerium-style empty Basic secret exchanges pass.
- Explicit Railway-private bridge mode has middleware regressions for tokenless
  initialization, wrong Host/Origin denial, and unchanged default bearer rejection.
  Its platform-marker checks do not prove that Railway ingress is absent.
- Opt-in Pomerium local-key coverage includes private atomic persistence, eight
  concurrent processes, stable-lock and fsync behavior, restart identity,
  corruption/link/permission rejection, missing keys beside existing databroker
  state, legacy override conflicts and credential-free errors.

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
to exercise Pomerium/Envoy startup, MCP 401/discovery/DCR/PKCE start, enforced Dex
S256 and token exchange against a synthetic connector, initial OIDC redirects,
secure host-only cookies, route isolation, root-owned empty volumes, non-root
process identities, persistent keys/client registration across recreation and
shutdown. The separate bridge Docker job exercises bearer and simulated
Railway-private tokenless MCP/IRC roundtrips using one actual image. These scripts
must pass in Docker-capable CI before source activation.

Real GitHub consent/login, live identity claims, owner-vs-other-user authorization,
OAuth token exchange, authorized backend header removal in private mode (or replacement in bearer mode), end-to-end MCP
streaming, Railway ingress/private DNS, persistence, and hard resource caps remain
live acceptance gates. Do not describe the topology as fully validated until
those gates pass.
