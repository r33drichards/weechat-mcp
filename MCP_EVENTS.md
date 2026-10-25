# Explicit chat subscriptions with MCP Events

This implements the webhook subset of [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events) for MCP `2026-07-28`, using the existing authenticated `/mcp` endpoint and pinned FastMCP/SDK versions.

## User workflow

1. Enable and rescan the server's events in the plugin after deploying this change.
2. In the chat that should receive updates, explicitly ask to monitor an exposed IRC channel and choose **all incoming messages** or **mentions of a particular nick**. Include what the chat should do with updates.
3. The host supplies the chat's callback URL and signing secret through `events/subscribe`. These are protocol fields, not a tool's model-facing arguments.
4. Ask that chat to stop monitoring; `events/unsubscribe` cancels its matching subscription.

For example: “In this chat, monitor `irc.example.#project` for mentions of `dot`, and tell me when someone asks a question.” This is illustrative; the server does not execute this request or subscribe any chat by itself.

Starting the process, starting a chat, joining a channel, listing tools/events, and reading messages **never create a subscription**. Fresh subscriptions start after their explicit request, without importing old messages. Separate callback URLs represent separate chats, including two chats watching the same channel. Existing unexpired, explicitly created subscriptions resume after a server restart; they are not inherited by a new chat.

Supported hosts are Work chats on ChatGPT web, desktop Work chats with Cloud selected, and dots, subject to workspace controls. This feature does not implement event polling, event streaming, or `gap`/`terminated` notifications. The older experimental resource-update listener remains unchanged and is separate from MCP Events.

## Operator configuration

Events are disabled unless `MCP_EVENTS_DB` is set to an absolute SQLite path on persistent storage, for example `/data/weechat/mcp/events.sqlite3` when `WEECHAT_HOME=/data/weechat` is mounted persistently. Its directory must be owned by the service user with mode `0700`; its database must have mode `0600`. The image already creates WeeChat's `mcp` directory privately. Use one bridge process per database; an exclusive file lock prevents competing dispatchers.

Events require HTTP transport. Bearer mode scopes grants to the verified runtime bearer credential. Changing that credential and restarting invalidates the previous owner's grants. For the existing guarded Railway-private, **single-owner** authentication proxy, also set a stable `MCP_EVENTS_OWNER` identifier. Without it, startup fails closed. This identifier is an operator assertion about the proxy's sole owner, not a client-supplied user ID. Do not enable that mode behind a multi-user proxy; a future multi-user deployment needs a verified per-user principal and revocation integration.

Configuration enables event discovery only. It does not grant monitoring or create subscriptions. Outbound public HTTPS access is needed only when a host explicitly subscribes or a previously authorized subscription delivers.

No new credentials, live subscriptions, plugin settings, deployment settings, or production configuration are created by this change.

## Protocol and delivery

The event catalog contains `message.created` with required arguments:

- `channel_id`: exact exposed IRC buffer name returned by `weechat_buffers`
- `trigger`: `message` or `mention`
- `nick`: required only for `mention`

Only IRC channel buffers exposed by the selected policy qualify: all current native IRC channels in default full mode, or configured allowlisted channels with `WEECHAT_MCP_FULL_CONTROL=0`. A newly joined full-mode channel is eligible only for a separately requested subscription. Joining/discovery does not create subscriptions or enable private-message monitoring. Own-nick messages, `notify_self` messages, notices and status lines are excluded. Mentions use whole-nick boundaries and RFC1459 case folding; ASCII/strict-RFC1459 server-specific case mappings are not yet negotiated.

Subscription identity hashes authenticated owner, exact callback URL, event name and canonical argument JSON. Refresh is idempotent. Unsubscribe uses that same identity, succeeds if already absent, deletes queued events and secrets, and waits for an already-started delivery for that subscription to finish before acknowledging. An accepted/in-flight request cannot be recalled.

Each callback is verified with a fresh signed challenge before activation. Verification is cached for five minutes by principal and URL. Signing keys must be `whsec_` plus valid Base64 encoding 24–64 bytes. Refresh replaces the key and signs with old and new keys during a five-minute rotation window.

