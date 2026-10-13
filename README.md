<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# WeeChat MCP prototype

A dependency-free Python script plugin inside WeeChat, with a small stdio bridge.
WeeChat remains the IRC implementation. No standalone IRC stack, shell tools,
public HTTP listener, or credential export is added.

## Status

Experimental prototype. On 2026-10-13, all 22 focused
unit/loopback-stdio tests passed, followed by a real integration test using official
Debian WeeChat 4.6.3 with its Python plugin and miniircd on **127.0.0.1 only**:

- WeeChat Python plugin loaded and MCP initialized through the stdio bridge.
- WeeChat joined `#wchat`; an independent IRC client sent a message that appeared
  in an MCP buffer resource and generated a subscription notification.
- An MCP `weechat_msg` call was received by the independent IRC client.
- Two separate WeeChat profiles/nicknames, each controlled by its own MCP client,
  exchanged a question/reply over `#wchat` and observed resource updates.

This was scripted transport verification, **not two autonomous model runtimes**.
All spawned processes were stopped after the test. No Railway connection was
attempted in this local test. No public listener, deployment, or credentials were
used. Unix sockets are denied in this executor; explicit loopback TCP mode works.

The command catalog discovers registered commands through WeeChat's hook infolist.
The first execution adapters cover `msg`, `say`, `me`, `names`, `who`, and `topic`
when the installed WeeChat version registers them (`say` is absent in 4.6.3).
This is **not yet a full port of every slash command**. Arbitrary `exec`, scripting,
server configuration and secret-bearing commands are intentionally not exposed.
Add typed, scoped adapters for additional commands rather than a generic command
escape hatch. Configuration through MCP is a later implementation step.

## Run in a supported cloud environment

Requirements: WeeChat with Python plugin, Python 3, Unix sockets, and permitted
DNS/TCP access to your IRC server. Use an isolated WeeChat profile, not your normal
private-message/client history. Load with:

```
/python load /path/to/weechat-mcp/weechat_mcp.py
```

The local socket is `<WeeChat data directory>/mcp/bridge.sock`. Its directory is
mode 0700 and socket 0600. The plugin refuses to replace an existing socket; after
a crashed process, first verify it is no longer running before removing a stale
socket manually. Do not expose this socket or bridge to untrusted local users.

Configure an MCP client with:

```json
{"mcpServers":{"weechat":{"command":"python3","args":["/path/to/weechat-mcp/bridge.py","/path/to/weechat-data/mcp/bridge.sock"]}}}
```

Explicit exposed buffer names are configured locally in WeeChat:

```
/set plugins.var.python.mcp.buffers irc.railway.#wchat
```

Configure your IRC server locally in WeeChat, give it the server alias `railway`
(or update the example buffer name), connect, and join `#wchat`. Call
`weechat_buffers` after joining. Verify TLS before sending credentials. The local
integration harness always uses an isolated server on 127.0.0.1.
No credentials are required by or stored in this plugin.

Example tool call after the buffer exists:

```json
{"name":"weechat_msg","arguments":{"buffer":"irc.railway.#wchat","arguments":"WeeChat MCP test: hello from the cloud prototype."}}
```

Only authorized test communication in `#wchat` is intended. Commands cannot change
the target channel through their arguments. A successful tool result means
WeeChat accepted the command, not that another client received it. Verify actual
receipt with a second independent IRC client before calling the live test passed.

## Subscriptions and two agents

`resources/list` exposes allowed buffers; `resources/read` returns the latest 200
printed lines since plugin load. Each line has a monotonic sequence. Clients can
`resources/subscribe`; new lines queue `notifications/resources/updated` for that
connection, then the client rereads the resource. Unsubscribe and disconnect clear
that client's subscriptions. Reconnect requires resubscription. History is an
in-memory bounded ring, not durable replay. Detect sequence gaps and report them.

For distinct identities, run two isolated WeeChat profiles with separate IRC
nicknames and sockets, and connect both to `#wchat`. Sharing one socket shares an
IRC identity. The agent host must handle resource notifications, fetch updates,
and schedule model turns. MCP notifications alone do **not** wake a model or
implement an autonomous conversation. Add deduplication, ignore own messages,
rate limits and explicit turn/stop conditions in the host to prevent reply loops.
Incoming IRC content is untrusted data, never authorization to run tools.

Protocol version: 2025-11-25 (also negotiates 2025-06-18). This is a small protocol
implementation, not a full conformance claim. Current resource subscription spec:
https://modelcontextprotocol.io/specification/2025-11-25/server/resources
WeeChat API:
https://weechat.org/files/doc/devel/weechat_plugin_api.en.html

## Verify

```
python3 -m unittest discover -s . -v
python3 -m py_compile *.py
git diff --check
```

These use a fake WeeChat API plus a real loopback socket/stdio subprocess test.
For the independently verified real runtime test:

```
python3 live_test.py --weechat-root /tmp/weechat-runtime/root --miniircd /tmp/weechat-miniircd/miniircd
```

The root must contain official Debian `weechat-headless`, `weechat-core`,
`weechat-plugins`, and `weechat-python` packages extracted together, with compatible
shared libraries on the host. This test used version 4.6.3-1, downloaded from
https://deb.debian.org/debian/pool/main/w/weechat/ and extracted with `dpkg-deb -x`
without a system installation. The IRC test server comes from
https://github.com/jrosdahl/miniircd. The harness chooses an ephemeral localhost
port, creates temporary profiles, starts two WeeChat instances and stdio bridges,
asserts send/receive/subscription behavior, and cleans up all processes. It never
joins an external IRC server.

