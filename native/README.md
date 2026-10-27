<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Native managed lane

`weechat-js-managed.cpp` is compiled **into the existing WeeChat JavaScript
plugin**, alongside the fork's modern V8 integration. The checked-in downstream
patch adds loader/init/end hooks and prevents full legacy API wrappers from being
called from non-legacy isolates. Nix applies this patch to the pinned source and
links the independent Regorus static library. There is no Node.js environment,
standalone evaluator process or mcp-js implementation dependency.

## Execution and identity

- Every managed generation owns a different V8 isolate/context from the legacy
  trusted isolate, but uses the existing single-threaded V8 platform.
- The only native bindings are `weechat.onMessage`, `send`, `http` and `log`.
  WebAssembly, shared-memory wait primitives, and deferred GC callbacks are not
  exposed in the managed context. Their `External` callback data is a
  native-owned generation record. JavaScript metadata, names, global properties
  and strings do not select a trust class.
- `onMessage(fn)` registers exactly once during source staging. Staging side
  effects throw; a failed candidate never replaces the existing generation.
- Native host callbacks queue intent only. No WeeChat calls occur while the
  deadline watchdog is alive. It calls only V8's thread-safe
  `TerminateExecution` on that managed isolate and is joined before effects are
  validated, authorized and dispatched on WeeChat's event loop.
- Microtasks and at most four foreground tasks are serviced under the same
  evaluation window, for this isolate only. Shutdown notifies the platform before
  disposing the isolate. A timeout retires only that managed generation and
  drops its pending callbacks; it does not terminate the legacy isolate.
- Dispatch retains its generation across synchronous trusted-plugin reentry.
  Unload immediately retires it, stops remaining effects and never calls foreign
  host code from its destructor. Bridge replies use core table helpers captured
  before reentry, not a potentially removed plugin object.

The 50 ms execution window is not a complete wall-clock resource guarantee for
native allocation/GC, policy evaluation or a buggy engine. Regorus evaluates
operator-owned policy; its interface bounds input sizes, not hostile policy
execution time. Memory exhaustion/native bugs remain process-wide failures.

## Host protocol

Trusted Python code confines named source files and calls the native info-hashtable
interfaces with a captured source snapshot. Managed JavaScript has no access to
these interfaces or to arbitrary WeeChat info/configuration/command APIs.

- `javascript_managed_validate` / `javascript_managed_activate`: input
  `name`, `source`, SHA-256 `digest`; result `ok=1`, `digest`, `generation`, or
  stable `error`. Names begin with an ASCII alphanumeric and contain at most 48
  ASCII alphanumerics, underscores or hyphens. Sources are at most 64 KiB.
- `javascript_managed_status`: result `ok=1`, `plugins` JSON.
- `javascript_managed_message`: trusted input `buffer`, `nick`, `text`; queued
  per generation and independently gated by `event.receive` policy.
- `javascript_managed_http_complete`: trusted input `id`, `response_json`.
  Pending native records determine the target generation and origin buffer.

Global candidate admission is one start per 250 ms, including failed candidates.
Excess validate/activate calls return `candidate_rate_limited` and
`retry_after_ms`, before creating a new isolate or generation. This prevents a
pipelined management request burst from multiplying the evaluation window.

At most eight contexts, 32 queued events/context, two pending HTTP calls/context
(and two across the Python HTTP controller), four effects/turn, and 20
callbacks/second/context are retained. The timer executes **one** context turn
per tick, round-robin. Sends and logs each have a four-per-second global ceiling.
`send()` returns `"queued"`, not delivery or authorization confirmation.

After the watchdog joins, native code calls `mcp_managed_http_validate`, gates
its normalized result through Regorus, then `mcp_managed_http_start`. Requests
use native `hook_url`; see the Python controller for DNS pinning/public-only
policy. HTTP callbacks receive `{id,result:{status,body,headers}}`, or
`{id,error,outcome}`. Denial before native dispatch reports `not_sent`. No retries.
A generation replacement drops local callbacks and queues best-effort transport
cancellation for the next timer turn. Full JavaScript-plugin unload only revokes
callbacks; an already dispatched transport may finish or expire in the trusted
Python controller. Neither replacement nor unload can undo an already sent
mutation.

## Trust-path defense

The ordinary trusted loader keeps its full existing API, but may not promote
agent-writable source. In managed mode, it opens a file once, classifies the
opened descriptor, and reads that same descriptor. Normal trusted symlinks remain
supported. Multiply linked files are checked against the managed tree by inode,
so legitimate Nix-store deduplicated files are not blanket-rejected. An alias of
a managed inode, or an ambiguous scan beyond 1,024 entries/eight levels, fails
closed. The managed directory descriptor also prevents an operator rename from
silently promoting its contents. Only the trusted operator controls the root,
policy/data paths and parent directories; managed code cannot modify those paths.

## Tests

`isolate-prototype.cpp` uses real V8 to verify independent isolate termination,
Promise-microtask deadlines, legacy liveness and watchdog join before fork.
`managed-test.cpp` links real V8 **and real Regorus**, with a fake WeeChat transport,
and covers policy/scope, narrow bindings, staged rejection, hot reload, stale
callbacks, request cleanup, queue/rate limits, trusted path aliases, timeout
recovery and synchronous trusted unload during effect dispatch. These tests do
not substitute for the actual WeeChat NixOS VM tests.

Build `policy/` first, then run with explicitly matched headers/library/ABI flags:

```sh
WEECHAT_SOURCE=/path/to/pinned/unpacked/weechat \
V8_CFLAGS="$(pkg-config --cflags v8)" \
V8_LIBS="$(pkg-config --libs v8)" \
  native/run-tests.sh
```

Use the ABI definitions selected by the Nix V8 compile/run probe. Do not force
incompatible pointer-compression or sandbox flags to make a build pass.
