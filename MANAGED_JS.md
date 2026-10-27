<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Managed JavaScript plugins with independent embedded Rego

This is an **experimental opt-in extension of WeeChat's existing V8 JavaScript plugin**, built
with a checked-in downstream patch in **this repository**. Nix applies that patch
to pinned WeeChat source and links an independent, embedded Regorus evaluator.
There is no mcp-js evaluator dependency, remote OPA service or new upstream fork.

The current default Docker build remains the stock runtime. The opt-in build pins
`r33drichards/weechat@b457688b41e429372a3dac64efd2534fdf4b2f73`, which identifies
as **5.0.0-dev**, because the modern V8 port requires V8 ≥11.3 and C++20. This is
an explicit package upgrade, **not** a claim that stock WeeChat 4.6.3 merely gains
a configuration option. All native plugins in the opt-in image come from the same
build; mixing their ABI with stock `.so` files is unsupported. Nothing here deploys
or upgrades an existing service.

## Trust boundaries

| Existing trusted plugins | Agent-managed plugins |
| --- | --- |
| Existing loader, legacy V8 isolate and APIs retained | Separate isolate per active generation |
| Operator-controlled source and configuration | Only named source files under a dedicated writable root |
| Native process, configuration and command APIs remain available | Typed bindings and bounded timers; default-deny native policy before effects |
| Not subject to the managed policy | Cannot modify policy, claim trusted identity or call the legacy API |

**No global no-exec/seccomp or Landlock filter is installed.** That would change
trusted plugins' capabilities and conflict with mixed-trust operation. Trusted
plugins can still execute programs. Managed scripts cannot request an executable,
read a file, invoke arbitrary commands, emit signals, manipulate configuration,
load another plugin or obtain raw WeeChat pointers through their exposed API.
They also cannot promote their source through the legacy file loader; the managed
root, ambiguous file identities and unsafe hard-link cases are rejected there.

Native context records, not JavaScript globals, registration names or supplied
claims, determine managed identity and generation. Names are audit labels, **not
extra authority**; the supplied policy applies a uniform operator-defined ceiling
to the entire managed root. Per-name privilege grants are not implemented.

The connected mcp-js/client must use `WEECHAT_MCP_FULL_CONTROL=0`. Otherwise its
existing unrestricted MCP command tools could bypass the managed workflow. This
limits that MCP endpoint, not the operator's trusted plugins or WeeChat console.
Existing relay, MCP Events authorization and Jev routing are unchanged.

## Nix builds the Docker image

```sh
nix build .#managed-js-image -o result-image
docker load --input result-image
# Optional Dockerfile compatibility route uses the same Nix runtime package:
docker build --target managed-js -t weechat-mcp:managed-compat .
# Normal/default Dockerfile image remains the stock build:
docker build -t weechat-mcp:stock .
```

`managed-js-image` is an actual `dockerTools.buildLayeredImage` image tarball,
including the MCP bridge environment from `uv.lock`; it is not just a native
runtime closure. Nix applies the checked-in WeeChat patch and builds matching
plugins. The separate `weechat-managed-container` output is that runtime closure
for packaging reuse, not an image.

Operator policy and grant data must be readonly regular files outside the managed
root. The image defaults to **deny-all data**. To try the example, review
[`policy/managed.rego`](policy/managed.rego) and
[`policy/example-data.json`](policy/example-data.json), which allows the selected
channel plus GET to `example.com`, then mount the reviewed data readonly:

```sh
sudo chown 0:0 /absolute/operator/managed-data.json
chmod 0444 /absolute/operator/managed-data.json
docker run --rm -i --memory=512m --pids-limit=128 \
  --mount type=bind,src=/absolute/operator/managed-data.json,dst=/operator-data.json,readonly \
  -e WEECHAT_MANAGED_DATA=/operator-data.json \
  -e WEECHAT_MCP_MANAGED_JS=1 -e WEECHAT_MCP_FULL_CONTROL=0 \
  -e IRC_HOST=irc.example.org -e IRC_CHANNELS='#wchat' \
  weechat-mcp:managed-js-nix
```

