-- Sentinel schema v1.
--
-- transactions: one row per accepted event. UNIQUE (event_id) is the idempotency claim:
--   INSERT ... ON CONFLICT DO NOTHING lets exactly one writer own an event, and a
--   concurrent duplicate waits for that writer to commit before reading its decision.
-- decisions / decision_audit / outbox: written in the same database transaction as the
--   claim, so a decision is never visible without its audit record and its event.

CREATE TABLE rulesets (
    version     TEXT PRIMARY KEY CHECK (version ~ '^[0-9a-f]{64}$'),
    name        TEXT NOT NULL,
    config      JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transactions (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id      TEXT NOT NULL,
    fingerprint   TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    account_id    TEXT NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL,
    amount_minor  NUMERIC(38, 0) NOT NULL CHECK (amount_minor > 0),
    currency      TEXT NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    merchant_id   TEXT NOT NULL,
    payload       JSONB NOT NULL,
    CONSTRAINT transactions_event_id_key UNIQUE (event_id)
);

CREATE INDEX transactions_account_received_idx ON transactions (account_id, received_at);
CREATE INDEX transactions_account_merchant_idx ON transactions (account_id, merchant_id);

CREATE TABLE decisions (
    id                   UUID PRIMARY KEY,
    transaction_id       BIGINT NOT NULL UNIQUE REFERENCES transactions (id),
    outcome              TEXT NOT NULL CHECK (outcome IN ('APPROVE', 'REVIEW', 'DECLINE')),
    reasons              TEXT[] NOT NULL,
    ruleset_version      TEXT NOT NULL REFERENCES rulesets (version),
    budget_exhausted     BOOLEAN NOT NULL,
    decision_latency_us  BIGINT NOT NULL CHECK (decision_latency_us >= 0),
    decided_at           TIMESTAMPTZ NOT NULL
);

CREATE TABLE decision_audit (
    decision_id  UUID PRIMARY KEY REFERENCES decisions (id),
    record       JSONB NOT NULL,
    digest       TEXT NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE outbox (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic         TEXT NOT NULL,
    message_key   TEXT NOT NULL,
    payload       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at  TIMESTAMPTZ,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);

CREATE INDEX outbox_pending_idx ON outbox (id) WHERE published_at IS NULL;
