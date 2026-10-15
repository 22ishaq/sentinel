# ADR 0002: Idempotency through a unique claim

**Status:** accepted

## Context

Payment networks retry. A client that times out cannot know whether its request was processed, so it
sends it again, sometimes several copies at once. A second decision for the same event is a real
error: it can approve a payment that was declined, or double-count spend.

## Decision

- **The claim.** The event's `event_id` is the idempotency key. The first database statement of the
  pipeline is `INSERT INTO transactions ... ON CONFLICT (event_id) DO NOTHING RETURNING id`.
- **Concurrent duplicates wait.** If another transaction is still inserting the same `event_id`,
  PostgreSQL makes this statement wait for it.
  - If that writer commits, the insert returns no row, and we read and return its decision.
  - If it rolls back, our insert succeeds and we own the event.
- **One transaction.** The claim, the decision, the audit record and the outbox message commit
  together, so a replay can never find a claimed event without its decision.
- **Same id, different payload.** The fingerprint is a SHA-256 of the *normalised* event, so
  `"12.5"` and `"12.50"` match and key order does not matter. A replay whose fingerprint differs from
  the stored one is `409 IDEMPOTENCY_CONFLICT`. The details include the original decision id.

## Alternatives considered

- **Check, then insert.** A `SELECT` followed by an `INSERT` has a race window under concurrency.
- **An in-memory or Redis lock.** This adds a second system whose failure modes (expiry, partitions)
  can still let duplicates through, and the database constraint would be needed anyway.
- **Separate idempotency-key table.** This means an extra write per request. The event table's own
  unique constraint is enough.

## Consequences

- The tests cover 32 concurrent identical submissions (one decision, 31 replays) and 12 concurrent
  submissions with conflicting payloads (exactly one winner, the rest `409`).
- A replay returns `200` with `Idempotent-Replayed: true`, and the body is identical to the original.
- Event ids are unique forever. Producers must not reuse them for new events.
