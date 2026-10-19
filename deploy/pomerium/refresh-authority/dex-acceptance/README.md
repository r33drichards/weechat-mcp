# Dex HTTP acceptance and bounded SQLite repair

AI-assisted local source/test bundle, created with OpenAI Codex. No credentials,
accounts, production volume, session setting, or deployment is accessed by it.

## Portable build-stage usage

Requirements: official Go 1.27.x image, Git, and a C compiler. On Alpine install
`build-base` (and `git` if absent); keep `CGO_ENABLED=1`. No absolute workspace
paths are embedded in this bundle.

Given a Dex v2.46.0 source checkout and a Pomerium v0.33.3 checkout with the
reviewed Pomerium authority patch already applied:

```sh
bundle=/path/to/deploy/pomerium/refresh-authority/dex-acceptance
"$bundle/run.sh" "$DEX_SOURCE" "$PATCHED_POMERIUM_SOURCE"
```

Run this before building the patched Dex binary. The script applies the bounded Dex patch to pristine target files, or verifies
that those files exactly match the already-applied patch; it refuses other
target-file edits. The patch remains applied for the following binary build. It copies the exact coordinator source from the
patched Pomerium checkout into a temporary module; prints matching checksums;
and runs the full Dex server/token race suites with both HTTP regression tags.
It uses an alternate temporary modfile and does not modify Dex's go.mod/go.sum.
A temporary acceptance test is removed on exit. An identical preexisting test
is preserved; a different one causes a failure rather than being overwritten.
`GO_BINARY` optionally selects a compiler; it defaults to `go` on PATH.

## Why Dex needs a source patch

With sessions disabled, Dex's first refresh moves connector data off the legacy
refresh-token record. On the next refresh, the transaction callback reads that
data through the outer storage handle. This deadlocks SQLite's single connection
(and memory storage's non-reentrant mutex). A plain real-HTTP SQLite test,
without the coordinator, reproduced this: rotation one succeeds and two hangs.
GitHub offline authentication stores and requires this connector data too.

The patch moves the stored-data read before the rotation transaction. Actual
upstream connector refresh remains inside the callback after current-token
validation. Read errors are surfaced only if the callback needs fresh identity,
preserving reuse/no-refresh behavior. Rotation, client binding, fresh connector
validation, and session configuration are unchanged. Separate clients sharing
one rotating upstream identity are not made transactionally coordinated by this
narrow fix.

## Acceptance coverage

Both memory and SQLite run with sessions disabled and strict rotation (zero reuse
interval), synthetic users, and a synthetic connector through real loopback HTTP:

- 16 refreshes from alternating/concurrent stale-reference consumers
- Durable authority close/reopen between refresh phases
- Direct original-bearer replay rejected with invalid_grant
- An injected lost-response error after an actual successful Dex rotation
- No additional HTTP call after that uncertainty, including after restart
- Independent SQLite regression: four raw-token HTTP rotations without authority

The lost-response test injects the client's error after receipt; it does not
simulate packet loss. The harness does not exercise live GitHub, the full
Pomerium browser/MCP integration, or production deployment. Separate acceptance
is required for those surfaces. The normal Dex server suite also runs, covering
its existing client/scope/rotation/session checks.
