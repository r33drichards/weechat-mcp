# Opt-in managed JavaScript on NixOS

Nix is the **single authoritative patched build**. The pinned source is
`r33drichards/weechat@b457688b41e429372a3dac64efd2534fdf4b2f73`, which reports
**WeeChat 5.0.0-dev**. This is an explicit opt-in upgrade, not JavaScript support
silently added to stock WeeChat 4.6.3/4.10.1. The ordinary Docker target remains
unchanged.

The build applies the checked-in downstream patch in `vendor/weechat/patches`,
adds the managed native implementation, and links the independent Rust/Rego
static library from `policy/`. It uses nixpkgs' V8 monolith from maintained Node.js 22,
without exposing the Node.js application runtime. Upstream `FindV8` must actually
compile, link and run its ABI-definition probe; no forced definitions bypass it.
An additional shared-module link check rejects non-PIC/TLS-incompatible archives
(the current Node.js 24 archive passes an executable probe but cannot link into
`javascript.so`). The immutable flake lock fixes source revisions and content hashes.

## Module

```nix
{
  imports = [ inputs.weechat-mcp.nixosModules.default ];
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
}
```

TLS and certificate verification are enabled by default. Empty default grants
deny all managed effects. Grants apply uniformly to the managed trust class:
agent-selected source names are audit/lifecycle identifiers, **not identities
that can acquire more privilege**. Native code binds the actual source name,
generation and trust class before independently evaluating Rego for each effect.

The module snapshots operator Rego and JSON into immutable Nix store files,
outside `/var/lib/weechat-managed-js/sources`. That private source-only directory
is the only filesystem surface exposed by the managed MCP tools. The fresh
WeeChat profile lives under `/run/weechat-managed-js/profile`; its local
`mcp/bridge.sock` is private IPC, not a public MCP endpoint. The module does not
create network ingress, a bridge deployment, a subscription, or an authorization
grant for additional clients.

Use the existing authenticated MCP connection to perform the named workflow:

1. `weechat_managed_script_write` with `name`, `source`, and `expected_digest`
   (empty only to create a new name).
2. `weechat_managed_script_validate` with that name and returned digest.
3. `weechat_managed_script_activate` with that same name and digest.
4. Inspect `weechat_managed_script_status` or reread with
   `weechat_managed_script_read`.

Writing and validating do not activate code. Invalid candidates keep the last
active generation; stale digests fail rather than silently overwriting changes.
Explicit activation is hot reload. Old-generation HTTP completions cannot invoke
a replacement's callbacks. Policy/trust data are not writable through this
workflow, and the trusted loader rejects agent-writable managed source paths.

Managed code has only `weechat.onMessage`, `send`, `http`, and `log`, not the
legacy WeeChat API or Node.js `process`/`require`. Sends are literal scoped text,
not slash-command dispatch. HTTP is the native `hook_url` transport with a
separate always-public network policy; an operator hostname grant cannot permit
private destinations. For example:

```javascript
weechat.onMessage(function (event) {
    if (event.text === "!hello") {
        weechat.send(event.buffer, "Hello, " + event.nick);
    }
});
```

## Mixed trust, not an OS sandbox

Legacy trusted JavaScript and native plugins retain their existing full APIs,
including process execution. There is **no process-wide no-exec filter,
Landlock restriction, or restrictive filesystem profile** silently changing
those plugins. `extraStartupCommands` is an operator-only way to load them.

Managed code receives a separate V8 isolate and bounded evaluation. This is a
language/API boundary in the same process, **not native-memory or confidentiality
isolation against a V8/native-code exploit**. A whole-service memory ceiling does
not guarantee recovery from every native allocation failure. Trusted plugins,
operator policy, and the native adapter remain privileged.

## Packages and verification

```sh
# Evaluate all supported derivations; this is not a build or VM run.
nix flake check --no-build --all-systems

# Independent Rego library, with its Rust tests.
nix build --no-link --print-build-logs .#managed-policy

# The authoritative patched fork.
nix build --no-link --print-build-logs .#weechat-managed

# Actual Docker image, including the same native build and uv.lock Python env.
nix build --print-build-logs .#managed-js-image
docker load < result

# Optional: inspect/build just the locked bridge Python environment.
nix build --no-link --print-build-logs .#managed-js-python

# Optional: native runtime closure with matching Python plugin/PYTHONHOME.
nix build --no-link --print-build-logs .#weechat-managed-container

# Actual x86-64 NixOS VM (KVM required).
nix build --no-link --print-build-logs .#checks.x86_64-linux.managed-js-vm
```

`managed-js-image` is a `dockerTools.buildLayeredImage` tarball tagged
`weechat-mcp:managed-js-nix`, not a Dockerfile that recompiles the fork. Its
Python environment is resolved from the existing `uv.lock` using pinned
uv2nix/pyproject-nix inputs; there is no networked installer at image startup.
It starts with immutable **deny-all managed grants**; operators must explicitly
provide their policy data before allowing managed effects. See
[`MANAGED_JS.md`](../MANAGED_JS.md) for configuration and the offline image smoke
test. Building an image does not load, start, or deploy it.

`weechat-managed-container` is only a runtime closure, not an image. It exposes
`bin/weechat-headless` and
`lib/weechat/plugins/{irc,python,javascript}.so`. Export its complete Nix runtime
closure; copying only the executable would omit matching runtime dependencies.

The VM uses real WeeChat, V8, independent Rego, native IRC and `hook_url`. All
HTTP/IRC traffic stays inside the VM. A globally classified fixture address is
assigned to its loopback; production address checks are not weakened. Coverage
includes:

- Named source write/read/digest validation/activation, failed/staging candidate
  preservation, hot reload, and stale callback suppression.
- Managed API and filesystem/policy boundaries; immutable grants; an existing
  ungranted IRC channel; policy-host denial; and trusted-loader promotion denial.
- Literal slash-prefixed sends despite a trusted `/msg` alias shadow.
- Native GET/POST/PUT/DELETE, bodies and original Host, ignored ambient proxies,
  blocked private targets, redirects not followed, rejected dot-segment paths,
  bounded chunked bodies, and responsiveness during pending HTTP.
- A VM-generated HTTPS certificate, successful hostname verification and a
  wrong-host certificate rejection. No private keys are committed.
- IRC reconnect, managed loop/microtask timeout, and trusted legacy callbacks
  and harmless external process execution before and after managed failures.

Nix evaluation, syntax checks and mocked policy tests are not substitutes for
running that VM. The dedicated CI workflow builds the same authoritative package
and executes the VM gate.