Relevant paths are configured once through `WEECHAT_MANAGED_ROOT`,
`WEECHAT_MANAGED_POLICY` and `WEECHAT_MANAGED_DATA`. Do not put policy, trusted
scripts, autoload directories, WeeChat profiles, credentials or deployment
configuration inside the agent-writable source root. The Nix image defaults to
`/home/weechat/managed-js` for sources and canonical immutable Nix store paths
for policy/data. The Dockerfile compatibility image uses
`/etc/weechat-managed/{managed.rego,data.json}`. An operator may explicitly mount
reviewed readonly policy/data at a canonical, nonsymlink path and set the matching
environment variable, as above; managed clients cannot do this themselves.
Bind mounts retain their host ownership: policy/data must be owned by root or
the service UID (10001 in this image), not merely readable by that UID. With
rootless Docker/user namespaces, account for the corresponding UID mapping.

A NixOS module is also available:

```nix
services.weechat-managed-js = {
  enable = true;
  server = "irc.example.org";
  channels = [ "#wchat" ];
  policyData.managed = {
    buffers = [ "irc.managed.#wchat" ];
    http_methods = [ "GET" ];
    http_hosts = [ "example.com" ];
    log = false;
  };
};
```

The module hosts WeeChat/private IPC, not a public FastMCP endpoint. It stores
policy/data immutably in the Nix store and sources separately under
`/var/lib/weechat-managed-js/sources`. It uses an isolated service profile and
whole-service memory/task ceilings. Existing trusted scripts may be explicitly
loaded through operator-only startup configuration.

## Scoped edit, validate and hot-activate

The new tools are present only when managed mode successfully initializes:

| Tool | Arguments / result |
| --- | --- |
| `weechat_managed_script_status` | `{}` → available names and native generations |
| `weechat_managed_script_read` | `{name}` → source and SHA-256 digest |
| `weechat_managed_script_write` | `{name, source, expected_digest}` → new digest; no activation. Empty digest creates a new file. |
| `weechat_managed_script_validate` | `{name, digest}` → stage/validate without replacing active generation |
| `weechat_managed_script_activate` | `{name, digest}` → validate then atomically hot-swap, or keep last good |

A name starts with an ASCII letter/digit and contains at most 48 letters, digits,
underscores or hyphens. It maps only to `<name>.js` under the held directory file
descriptor. No arbitrary paths, symlinks, hard-linked sources, special files,
NULs, invalid UTF-8 or sources over 64 KiB are accepted. The root must be private;
MCP-created sources are mode 0600. Source writes are atomic and digest-checked.
Outside filesystem writers must coordinate edits: this is not a cross-process
compare-and-swap protocol. At most 128 directory entries may be created by the
write tool; at most eight managed contexts can be active.

Use the returned digest for validate/activate. Activation evaluates the exact
source snapshot checked by the tool, not a later filename lookup. Merely writing
a file does not activate it. Candidate evaluation cannot emit effects. A syntax
error, missing handler or timeout preserves the old generation. Candidate starts
are globally limited to one per 250 ms; a `candidate_rate_limited` result has no
effect and may be retried after the indicated interval with the same digest. Successful
activation resets globals and revokes old callbacks/requests. An already-sent
HTTP mutation may still have happened; cancellation does not roll it back.

An mcp-js filesystem route can alternatively be granted **only this source
root**, with its own existing filesystem policy; then the client calls the narrow
validate/activate tools. Such a route must independently preserve no-follow,
no-hardlink mutation and bounded-file-count protections; a plain path-prefix
allowlist alone is not enough. If it cannot, use the named write tool instead.
No running mcp-js permission, mount or credential is
changed by this PR. There is no policy-admin, promotion, arbitrary plugin reload,
`/javascript load`, `/script reload`, generic eval or arbitrary command tool in
this managed interface.

## Script API

See [`examples/managed-bot.js`](examples/managed-bot.js). A candidate must register
exactly one handler while staging:

```js
weechat.onMessage(({ buffer, nick, text }) => {
  if (text === "!ping") weechat.send(buffer, "pong");
});
```

The exposed `weechat` host object is:

- `onMessage(function)` — receives data from exposed native IRC **channel**
  `irc_privmsg` lines, excluding own messages; policy also gates event intake.
