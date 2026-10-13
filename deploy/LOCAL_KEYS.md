# Opt-in volume-local Pomerium keys

This mode is off by default. Activation is a separate deployment decision; running
unit tests or building the image does not create production credentials.

Set `POMERIUM_LOCAL_KEYS=true` only on the Pomerium service, with its own persistent
`/data` volume and the default image entrypoint. On a fresh volume, after the root
initializer has verified storage and permanently dropped every UID/GID to 10001
and cleared supplementary groups, startup generates two separate 32-byte CSPRNG
values, base64-encoded as `SHARED_SECRET` and `COOKIE_SECRET`.

The bundle lives at `/data/pomerium-keys/keys.json`. The directory is 0700; the
bundle and stable `.lock` file are 0600, owned by UID/GID 10001. A no-follow advisory
lock serializes first starts. Startup writes a private temporary file, fsyncs it,
atomically renames it and fsyncs the directory. Every restart rechecks ownership,
exact modes, file types, links, bundle schema and key encoding. Invalid state,
unexpected files (including interrupted temporary writes), unavailable storage or
unsafe links stop startup without repairing or replacing keys. No key values are
printed. Only the Pomerium child receives the validated values in its environment;
no exchange with Dex, the bridge, CI or another service is performed.

Do not configure `SHARED_SECRET`, `COOKIE_SECRET` or either `_FILE` input when
initializing this mode. Existing explicit keys remain supported with local mode
off. After a bundle exists, matching explicit shared/cookie values are accepted;
mismatches stop startup rather than silently overriding either value. Local mode
also rejects `SIGNING_KEY`, `SIGNING_KEY_FILE`, `IDP_CLIENT_SECRET` and
`IDP_CLIENT_SECRET_FILE`; the deployment uses a public OIDC client with PKCE.

No separate signing key is needed: pinned Pomerium 0.33.3 derives a deterministic
P-256 signing key from the persisted shared secret when no signing override is
present. This is verified in upstream commit
[`76042ed40db3bab9506b689f436716b6df624f23`](https://github.com/pomerium/pomerium/blob/76042ed40db3bab9506b689f436716b6df624f23/config/options.go#L1453-L1500).

Back up the keys and `/data/databroker` together through an approved private backup
process. If the bundle is missing while any databroker entry exists, startup fails
and asks for the matching verified backup; it never silently generates replacement
keys beside existing state. Loss of the whole volume means new keys and lost
OAuth state: old sessions, cookies and tokens may stop working, and clients must
reauthenticate or register again. Restoring a matching backup preserves key
identity. Do not manually delete key files as a rotation procedure, share the
volume with another service, or use this single-volume design for multi-replica
key distribution.

`POMERIUM_LOCAL_KEYS=false` (or omitting it) leaves the existing environment-based
behavior unchanged and never touches key storage. Other values are rejected.
Synthetic unit coverage runs in the normal `test_*.py` suite and includes restart,
parallel initialization, permission/link corruption, missing-key state, atomic
write failures, explicit-input conflicts and credential-free diagnostics. Real
container and live OAuth acceptance remain separate validation gates.