The transport validates HTTPS URLs, resolves and rejects non-public destinations at each connection, pins a validated numeric address, and verifies TLS against the original hostname. It never follows redirects or uses environment proxies. A ten-second deadline covers DNS, connect, TLS and response reading. Requests, response bodies, headers and HTTP framing are bounded. Errors are sanitized; secrets remain masked through generic FastMCP hooks.

SQLite commits captured events and intake cursors atomically. Each event gets a stable ID and byte-exact payload; retries preserve both, with a fresh Standard Webhooks timestamp/signature. Delivery is ordered within a subscription, so a cursor cannot skip its pending head. Up to eight subscriptions deliver concurrently while a separate push-driven intake worker continues collecting messages.

Transient network failures, HTTP 408/429, and 5xx responses use exponential backoff, up to eight attempts. Other failures, including 410/413, stop the grant without retrying the failed event. Refresh then returns an actionable error; explicitly unsubscribe and subscribe again to start a new future-only grant. The server never silently activates an inert refresh. Retry counts survive crashes, including uncertain final-attempt delivery.

## Lifetime, replay and limits

The default lifetime is one hour; `ttlMs` requests are capped at 24 hours and are never lengthened. A request for no expiration (`ttlMs:null`) receives a finite one-hour grant. Delivery stops at expiry. Hosts should refresh before the returned `refreshBefore`.

Subscriptions and pending payloads survive bridge restarts. A configured channel temporarily absent during reconnect/autojoin is dormant rather than revoked. Removed allowlist scope or a changed authenticated owner stops delivery and removes the grant. The private proxy cannot independently inform the bridge of every host-side account disconnection: stopping then relies on unsubscribe, callback rejection, expiry, or operator credential/owner revocation.

Acknowledged replay is retained for up to 24 hours and at most 10,000 events per subscription. Pending queues are capped at 10,000 events; overflow stops the grant rather than silently advancing past missing data. There are at most 100 subscriptions per database, including concurrent creation. Expired grants are retained for one additional day for refresh/replay, then removed. SQLite secure deletion is enabled.

Replay also depends on what WeeChat retained while the bridge was offline. Event intake reads a bounded newest native WeeChat snapshot (up to 500 lines, with byte limits), using native line IDs and a buffer lifecycle epoch. There is no duplicate content ring or custom history-read tool. A confirmed omitted native window, epoch change, or conservative native-retention uncertainty sets `truncated:true` on subsequent subscription responses. On WeeChat versions with stable native buffer IDs, plugin reload preserves the private Events lifetime while public pagination cursors still expire. A changed or unknown buffer lifetime resumes from its current tip and marks an interruption instead of importing possibly pre-subscription history. That flag is conservatively sticky for the grant. No unsupported control notification is sent. This is bounded replay, not a lossless IRC archive.

Delivery is at least once, not exactly once. If a process crashes after the receiver accepted the HTTP request but before the acknowledgement commits, the same event can be sent again. Consumers should deduplicate by `eventId`; any follow-on write actions must be idempotent. This feature itself performs no IRC writes.

The runtime database contains callback URLs, signing secrets and message payloads. Keep it on private storage, protect backups, and do not attach it to support issues or log raw event requests/headers. Secrets are absent from catalog/subscription responses and normal logs; they are intentionally persisted in the runtime store to survive restarts.

## Validation and rollout

Offline tests cover real in-memory FastMCP wire dispatch, producer capture, no-default opt-in, chat/owner/channel isolation, verification/signing, SSRF/DNS pinning, deduplication, restart/expiry/replay, revocation, queue ordering, retries, concurrent quota enforcement and slow receivers. Run:

```sh
uv run --locked python -m unittest discover -s . -p 'test_*.py' -v
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked ty check --error-on-warning
```

Deployment and live validation are separate approval steps. After deployment, rescan the plugin, explicitly subscribe a test chat to a selected test channel, verify a matching and non-matching event, then unsubscribe. Do not turn the sample workflow into startup automation.

## Optional Jev preference filter (disabled by default)

`trigger:"message"` already captures every eligible incoming IRC message. An
optional `jev` argument now makes a separate send/drop decision for each captured
message before its webhook is sent. Existing mention and all-message subscriptions
without this argument are unchanged; upgrading never widens a grant.

