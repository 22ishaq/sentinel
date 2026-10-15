# ADR 0005: A transactional outbox for decision events

**Status:** accepted

## Context

Downstream systems (case management, notifications, analytics) react to decisions. Publishing to a
broker directly from the request path creates a dual-write problem. A decision can commit while the
publish fails, or the event can be published for a decision that then rolls back.

## Decision

- **Write, don't publish.** The decision transaction inserts a `decision.made` message into `outbox`
  and calls `pg_notify`. The message exists if and only if the decision committed.
- **A separate relay.** The relay (`sentinel relay`) works in batches:
  1. It claims pending rows with `FOR UPDATE SKIP LOCKED`, so several relays never publish the same
     row concurrently.
  2. It publishes them in id order to a sink (stdout JSON lines, or an HTTP webhook with an
     `Idempotency-Key` header).
  3. It marks them published in the claiming transaction.
- **On failure**, the relay records `attempts` and `last_error` and stops the batch, so later messages
  never overtake an earlier one. After `max_attempts` the message is parked: it stays unpublished
  for inspection, and the queue moves on.
- **Waking up.** The relay `LISTEN`s and sleeps until a commit notifies it. The poll interval is only
  a safety net.

## Consequences

- Delivery is **at least once**. If the relay crashes after publishing but before committing, the
  batch is published again. Consumers de-duplicate on the envelope `id`.
- The tests cover:
  - publishing in order and marking messages published,
  - stopping the batch on failure, then resuming in order,
  - parking a poison message after `max_attempts`,
  - three concurrent relays with no duplicates,
  - a NOTIFY wake-up well under the poll interval.
- With several relays, batches can interleave. Strict per-account ordering would need one relay per
  partition. Decision events are independent facts with their own ids, so this design accepts the
  interleaving.
