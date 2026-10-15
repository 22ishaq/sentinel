#!/usr/bin/env bash
# End-to-end smoke test against a running stack (docker compose up, or sentinel serve).
# Exercises: a decision, an idempotent replay, a conflict, a typed validation error and
# audit verification. Exits non-zero on the first surprise.
set -euo pipefail

BASE="${SENTINEL_URL:-http://127.0.0.1:8000}"
EVENT_ID="smoke_$(date +%s)_$RANDOM"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

for _ in $(seq 1 60); do
  curl -fsS "$BASE/readyz" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "$BASE/readyz" >/dev/null

event() {
  printf '{"event_id":"%s","occurred_at":"%s","account":{"id":"acc_smoke","country":"GB"},"amount":"%s","currency":"GBP","channel":"card_present","merchant":{"id":"m_smoke","category_code":"5411","country":"GB"}}' \
    "$EVENT_ID" "$NOW" "$1"
}

post() {
  curl -sS -o /tmp/sentinel-smoke.json -w '%{http_code}' -X POST "$BASE/v1/transactions" \
    -H 'Content-Type: application/json' --data "$1"
}

field() {
  python3 -c "import json,sys; print(json.load(open('/tmp/sentinel-smoke.json'))$1)"
}

expect() {
  if [[ "$1" != "$2" ]]; then
    echo "FAIL: $3 (expected $2, got $1)" >&2
    cat /tmp/sentinel-smoke.json >&2
    exit 1
  fi
  echo "ok   $3"
}

expect "$(post "$(event 12.50)")" 201 "new event is decided"
DECISION_ID="$(field "['decision_id']")"
expect "$(field "['outcome']")" APPROVE "small domestic payment is approved"

expect "$(post "$(event 12.50)")" 200 "retry returns the stored decision"
expect "$(field "['decision_id']")" "$DECISION_ID" "retry returns the same decision id"

expect "$(post "$(event 99.00)")" 409 "same event_id with a different payload conflicts"
expect "$(field "['error']['code']")" IDEMPOTENCY_CONFLICT "conflict carries a typed code"

expect "$(post '{"event_id":"smoke_float","amount":12.5}')" 422 "invalid event is rejected"

curl -fsS "$BASE/v1/decisions/$DECISION_ID/audit" -o /tmp/sentinel-smoke.json
expect "$(field "['verification']['ok']")" True "audit record replays to the same decision"

curl -fsS "$BASE/metrics" | grep -q 'sentinel_decisions_total{outcome="APPROVE"}'
echo "ok   metrics are exposed"
echo "smoke test passed"