- `send(buffer, text)` — queues at most 350 UTF-8 bytes of single-line literal
  IRC input, rechecks current channel scope and policy before dispatch. Slash
  text stays text, including when IRC `send_unknown_commands` is enabled; no
  `/msg` alias or generic command path is invoked. The trusted adapter uses a
  fixed `irc_input_send` signal with `user_message`, not a script-accessible signal
  API. Ambiguous metadata/framing and one-character server aliases are rejected.
- `http(method, url, body, callback)` — asynchronous GET/POST/PUT/DELETE; returns
  an opaque request ID. Completion is `{id, result: {status, body, headers}}` or
  `{id, error, outcome}`. No promises of binary/NUL-preserving response fidelity.
- `log(text)` — bounded policy-controlled diagnostic, no arbitrary buffer pointer.

Bindings queue intent during evaluation. Effects run only after successful
execution, the native watchdog has joined, and the independent policy permits
the final normalized arguments. A queued send is not delivery confirmation.
Failure discards that turn's queued effects. Callbacks execute on WeeChat's event
loop; neither Node.js globals nor full WeeChat APIs are installed in managed
isolates. SharedArrayBuffer, Atomics and WebAssembly are also removed from the
managed globals; this initial API does not support their blocking/shared-memory
paths. Trusted native hooks/modifiers remain trusted and can still affect IRC
processing; this does not make intentionally privileged trusted plugins safe.

## Managed timers

Managed isolates also expose four globals:

- `setTimeout(callback, delayMs)` — schedule one callback and return an opaque
  string handle.
- `setInterval(callback, intervalMs)` — schedule repeated callbacks and return
  an opaque string handle.
- `clearTimeout(handle)` and `clearInterval(handle)` — cancel either kind of
  timer owned by this generation. Clearing an unknown, completed or already
  cleared string handle is a no-op; handles cannot cancel another generation's
  timers.

Callbacks must be functions; delays must be integer numbers between **100 and
2,147,483,647 milliseconds**, inclusive. There is no implicit string/numeric
coercion, omitted-delay default, extra callback-argument forwarding or string
code evaluation. Each generation may hold at most **eight live timers**, with at
most **16 timer registration/cancellation intents per turn**. Clear operations require one string
handle of at most 48 UTF-8 bytes without NUL characters.
These are not Node.js timer objects: there is no `ref`, `unref` or Promise-based
sleep API.

```js
const heartbeat = setInterval(() => {
  weechat.send("irc.managed.#wchat", "heartbeat");
}, 60_000);

weechat.onMessage(({ text }) => {
  if (text === "!stop-heartbeat") clearInterval(heartbeat);
});
```

Timer registration is allowed during candidate evaluation, but it installs no
native hook until successful activation. Validation alone never starts timers;
a rejected candidate leaves the running generation unchanged. Registrations
and cancellations made during an active callback are committed only if that
turn succeeds; a failed turn discards its timer changes along with its queued
effects. A successful hot-swap cancels the old generation's native hooks and
queued timer callbacks. Unload or deadline retirement also cancels them; stale
callbacks never acquire the replacement generation's identity.

Timers use native WeeChat `hook_timer` with no second alignment, `max_calls=1`
for a timeout and `max_calls=0` for an interval. Native hooks enqueue work;
they do not run JavaScript reentrantly. Each timer has at most one queued
callback, so repeated native ticks while it is pending coalesce rather than
creating an unbounded backlog. Timer callbacks share the existing dispatch
queue, per-generation callback rate and **50 ms execution deadline**, including
microtasks. A timer does not grant send, HTTP or logging permission: effects
still cross the same independent policy checks and resource limits.

Delays are scheduling requests, not precise deadlines or delivery guarantees.
WeeChat runs timers on its event loop and may catch up missed native intervals
on later loop iterations; it can also reinitialize timers after a detected
system-clock jump. The managed queue can delay or coalesce callbacks further.
These timers are **in-memory only**, not durable cron jobs: process restart,
unload or script replacement loses them and reload evaluates the new script
from scratch.

