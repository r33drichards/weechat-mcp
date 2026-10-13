# Local command mapping validation

Base: `bafb751c5eaa9c6d7b8f959000f70f9e3cef0ba7`, fetched from
`origin/docker-packaging` before creating the isolated feature worktree.

## Runtime coverage

The isolated official Debian WeeChat 4.6.3 runtime, with packaged plugins loaded,
registered **165 commands**. All 165 have distinct generated MCP tools, including
plugin-scoped duplicate names. The committed fixture records every registration
and its exact runtime metadata in `fixtures/runtime-commands-weechat-4.6.3.json`.

- 41 commands: all documented alternatives handled by the conservative compiler
- 31 commands: some documented alternatives typed, some raw
- 93 commands: raw-only, including empty/undocumented help
- All 165: transparent raw fallback available

“Typed” means structured string placeholders and literal enums, with an exact
serialization plan for the supported syntax. It does **not** mean fully modeled
C callback semantics or proof of every accepted input. Optional positional gaps
are rejected by serialization. Complex repetition, nested flags, server-dependent
mode grammar and callback-specific quoting remain explicit raw syntax.

Examples: IRC `server` exposes structured list/listfull, copy/rename, del, raw and
other simple alternatives. The compound hostname/port and arbitrary option syntax
for server creation remains raw. IRC mode semantics remain raw because supported
flags and their argument requirements depend on the connected server.

## Acceptance and regressions

The fresh-profile acceptance script validates every generated JSON Schema and
checks inventory equality. Read-only core `/help help` and IRC `/server list`
were dispatched through private IPC, with expected printed output verified through
the existing history API in an isolated core-buffer allowlist. MCP stdio `tools/list` returned the complete
mapping, and a generated typed `/server list` invocation was dispatched through the
existing FastMCP bridge and official MCP client. No external IRC server was
configured or contacted by this acceptance test.

The existing localhost IRC live integration also passed unchanged, including
history/resources and its pre-existing subscription/runner regressions. Those
features were not extended by this implementation.

Unit tests exercise full fixture coverage, plugin load/unload, shadowed command
names, buffer context separation, metadata/version drift, source evidence,
unknown grammar fallback, control-character rejection, typed quoting rejection,
verbatim raw serialization, unexpected arguments and stale schema rejection.

## Source confidence and limitations

Source extraction is a balanced C lexical registration parser with explicit macro
resolvers and bounded callback evidence. It is **not a C compiler/AST semantic
analyzer**. Dynamic expressions, unresolved macros, conditional compilation and
callback logic that cannot be established remain evidence rather than inferred
strong types. Runtime inventory is authoritative for which commands are loaded.
The optional source manifest records revision/content provenance and flags syntax
drift; it does not silently replace runtime metadata with a different source tree.

The source checkout scanned 506 C/H files at
`b457688b41e429372a3dac64efd2534fdf4b2f73`: 137 registration sites, including
133 static names and four dynamic sites; 135 callback definitions were located.
Comparing this checkout to the installed WeeChat 4.6.3 runtime found 125 exact
argument-syntax matches, seven drifts/unresolved matches (including `server`),
and 33 runtime-only registrations. These are different versions; matching a help
string alone is not a version or behavioral equivalence guarantee. Source-enriched
runtime acceptance also passed.

Only local dispatch plus expected read-only fixture output is tested. The explicit
plugin `/command` wrapper discards target callback return codes, so generated
results deliberately report dispatch only, with target callback status unavailable. Destructive commands, shell execution,
configuration changes and arbitrary plugin callbacks were not exercised. Typed
metadata is not an authorization boundary. Full-control mode stays disabled by
default and does not broaden history/resource access. Publication, public
full-control access, and deployment are outside this local slice.

Existing bounded IPC frames/queues still apply. Very large custom plugin
registries may require a separate pagination/size-budget extension. Unusual hook
identities containing whitespace, quotes, backslashes, control characters, or a
leading dash are discoverable but rejected for dispatch rather than interpolated
unsafely. No such identity occurs in the validated 165-command fixture.

Final local checks: **147 unit tests passed**, Ruff check passed, Ruff formatting
passed, and ty with `--error-on-warning` passed. The existing localhost integration
printed `ALL_LIVE_CHECKS_PASS`. The isolated generated-command acceptance passed
both without source data and with the extracted source manifest attached.
A separate focused review found the dispatcher acknowledgement issue described
above; it was fixed and the 26 manifest/source tests were rerun successfully.
Docker image build/runtime was not rerun for this slice; the Dockerfile includes
the new dependency-free compiler module and container unit checks are in the suite.
