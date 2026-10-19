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

A synthetic full Dex v2.46.0 HTTP token-endpoint test passed strict rotation across
alternating and concurrent consumers plus coordinator restart. Direct old-bearer
replay returned invalid_grant. Simulated response loss after successful HTTP
rotation prevented another call, including after restart.

This is not a full deployed Pomerium/Dex/GitHub authorization flow. The successful
fixture enables Dex sessions and uses a stored synthetic identity. Both the initial sessions-disabled in-memory fixture and a separate plain-Dex
SQLite fixture (no coordinator) hit a nested-storage deadlock on the second
refresh. Production uses SQLite without a sessions stanza. This confirmed
additional blocker must be repaired and verified separately; enabling sessions merely to make the test pass
is not an approved production remedy. No real GitHub credentials or login were
used. See the separate Dex acceptance evidence for fixture details.

## Not yet verified; required before live deployment

1. Build the actual source-patched Pomerium image and gateway wrapper, including
   UI/Envoy assets. Docker/Podman is unavailable in this local workspace. A local
   Go compile-only binary, if built with external-asset tags, is not this image.
2. Run the prepared GitHub Actions source build and isolated container topology
   smoke, including same-volume second-process rejection and container restart.
   The user approved branch/draft-PR CI publication and deployment when ready;
   this approval does not establish technical readiness.
3. Verify repeated sessions-disabled SQLite Dex refresh with fresh connector
   identity checks, followed by the complete two-client Pomerium flow in an
   isolated image. Do not claim the sessions-enabled fixture proves this.
4. Verify final source/patch/image digests and the live service's one-replica,
   one-volume, all-in-one configuration. No old/new writer overlap is allowed.
5. Obtain explicit approval for the gateway rollout and expected one-time browser
   and MCP reauthorization. Any actual GitHub login stays with the user/secure
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

Decision: reviewed source candidate; not approved or proven ready for live rollout.