Operators must explicitly enable `JEV_FILTER_ENABLED=true` and inject `JEV_API_KEY`
through their secret manager. Missing/invalid enabled configuration fails startup;
no key is read when disabled. `JEV_MODEL` optionally overrides the default pinned
`jev-1.14.0`. The endpoint is fixed to `https://api.typesafe.ai/v1/systemone`;
subscribers cannot provide a URL, model, credential or system instruction.
The image includes the adapter; no provider SDK is required.

After the user authorizes disclosing this channel context to Typesafe, the host
can explicitly subscribe with these event arguments:

```json
{
  "channel_id": "irc.example.#project",
  "trigger": "message",
  "jev": {
    "preference": "Forward questions for me and deployment failures; drop casual chat.",
    "history_limit": 20
  }
}
```

The preference is user-controlled, nonblank, and at most 2,048 characters. It is
scoped to the authenticated owner, callback/chat and channel subscription, never
global. Changing it (or the history limit) creates a different subscription identity:
unsubscribe the old exact argument set before creating its replacement, or both
remain active. Refresh the exact same argument set to retain cursor/decisions.
`jev` is accepted only with `trigger:"message"`; no nickname trigger is needed.

Input to Jev is the current message, preference, and up to 20 preceding native
channel chat lines by default (configurable 0–50, at most 32 KiB serialized prior
context). History may include the user's own messages and pre-subscription lines,
but not notices/status lines, other channels, future lines, webhook credentials or
callback URLs. The candidate message itself must be eligible under the existing
rules (so self messages still do not trigger). Snapshots reflect what WeeChat
retained, not a complete archive. They are captured at intake and persisted with
the event, so a backlog/retry does not acquire later context. Total provider
requests are capped at 64 KiB; oversized input stops the grant rather than silently
truncating the current message. Preferences/history are private runtime data with
the same database/backups protections as existing event payloads.

Jev receives untrusted IRC content as data, separately from the fixed decision
instructions and user preference. Its only allowed result is `send` or `drop`.
It cannot edit the message, call a tool, change the preference, change the target,
or authorize an IRC write. Prompt boundaries reduce risk, but model judgments
can still be mistaken; this is a relevance filter, not a security boundary.
The webhook payload remains the original message only, never the surrounding
history/preference/provider response.

### Failure, cost and replay behavior

- Each captured message for each opted-in subscription can incur a paid Jev call.
  The operator controls the key/model and must approve provider disclosure and
  costs before enabling. Tests use synthetic inputs and fake responses only.
- At most eight subscriptions classify/deliver concurrently, with ordered
  processing per subscription and the existing 10,000-event queue bound. Slow
  classification does not block WeeChat intake. There is no automatic model retry.
- The adapter has a five-second deadline, bounded HTTP response, validated public
  HTTPS destination/TLS, no redirects/environment proxies, and strict JSON choice
  parsing. Provider failures, rate limits, timeouts, missing configuration and
  malformed results **suspend the grant** without forwarding or treating the error
  as a semantic drop. Refresh reports the existing actionable `Delivery stopped`
  error; explicitly unsubscribe/resubscribe to resume future-only intake.
- A send/drop result commits before webhook delivery. Send retries and replay
  reuse that decision and byte-exact payload; drop events never reach the callback
  or reappear on replay. Dropped events advance the subscription cursor and are
  pruned under the same 24-hour/10,000 settled-event retention as delivered events.
- A crash before the decision commits can repeat a provider request (and cost),
  but cannot forward without a committed send decision. Unsubscribe waits for an
  already-started decision/send; expiry, revocation and channel scope are rechecked
  before subsequent webhook I/O. No exactly-once provider-call guarantee is made.

The SQLite change is additive (`filter_context` and `decision` columns). Back up
the private runtime DB before upgrading. Do not run an older dispatcher against
new filtered rows: it cannot interpret their decisions. Disable new grants and
explicitly unsubscribe existing filtered grants before rollback. No deployment,
provider credentials or live subscriptions are created by this PR.

Primary API references: [Choice](https://docs.typesafe.ai/primitives/choice),
[System One](https://docs.typesafe.ai/api/system-one),
[models](https://docs.typesafe.ai/models).