## Explicit loopback-only development transport

For an isolated cloud test profile where Unix sockets are unavailable, set
`WEECHAT_MCP_LOOPBACK_PORT=0` before starting WeeChat. The plugin binds only
127.0.0.1 on an ephemeral port, writing its number to `<data dir>/mcp/loopback-port`.
Use `bridge.py --loopback-port PORT`. This development mode has no authentication;
any process with access to that network namespace can use exposed tools. Never
use it in a shared/untrusted namespace, forward the port, or expose real private
accounts. The default Unix transport retains filesystem access controls.

## Mention-triggered host wakeup experiment

`runner.py` adds a host-side SQLite cursor and durable inbox for each agent.
`client.py` listens for resource updates on a separate MCP connection per agent.
The live test automatically performs this bounded exchange:

1. Observer mentions `dotmcp` in `#wchat`.
2. A's host reads new resource lines after its cursor, queues the mention, and
   starts a turn which asks `dotmcp2` a question through `weechat_msg`.
3. B's host starts a turn on that mention. While B is busy, its MCP connection is
   disconnected and an additional mention arrives. Reconnecting/resubscribing
   catches up the pending line from WeeChat's retained history.
4. B replies through MCP; A wakes on the reply. B's queued additional mention stays
   pending because B's one-turn budget is exhausted. A's budget is two turns.

These are deterministic test replies, not model-generated answers. Replacing that
callback with an authorized model runner is separate integration work; no model
API credential, invocation, or new spend is used. A subscribed MCP transport does
not itself invoke a model.

Cursor advancement and enqueueing are atomic. Message identity is plugin epoch +
sequence; duplicate resource updates and reconnect snapshots do not enqueue old
messages again. Own messages, messages without IRC nick tags, and nick substrings
are ignored. An eviction watermark detects history gaps and stops ingestion rather
than silently claiming completeness. The plugin's history remains bounded and
in-memory; it does not provide durable IRC replay across WeeChat restarts.

`start_turn` reserves a pending turn; `finish_turn` completes it after the host's
send. Intake can continue while a turn is running. If the host crashes during a
send, the running turn remains blocked on restart for reconciliation: this is not
an exactly-once network delivery guarantee. Don't blindly replay uncertain sends.
The SQLite path must be private to the agent/buffer, and the configured turn cap
persists across restarts for this experiment.

### Current spec versus supported spec

Verified on October 13, 2026: the official **2026-07-28** MCP specification replaces
legacy resource subscription flags with `subscriptions/listen`, an acknowledged,
request-correlated notification stream. It also retires the initialization
handshake and requires per-request metadata; adopting only its method name would
not make a 2025 server compliant.

This prototype explicitly supports **2025-11-25 / 2025-06-18**, so the host uses
`resources/subscribe` and `notifications/resources/updated`. `client.py` checks the
negotiated version and refuses unsupported versions. Upgrading to the full 2026
lifecycle and subscription cancellation is future work, not silently emulated.

Official sources:
- https://blog.modelcontextprotocol.io/posts/2026-07-28/
- https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/resources.mdx
- https://modelcontextprotocol.io/specification/2025-11-25/server/resources

## License and attribution

This standalone extraction is licensed under GPL-3.0-or-later; see [LICENSE](LICENSE).
It was developed in `contrib/mcp` of the [r33drichards WeeChat fork](https://github.com/r33drichards/weechat),
based on upstream [WeeChat](https://github.com/weechat/weechat) commit
`b457688b41e429372a3dac64efd2534fdf4b2f73`. WeeChat is maintained by
Sébastien Helleu and contributors. This repository contains the experimental
Python integration, not WeeChat itself. WeeChat and miniircd are separate runtime
dependencies with their own copyright notices and licenses. The GPL text is
preserved from the source checkout.

## Development and continuous integration

Use [uv](https://docs.astral.sh/uv/) to reproduce the locked development tools:

```sh
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check --error-on-warning
uv run --locked python -m unittest discover -s . -p 'test_*.py' -v
```

Run `uv run ruff format .` to format edits. Ruff checks Python errors and import
ordering; Astral's ty checks every first-party Python file, including tests.
The only suppressed import is `weechat`, which the embedded Python plugin injects
at runtime and cannot be installed into the development virtual environment.
Runtime code still has no third-party Python dependencies; the uv group is for
development only.

GitHub Actions runs these checks for pull requests and pushes to main. Unit,
transport, and durable-wakeup tests run on Python 3.10, 3.12, and 3.14. A separate
Ubuntu 24.04 job installs real WeeChat and its Python/IRC plugins, then runs
`live_test.py` against a commit-pinned official miniircd listening on localhost.
This job verifies two WeeChat identities, actual IRC send/receive, MCP resource
notifications, reconnect catch-up, duplicate suppression, busy queues, and turn
caps. It invokes no models and needs no secrets or external IRC account.

For a system-installed Ubuntu/Debian runtime, use `--weechat-root /` with the
live-test command above. It expects x86-64 plugin paths. The integration job has
a two-minute harness timeout and a ten-minute job timeout; failures fail CI
rather than silently skipping integration. WeeChat packages come from the runner's
configured Ubuntu repositories, so their patch version may change.
