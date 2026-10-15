# ADR 0003: A latency budget enforced inside PostgreSQL

**Status:** accepted

## Context

A card authorisation has to be answered within a fixed time. A decision that arrives late is worse
than a cautious one that arrives on time. Almost all of the decision time is spent in the database:
waiting for the per-account lock, and reading the account's history. The rules themselves take
microseconds.

## Decision

- **A deadline per decision.** Each decision gets a `Deadline` on the monotonic clock (default 50 ms,
  set with `SENTINEL_DECISION_BUDGET_MS`).
- **The lookup gets 80% of it.** The account lock and the feature query run inside a savepoint with
  `lock_timeout` and `statement_timeout` set to what remains of that 80%. PostgreSQL then enforces the
  budget itself. A cancelled lookup rolls back only the savepoint, so the event's idempotency claim
  survives and the decision is still recorded.
- **The rest is reserved for the rules.** Static rules, such as blocked countries, need no history
  and always run. A slow query must never hide a DECLINE that needed no data.
- **Deadline checks between rules.** The engine checks the deadline before each rule. If it has
  passed, the remaining rules are recorded as `skipped`.
- **Degraded decisions.** A decision whose features are missing or whose budget ran out is
  *degraded*. Its outcome is raised to the ruleset's `fallback_outcome` (REVIEW by default; APPROVE
  for fail-open deployments), and it is **never lowered**: a DECLINE that already fired stays a
  DECLINE. The reasons say why (`features_unavailable`, `budget_exhausted`), and a metric counts
  these decisions.
- **Per-account serialisation.** Decisions for one account are serialised with
  `pg_advisory_xact_lock(hashtextextended(account_id, 0))`. Velocity features are therefore read
  after every earlier decision for that account has committed. Without this, two concurrent payments
  could both pass a spend limit that only one of them fits in. Rulesets with no history rules skip
  the lock entirely.

## Consequences

- The tests hold the account lock from another session, take an `ACCESS EXCLUSIVE` lock to stall the
  feature query, and use a clock that jumps past the deadline. In all three cases the decision falls
  back within the budget and still verifies against its audit record.
- One busy account is serialised. That is the price of exact spend limits, and it only affects that
  account.
- Under load (4 processes, 3,300 decisions/s on one laptop) the decision path p99 was 0.95 ms. None of
  the 64,000 measured decisions came near the 50 ms budget. The fallback exists for the database's
  bad days, not the median.
