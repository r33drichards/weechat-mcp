# WeeChat shared refresh-token repair

Local candidate, not deployed or published. AI-assisted implementation and review
using OpenAI dot. This is a private deployment patch, not an upstream contribution.

## Scope and ownership

Pinned Pomerium v0.33.3: 76042ed40db3bab9506b689f436716b6df624f23.
The deployment wrapper was prepared from the f822f5a snapshot and ported onto
GitHub main 7d934730 without replacing later bridge changes. Actual deployed
route/config additions must be preserved separately during snapshot rollout.
Dex rotation, initial interactive authentication, S256 PKCE, downstream client
binding, session/grant expiry, and upstream claim revalidation remain in place.
No production credentials were read, generated, rotated, or configured.

The candidate also includes a separately reviewed Dex v2.46.0 read-order fix.
Its sessions-disabled SQLite path otherwise deadlocks on the second refresh.
Only stored connector-data lookup moves before the transaction; token validation
and actual connector refresh remain inside it. The schema, sessions setting,
rotation, scopes, and client checks do not change. See dex-acceptance/README.md.

Each generic OIDC login stores its Dex refresh bearer in a private durable record
and returns an opaque reference in the existing OAuth refresh-token field. The
browser session, background manager, MCP grants, and recreated sessions therefore
share one lineage without schema changes. Every upstream refresh serializes on
that lineage, rereads its latest bearer, durably marks it in flight, then commits
the successor before returning to claim/session processing. All callers still
perform an upstream refresh; this does not cache or skip identity revalidation.

The authority is installed once before services start and survives configuration
reloads. It binds lineages to issuer, token endpoint, and client ID. New interactive
logins receive independent references. Failed or uncertain calls cannot replay the
old bearer; in-flight records after a crash require reauthentication. If a final
write reached disk but its acknowledgement failed, a restart can observe either
an in-flight/failed record or the durably written successor, never a resurrected
predecessor. The current process remains poisoned after any such failure.

Only the ID-token extra is returned with tokens; raw response extras are dropped
so they cannot retain another copy of the refresh bearer. Dex public-client
refresh uses explicit form authentication, avoiding OAuth2 automatic retry, and
requires an explicitly returned refresh successor, avoiding OAuth2's silent
old-token fallback.

## Supported deployment and migration

- One all-in-one generic OIDC gateway, public Dex client, local persistent file DB.
- WEECHAT_REFRESH_AUTHORITY_DIR must equal the absolute file DB path plus
  `.refresh-authority`; the candidate image uses `/data/databroker.refresh-authority`.
- That directory and its records are private, with a lifetime process flock.
  A second process using the same volume is rejected. Do not use network storage,
  split services, clustering, or replicas with separate/copied volumes.
- DSN files and managed mode are rejected. Route providers must remain generic
  OIDC public clients. Configuration reload cannot move the database path.
- Deploy the bounded Dex/auth image fix first, preserving all current routes,
  environment values, volumes and frontend assets. Its storage schema is unchanged.
  Then replace the gateway with no old/new overlap and reauthorize affected
  browser/MCP connections. Validate any additional upstream Dex clients sharing
  rotating connector credentials before using this single-client repair.
- Stop old writers before startup. Do not overlap old and new image versions.
  An old binary does not participate in the new lock and cannot be fenced by it.
- Existing raw-token sessions/grants are intentionally not imported. Affected
  browser and MCP connections need one new authorization after migration.
- Preserve the DB, authority directory, and existing key bundle as one recovery
  unit. Never restore stale token snapshots while Dex state has moved forward.
- Rollback requires stopping new writers, restoring the prior image/config, and
  fresh authorization. Do not replay obsolete saved tokens to avoid reconnects.
- No automatic lineage garbage collection is implemented. Plan offline cleanup
  only after proving records are no longer referenced; do not delete active files.

Existing downstream MCP refresh-token rotation is not made transactionally atomic
by this patch. It repairs shared upstream Dex token ownership only.

## Building the candidate

With Docker installed, from the repository root run:

    deploy/pomerium/refresh-authority/build.sh

The gateway multi-stage Dockerfile fetches the exact Pomerium commit, checks/applies this patch, runs the
focused Go race suites in the pinned upstream build stage, builds its UI/Envoy
assets with the upstream Docker workflow, and builds the gateway wrapper locally. A clean remote Docker builder can use
this same Dockerfile; no unpublished local base tag or registry upload is required.
It does not push, deploy, log in, or access production volumes. Image references
are fixed upstream versions/digests where provided by the pinned Dockerfile;
verify final image digests and scan the candidate before rollout.

The local source checks do not replace an actual container build, isolated
end-to-end image checks, or the separately approved live smoke/reconnection step.
See VALIDATION.md for actual completed checks and remaining limits.

The auth-service Dockerfile also builds the exact patched Dex source and runs the
portable sessions-disabled SQLite/coordinator acceptance suite before compilation.
It keeps the pinned original Dex frontend assets. The PR auth-topology job builds
both images; building only the gateway is not sufficient for this repair.
