# ADR-0005: LISTEN/NOTIFY delivery model

- **Status:** accepted
- **Date:** 2026-05-24

## Context

Batch E (Phase 26) wants to bridge PostgreSQL's `LISTEN` / `NOTIFY` to
the MCP tool surface so an agent can subscribe to a channel and react
to notifications. PG delivers notifications asynchronously on a held
connection — the listening session blocks (or polls) until a NOTIFY
arrives, then yields the payload.

MCP, by contrast, is request/response: a tool call takes arguments and
returns a result. There is no built-in "the server has data for you"
inversion. Phase 26 must choose how to bridge these two models.

This decision shapes server architecture (background workers? extra
state in `AppContext`?), the MCP wire shape, and the failure modes
(disconnected listeners, queue overflow, dead subscriptions).

## Options considered

1. **Streaming via MCP notifications.** Extend the FastMCP wiring so
   tool calls can emit `notifications/resources/updated` (or a custom
   notification) back to the client when a NOTIFY arrives. The closest
   match to the underlying PG model. But: requires bypassing FastMCP's
   per-tool-call lifecycle, introducing a long-lived background task
   that survives across tool calls, and changes the server's failure
   surface (a dead notification connection now has to be reaped). Tools
   become asymmetric — some return, some emit. The MCP client
   ecosystem support for arbitrary server-sent notifications is uneven.
2. **Tool-poll model.** A `subscribe_channel(channel)` tool opens a
   server-side listener on the **lifespan database** connection and
   buffers incoming notifications in a bounded in-process queue keyed
   by subscription id. A second `poll_notifications(subscription_id,
   timeout_ms=0)` tool drains that queue. Subscriptions are closed
   either by an explicit `unsubscribe_channel(subscription_id)` or by
   server lifespan exit. Fits the request/response model perfectly —
   tools stay symmetric, no MCP wire changes. Trade-off: between polls,
   notifications sit in memory and a slow consumer can fill the queue.
3. **External broker (Redis Streams, NATS, Kafka).** Strongest
   durability but adds a hard infrastructure dependency that MCPg has
   so far avoided (current deps: Python + PostgreSQL only). Rejected
   for v1; an operator who wants durability can put a broker between
   PG and their consumers.

## Decision

**Option 2 — tool-poll model.** New tools:

- `subscribe_channel(channel: str) -> {subscription_id, channel}` —
  opens a PG `LISTEN` (idempotent per channel), creates an in-process
  subscription record, returns its id.
- `poll_notifications(subscription_id: str, timeout_ms: int = 0,
  max_messages: int = 100) -> list[Notification]` — drains up to
  `max_messages` from the subscription's queue, waiting at most
  `timeout_ms` for at least one if the queue is empty. Returns `[]` on
  timeout. Each notification carries `{channel, payload,
  delivered_at}`.
- `unsubscribe_channel(subscription_id: str) -> bool` — removes the
  subscription and, if it was the last one on the channel, issues an
  `UNLISTEN`. Returns `True` when the subscription existed.
- `list_subscriptions(...)` already exists as a logical-replication
  read tool (Phase 16); the new tool will be named
  `list_notification_subscriptions` to avoid the collision.

Implementation outline:

- A new `mcpg.listen` module owns subscription state. It uses a
  dedicated background `asyncio.Task` per server lifespan that holds
  one PG connection (separate from the request pool), runs `LISTEN`
  for every active channel, and drains psycopg's notifies generator
  into per-subscription `asyncio.Queue` instances. Queues are bounded
  (default 1000); overflow drops the oldest message and flags the
  next `poll_notifications` response with `dropped_count`.
- Subscriptions live in process memory. On server restart they are
  lost — agents must re-subscribe. This is documented behaviour, not
  a bug; durable subscriptions are out of scope for v1.
- All three tools are gated under a new `Capability.LISTEN` enum
  entry. `subscribe_channel` and `unsubscribe_channel` require
  unrestricted mode + a new `MCPG_ALLOW_LISTEN` opt-in setting
  (default false). `poll_notifications` runs in any mode where a
  subscription already exists for the caller — the gate is on
  *creating* subscriptions, not *reading* them.

## Consequences

What becomes easier:

- Bridging PG events to agentic workflows: an agent can wait on
  `cache_invalidated` and react.
- The server stays request/response; no new MCP-side notifications
  protocol surface to maintain.
- Background-task ownership has a clear home (`mcpg.listen`) and
  shutdown story (server lifespan close cancels the task and drains
  the queues).

What becomes harder:

- A dedicated background task and an extra PG connection live for the
  server's lifetime — the pool size grows by one effective slot. The
  default `pool_max_size=5` is unaffected (the listen connection sits
  outside the pool), but the operator should know.
- A slow consumer fills a bounded queue and starts dropping events.
  Tests must pin the drop-oldest behaviour and surface the
  `dropped_count` to callers.
- Cross-process scaling (multiple MCPg replicas) means each replica
  has independent subscriptions — an agent's `subscribe_channel`
  binds to one replica and won't roam. Hosted deployments that need
  consistency must use sticky sessions or stand up the durable-broker
  variant (Option 3) as a separate tool family in a future ADR.

Follow-ups:

- Define the `Notification` dataclass (channel, payload, delivered_at,
  optional `dropped_count`).
- Confirm that psycopg 3's async notifies API surfaces enough for the
  background loop (`AsyncConnection.notifies()` async iterator).
- Sketch the test fixture: psycopg supports `pg_notify(...)` so the
  integration test can self-publish.
