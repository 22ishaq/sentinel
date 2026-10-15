# ADR 0004: A reconstructable, tamper-evident audit trail

**Status:** accepted

## Context

A decision has to be explainable long after it was made: to a customer, an analyst or a regulator.
Two things make that hard. First, rulesets change, so "the current rules" cannot explain an old
decision. Second, account history keeps growing, so recomputing features today gives different
inputs than the engine saw at the time.

## Decision

- **A self-contained record.** Each decision stores a record (schema `sentinel.audit/v1`) with:
  - the normalised event,
  - the feature values the rules saw,
  - the ruleset name and version,
  - every rule's status, outcome, detail and evidence,
  - the final outcome and reasons,
  - the budget state, including how many rules ran before it expired,
  - per-stage timings.
- **Versions are content hashes.** A ruleset's version is the SHA-256 of its canonical JSON. Every
  ruleset ever used is stored under that key, so the version cannot drift from its content.
- **A digest beside each record.** The record's digest is the SHA-256 of its canonical JSON (sorted
  keys, no floats). It is written next to the record in the same transaction.
- **Verification.** `verify()` (also exposed as `sentinel verify` and in `GET …/audit`) re-derives the
  decision and reports three independent checks:
  1. **Digest:** the record still hashes to the stored digest.
  2. **Ruleset:** the stored ruleset config still hashes to the version the decision cites.
  3. **Replay:** the event is re-parsed from the record, the features are rebuilt from the record,
     and the engine is re-run with a `StepBudget` that stops after the same number of rules. The
     outcome, reasons and per-rule results must match both the record and the `decisions` row, and
     the event must match the stored fingerprint.

## Consequences

- Each kind of tampering trips a different check, and each is tested:
  - Editing the record breaks (1).
  - Rewriting the record and its digest together still fails (3), because the replay disagrees.
  - Editing the decision row fails (3).
  - Editing the ruleset fails (2).
- Old decisions keep verifying after the ruleset changes, and after the account's history grows.
- This proves internal consistency, not resistance to an attacker with full database access who
  recomputes everything consistently. That would need an external anchor: a signed digest chain or
  write-once storage. It is a natural next step.
