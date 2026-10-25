# Validation and deployment gate

## Completed locally (2026-10-19 UTC)

- Exact Pomerium base commit: 76042ed40db3bab9506b689f436716b6df624f23.
- Go 1.26.3, workspace-local compiler; no system compiler installation.
- Full race-enabled packages: pkg/identity/refreshcoord, pkg/identity/oidc,
  pkg/identity/manager, internal/mcp passed.
- Full config package passed with one baseline-failing subtest excluded:
  TestIncomingIDPTokenSessionCreator_CreateSession/proxy_protocol.
  The same failure was reproduced on a pristine archive of the exact base:
  proxy identity-token verification received an empty HTTP response instead of
  JSON. It is not introduced by this repair; the unskipped full suite is not green.
- The new real-manager/real-MCP regressions passed ten race-enabled repetitions.
  Browser background refresh preserves the MCP grant; two downstream clients
  alternate four rounds beyond the synthetic grace interval, with fresh claims.
- Coordinator coverage includes concurrency, restart, process exclusion, private
  filesystem checks, malformed/missing/binding-mismatched references, nil/empty
  results, ambiguous calls, failure before and after persistence, panic,
  cancellation, successor recovery after lost fsync acknowledgement, and extras
  sanitization. Independent logins remain distinct.
- Real generic OIDC HTTP tests cover authentication initialization, parallel stale
  copies, provider recreation, binding errors, unavailable authority, one request
  after an ambiguous 500, and missing/empty returned refresh-token poisoning.
- go vet passed for the four main affected packages above.
- Deployment rendering/key persistence tests: 76 passed. The full local wrapper
  snapshot suite passed 118 tests, plus Ruff lint/format and ty checks. Changes
  were ported onto current main 7d934730 without overwriting newer bridge work;
  the PR CI is responsible for validating the complete current-main suite.
- Source patch applies cleanly to the pristine pinned Pomerium source.
- Independent review found three issues, all corrected and retested: raw OAuth
  extras retaining a bearer, reloadable DSN-file drift, and OAuth2 silently filling
  an omitted successor with the old bearer. No remaining source blockers found
  in that review's scope.

## Real Dex coverage and limits

Real Dex v2.46.0 sessions-disabled HTTP acceptance now passes for both memory
and SQLite, with strict rotation and synthetic connector identities. Three runs
per backend cover 16 alternating/concurrent refreshes, authority restart, direct
old-bearer invalid_grant, and simulated lost-response fail-closed behavior.

The original plain-Dex SQLite test (without any coordinator) reproduced a real
nested-storage deadlock at refresh two. A separately reviewed 17-line read-order
change fixes it without moving connector refresh outside current-token validation.
The complete Dex server and token race suites pass with both HTTP acceptance tags.
The portable CI bundle copies the exact patched coordinator source, rather than
maintaining another implementation. Cross-client shared rotating connector
credentials remain outside this narrow single-client fix.

These checks use synthetic connector identities, not live GitHub. The packet-loss
case injects a client error after a real successful HTTP response. They do not
claim a complete deployed Pomerium browser/MCP/GitHub flow. A Pomerium command
compile with external-asset tags also passed; it is not the full UI/Envoy image.

## Not yet verified; required before live deployment

1. Rebuild the final source-patched Pomerium and Dex images. The first CI run
   successfully built Pomerium including UI/Envoy and passed its race tests;
   topology boot and real Dex PKCE checks passed. The process-lock probe failed
   because its Python isolated-mode invocation removed sibling imports; the
   probe now uses the same invocation as the production supervisor. Final image
   integration still needs a passing run, including
   UI/Envoy assets. Docker/Podman is unavailable in this local workspace. A local
   Go compile-only binary, if built with external-asset tags, is not this image.
2. Run the prepared GitHub Actions source build and isolated container topology
   smoke, including same-volume second-process rejection and container restart.
   The user approved branch/draft-PR CI publication and deployment when ready;
   this approval does not establish technical readiness.
3. Run the complete two-client Pomerium/Dex flow in the isolated final images,
   retaining the now-passing sessions-disabled SQLite connector checks. The
   opt-in --refresh-flow fixture uses synthetic mockCallback identity and checks
   two complete DCR/PKCE grants, overlapping strict-rotation refreshes and
   refresh after process recreation. Its execution is pending. Live source audit
   confirmed exactly one Dex client, pomerium, inherited by all three routes.
4. Verify final source/patch/image digests and the live service's one-replica,
   one-volume, all-in-one configuration. No old/new writer overlap is allowed.
5. The user authorized deployment when ready; confirm readiness and communicate
   the expected one-time browser/MCP reauthorization before rollout. Any actual
   GitHub login stays with the user/secure
   login flow. Never retrieve or paste production credentials into test fixtures.

## Storage and logs

Authority records contain plaintext upstream refresh bearers on the same private
persistent volume as Pomerium's local file database. They are not application-
encrypted. Directory mode is 0700, records/lock 0600, and deployment startup checks
UID/GID 10001 ownership before services run. The process holds its exclusive lock
until exit. The entire volume and backups must be treated as credential material.
The new refresh path returns generic failure messages without token response
bodies or bearers; it does not log token values. Existing unrelated authentication
logging is unchanged. No production secrets were read or included in artifacts.

Decision: deployment authorized when ready; technical rollout gates still pending.

Live gateway DIP/Claude route-renderer additions are preserved with regression
tests for distinct private backends and the same owner-only authentication policy.

## Issue #10 increment validation (2026-10-25, cuse agent, this repository)

- Environment: linux/amd64, Go 1.27.3, CGO_ENABLED=1 (Nix gcc wrapper),
  Dex v2.46.0 (05a27505) + read-order patch + dex-issue-10-independent-grants.patch.
- Patch generated from and re-applied to a fresh clone: git apply --check and
  apply both clean on top of the read-order patch.
- go build ./server/... green; go test ./server/... green (17 packages);
  go test ./storage/... green; run.sh tag set
  'weechat_independent_grants weechat_shared_connector' green standalone and
  combined with the existing acceptance tags; -race runs green.
- Container/browser E2E (container_auth_test --refresh-flow, including the new
  establish_independent third-browser scenario) requires Docker and runs in
  CI; not executed locally here.
