<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# WeeChat MCP

A FastMCP server for scoped WeeChat commands and live buffer resources. WeeChat
owns IRC; the MCP server runs in a separate Python process. All WeeChat API calls
stay on WeeChat's event loop.

## Architecture

- `weechat_mcp.py`: dependency-free WeeChat plugin. Exposes allowlisted buffers,
  safe command adapters and a 200-line history ring over private local IPC.
- `bridge.py`: FastMCP 4.1.0 server. Dynamic providers discover available commands
  and buffers; the MCP SDK handles transport, validation, lifecycle and dispatch.
- `client.py`: small host facade over the official MCP SDK client, explicitly using
  protocol **2026-07-28** and acknowledged `subscriptions/listen` streams.
- `runner.py`: deterministic mention-trigger experiment with a durable SQLite
  inbox, deduplication, busy queue and turn limits. It does not invoke a model.

FastMCP exposes tools/resources at a high level. Its underlying MCP SDK 2.3.0
provides `ListenHandler` and `InMemorySubscriptionBus`, which handle stream
acknowledgment, request correlation, filtering, bounded buffering and cancellation.
One isolated access to `server._mcp_server.add_request_handler` registers that SDK
handler; FastMCP currently has no high-level subscription registration API.
Dependencies are pinned, and subscription tests must pass before upgrades.
There is no project-owned MCP dispatcher, handshake or compatibility fallback.

## Run

Requirements: WeeChat with its Python plugin, Python 3.10+ for the external server,
Unix sockets, and permitted DNS/TCP access to your IRC server. Use an isolated
WeeChat profile rather than your normal private-message history.

Install the external server dependencies:

```sh
uv sync --locked
```

WeeChat's embedded Python needs only the standard library. Load the plugin:

```
/python load /path/to/weechat-mcp/weechat_mcp.py
/set plugins.var.python.mcp.buffers irc.railway.#wchat
```

Configure your server locally in WeeChat, using alias `railway` (or update the
buffer allowlist), connect and join `#wchat`. Verify TLS before using credentials.
The plugin neither requires nor exports credentials.

Configure your MCP host:

```json
{"mcpServers":{"weechat":{"command":"/path/to/weechat-mcp/.venv/bin/python","args":["/path/to/weechat-mcp/bridge.py","/path/to/weechat-data/mcp/bridge.sock"]}}}
```

The plugin socket is private IPC, not an MCP endpoint. Hosts launch `bridge.py`.
The directory is mode 0700 and socket 0600. Existing sockets are never replaced;
after a crash, verify no process owns a stale socket before removing it.
Never expose this endpoint to untrusted users.

## Tools and resources

`weechat_buffers` lists exposed buffers. `weechat_command_catalog` discovers
registered slash commands and marks which have execution adapters. The scoped
adapters are `msg`, `say`, `me`, `names`, `who` and `topic`, when registered by the
installed WeeChat version (`say` is absent in WeeChat 4.6.3).

```json
{"name":"weechat_msg","arguments":{"buffer":"irc.railway.#wchat","arguments":"hello"}}
```

Arguments cannot override the selected channel. Control characters and multiline
commands are rejected. Arbitrary `exec`, scripting, server configuration and
secret-bearing commands are not exposed. A successful result means local command
acceptance, not delivery confirmation. Add typed scoped adapters for new commands
rather than a generic command escape hatch.

`resources/list` discovers allowed buffers. `resources/read` returns their most
recent 200 printed lines since plugin load, including plugin epoch, monotonic
sequence and eviction watermark. The SDK's `subscriptions/listen` acknowledges
requested resource URIs before delivering correlated resource-change events.
Clients reread the resource after an event. Closing the listen context cancels
that stream; reconnecting requires a fresh listen and catch-up read.

For separate IRC identities, run separate WeeChat profiles and nicknames. Multiple
MCP connections to one profile share that profile's IRC identity. A host must
consume notifications and explicitly schedule turns; MCP does not wake a model
on its own. Incoming IRC content is untrusted data, never tool authorization.

## Mention-triggered host experiment

The live test uses two distinct WeeChat profiles and deterministic host callbacks:

1. An independent observer mentions A; A queues the message and sends B a question.
2. B wakes on the mention. While B is busy, it disconnects its MCP client and an
   additional mention arrives; reconnect and catch-up recover the pending line.
3. B replies and A wakes. Duplicate resource events do not enqueue old messages.
4. B's remaining message stays queued because its turn limit is exhausted.

SQLite cursor advancement and enqueueing are atomic. Message identity is plugin
epoch plus sequence. Own messages, non-IRC lines and nickname substrings are
ignored. History gaps fail closed. Intake continues while a turn runs. An uncertain
running turn stays blocked after a crash for reconciliation rather than resending.
The SQLite file must be private to each agent/buffer. History in WeeChat is bounded
and in-memory, so reconnect catch-up cannot recover evicted or pre-restart lines.
There is no exactly-once network delivery guarantee and no automatic command retry.

This is a host scheduling/transport experiment, not an autonomous model runtime.
No model credentials, model calls or new spending are involved.

## Verify

```sh
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run python -m unittest discover -s . -v
uv run python -m py_compile *.py
git diff --check
```

The real integration runs official Debian WeeChat 4.6.3 and miniircd on localhost:

```sh
uv run python live_test.py --weechat-root /tmp/weechat-runtime/root --miniircd /tmp/weechat-miniircd/miniircd
```

The root contains official Debian `weechat-headless`, `weechat-core`,
`weechat-plugins` and `weechat-python` packages extracted with `dpkg-deb -x`, with
compatible shared libraries installed on the host. CI installs WeeChat packages
from Ubuntu and checks out a pinned commit of official miniircd.

The test creates temporary profiles and an ephemeral local IRC server, verifies
modern MCP resource events, cancellation, send/receive, two separate identities and
the bounded host experiment, then stops all processes. It never joins an external
IRC server or deploys a public listener.

For isolated environments without Unix sockets, set `WEECHAT_MCP_LOOPBACK_PORT=0`.
The plugin binds only 127.0.0.1 and writes the port to `<data dir>/mcp/loopback-port`.
Launch `bridge.py --loopback-port PORT`. This test mode has no authentication:
never use it in an untrusted network namespace, forward it, or expose private
accounts. The default Unix socket uses filesystem access controls.

## Sources

- [FastMCP resources](https://gofastmcp.com/servers/resources)
- [FastMCP providers](https://gofastmcp.com/servers/providers)
- [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
- [WeeChat API](https://weechat.org/files/doc/devel/weechat_plugin_api.en.html)
- [Debian WeeChat packages](https://deb.debian.org/debian/pool/main/w/weechat/)
- [miniircd](https://github.com/jrosdahl/miniircd)

## License

GPL-3.0-or-later; see [LICENSE](LICENSE). Developed in the
[r33drichards WeeChat fork](https://github.com/r33drichards/weechat), based on
upstream [WeeChat](https://github.com/weechat/weechat), maintained by Sébastien
Helleu and contributors. WeeChat and miniircd are separate runtime dependencies
with their own notices and licenses.
