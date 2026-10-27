<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Independent embedded policy evaluator

This small native library embeds the standalone **Regorus 0.5.0** Rego evaluator
inside WeeChat. It does not link to, call, import or depend on the mcp-js evaluator,
configuration, service or policy hooks. The implementation is independent; the
shared pattern is synchronous local policy evaluation before native effects.

`Cargo.lock` freezes dependencies. `regorus` disables default/full-OPA features;
only `std` and `arc` are enabled. No host extensions, remote policy service,
network policy builtins or subprocess evaluator are registered. Third-party
Regorus is MIT-licensed; this adapter follows the repository's GPL-3.0-or-later
license.

## Native contract

Include `include/weechat_managed_policy.h` and link `libweechat_managed_policy.a`.
The API only loads an immutable source/data pair, authorizes one canonical input,
and frees the native handle. There is **no policy mutation, arbitrary query,
trusted-mode promotion or data update API**.

```c
policy = weechat_managed_policy_load(rego, rego_len, data, data_len);
if (!policy) { /* protected startup fails */ }
int decision = weechat_managed_policy_authorize(policy, input, input_len);
if (decision != WEECHAT_MANAGED_POLICY_ALLOW) { /* do not perform effect */ }
/* The evaluator lock has been released. Now perform the authorized effect. */
```

The fixed query is `data.weechat.managed.allow`. **Only literal Rego boolean
`true` allows.** Successful evaluation yielding `false`, undefined, a string,
number, list or object denies. Parse/evaluation errors, a busy/poisoned evaluator,
invalid input and Rust panics return an error, which callers must also deny.
A missing rule, empty policy, wrong package or malformed source/data fails load.
Data cannot inject the reserved `weechat` policy namespace.

The caller must obtain policy/data from operator-owned paths outside every
managed-script writable root, and derive the principal from actual native V8
context metadata. The C ABI cannot authenticate a hostile native caller that
fabricates an identity; it must never be exposed as a JavaScript binding or MCP
policy-admin tool. An opaque handle is not a user-provided value.

## Canonical input

```json
{
  "principal": {"id": "example", "generation": 1, "trust": "managed"},
  "phase": "active",
  "operation": "irc.send",
  "resource": {"buffer": "irc.docker.#wchat"},
  "request": {"text": "hello"}
}
```

The five top-level keys and three principal keys are exact. IDs are 1–64 ASCII
letters/digits/dots/underscores/hyphens starting with a letter or digit;
generation is a positive unsigned integer. Resource/request are bounded objects
constructed from final validated effect arguments, not caller-provided authority
claims. The initial known operations are:

- `event.receive`: resource buffer; request nick/text.
- `irc.send`: resource buffer; request text.
- `http.request`: originating resource buffer; request normalized method/URL,
  scheme, host, integer port and body.
- `log`: resource context; request text.

Any other operation is denied **even by an `allow := true` policy**. Non-managed
trust classes and all non-active phases are denied. Trusted legacy plugins bypass
this managed API in the native integration; they do not ask Rego to become trusted.

[`managed.rego`](managed.rego) is a default-deny example with uniform managed-lane
buffer, method and hostname ceilings in [`example-data.json`](example-data.json).
Script names/generations are audit labels, never extra caller-selected authority.
Per-script grants would require a separate immutable operator registry constraining
which source identity may use which grant; that feature is not implemented here.
The example never allows a generic WeeChat command, script loader, signal, pointer,
configuration write, filesystem or process operation. HTTP destination parsing,
public-address checks, DNS pinning, TLS, redirect/proxy controls and response
limits remain mandatory native wrapper checks: Rego hostname grants do not
replace those protections.

## Resource and failure boundaries

Source and data are each capped at 64 KiB; input is capped at 32 KiB. JSON has a
24-level depth ceiling, 4,096-node ceiling, 16 KiB string ceiling and integer-only
numbers. Rust panic unwinding is caught at the C boundary. Invalid native
pointers, use-after-free, double-free and allocator aborts are outside that
contract; no in-process evaluator can turn them into memory isolation.

**Regorus 0.5.0 has no public evaluation fuel, timeout or cancellation API.**
These byte/shape caps are not a hard evaluator CPU or memory limit. The supplied
policy uses bounded direct lookups and small membership tests, but an arbitrary
operator policy can be expensive or pathological. Operator Rego is trusted native
host configuration, not agent-authored code. A V8 script watchdog does not cancel
a synchronous Rust evaluation. If hard policy-evaluation deadlines are required,
this pin must gain a separately reviewed budget mechanism or be replaced with a
reviewed engine exposing one. Do not claim a post-return timer provides a bound.
Native failures/OOM can still affect WeeChat and trusted plugins.

## Build and verify

```sh
cargo test --locked --manifest-path policy/Cargo.toml
cargo clippy --locked --manifest-path policy/Cargo.toml --all-targets -- -D warnings
cargo build --locked --release --manifest-path policy/Cargo.toml
cc -std=c11 -Wall -Wextra -Werror -Ipolicy/include policy/tests/ffi_smoke.c \
  policy/target/release/libweechat_managed_policy.a -ldl -lpthread -lm \
  -o /tmp/weechat-policy-ffi-smoke
/tmp/weechat-policy-ffi-smoke
```

Tests exercise literal decision types, errors/undefined/missing policy, immutable
separate handles, schema/byte/node/depth bounds, unknown operations, staging/trust
rejection, no authority injection, busy/poisoned engines and the actual C ABI.
The native integration must separately test context identity, callback reentry,
policy-admin isolation, effect validation and mixed trusted/managed behavior.