The pinned implementation details are in
[`hook_timer_exec`](https://github.com/r33drichards/weechat/blob/b457688b41e429372a3dac64efd2534fdf4b2f73/src/core/hook/hook-timer.c)
and
[`unhook`](https://github.com/r33drichards/weechat/blob/b457688b41e429372a3dac64efd2534fdf4b2f73/src/core/core-hook.c).

## Independent policy and public-only HTTP

`policy/` links the standalone Regorus **0.5.0** crate through a small C ABI, not
mcp-js code. Policy/data load once, outside agent-writable paths. Only an actual
boolean `true` at `data.weechat.managed.allow` permits an operation. Missing,
malformed, undefined, false or error decisions deny. No query/data mutation or
policy reload API is exposed. Native code supplies the principal and known
operation; unknown operations remain denied even under an overly permissive rule.
The policy mutex is released before entering WeeChat/reentrant host callbacks.

HTTP uses native **`hook_url`**, with a fixed trusted `hook_process("func:...")`
DNS helper, not a separate broker. All DNS answers must be public unicast; mixed
public/private results fail. One approved address is pinned while preserving the
hostname for Host/TLS. Canonical URLs reach policy and dispatch unchanged;
ambiguous dot-segment/encoded-separator paths are rejected. Curl path normalization
is disabled. The wrapper disables redirects, proxies, ambient credentials and
content decompression. No raw curl options, arbitrary headers, file paths or Unix
sockets reach managed code.

Limits include two HTTP requests per context and two active native transfers
across the adapter, 2 KiB URLs, 16 KiB bodies, a 64 KiB streamed response limit and
10-second combined DNS/transfer deadline. Loaded libcurl ≥8.4 and all required
options are mandatory. Native headers have libcurl's own accumulation ceiling;
returned headers over 32 KiB are rejected. Binary data, invalid UTF-8 and known
length mismatches are rejected, but native C-string callbacks cannot preserve
embedded NUL bytes reliably. This is a text API.

No automatic retries. A mutation failure after dispatch is **`uncertain`**;
`not_sent` means no native HTTP dispatch, and a read failure is `failed`.
Redirect status is returned without following it. Public-destination checks do
not control what a remote server subsequently proxies or how it interprets URLs.
The public-only restriction belongs to this HTTP wrapper, not trusted WeeChat's
IRC or other network operations.

## Resource limits and honest scope

Each managed generation has its own isolate; a temporary native watchdog calls
V8 termination after 50 ms, including Promise microtasks. It joins before any
WeeChat operation or native DNS fork. A timed-out generation is retired, not the
legacy trusted isolate. Native dispatch runs one managed turn per timer tick;
queues, effects and per-second callbacks/sends/logs are bounded.
This is an interrupt deadline for managed JavaScript, not a complete wall-clock
bound on native allocation/GC, policy evaluation, or an engine bug.

**This is still in-process capability isolation, not an OS security boundary.**
Native V8/WeeChat/evaluator bugs, process OOM and allocator aborts can affect
trusted plugins. There is no independent process memory or credential boundary.
Use whole-service resource limits and maintain the pinned runtime.

Regorus 0.5.0 has **no hard evaluation fuel/deadline API**. Policy is trusted
operator code; input/source/JSON limits do not bound a pathological policy's CPU
cost. The V8 watchdog does not interrupt Rust/native policy evaluation. See
[`policy/README.md`](policy/README.md) for the exact limitations.

## Verification

```sh
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check --error-on-warning
uv run --locked python -m unittest discover -s . -p 'test_*.py'
cargo test --locked --manifest-path policy/Cargo.toml
nix flake check --no-build --all-systems
nix build --print-build-logs .#weechat-managed-container
nix build --print-build-logs .#checks.x86_64-linux.managed-js-vm
nix build --print-build-logs .#managed-js-image -o result-image
docker load --input result-image
python3 docker_managed_js_test.py --skip-build
```

CI builds the actual patched package and runs a NixOS VM with real WeeChat,
trusted and managed plugins, IRC reconnect, policy/API denial, independent
isolate timeout/reload, confined source management, and actual native HTTP/TLS.
Synthetic public-address origins remain inside the isolated VM; no policy bypass
or real Internet service is needed. Separate Docker smoke checks load and run the
actual Nix-built image. Native harness tests use real V8/Regorus but a fake
WeeChat transport and are explicitly **not** a substitute for the VM/image run.
Check results for the specific commit; written/evaluated tests are not a claim
that the actual VM or Docker image has executed successfully.
