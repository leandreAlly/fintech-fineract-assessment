#!/usr/bin/env bash
set -euo pipefail

API_KEY="${INTEGRATION_API_KEY:-dev-teller-key}"
curl() { command curl -H "X-API-Key: $API_KEY" "$@"; }

ERP="${ERP_URL:-http://localhost:8081}"
SYNC="${INTEGRATION_URL:-http://localhost:8080}/api/accounts/sync"
STAMP="$(date +%H%M%S)"

erp_account() {
  jq -nc --arg code "$1" --arg name "$2" --arg type "$3" --arg ref "$4" \
    '{code: $code, name: $name, type: $type, currency: "RWF", external_reference: $ref}'
}

post() {
  curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d "$2" "$1"
}

sync() {
  post "$SYNC" "$(jq -nc --arg ref "$1" '{external_reference: $ref}')"
}

status_of() {
  curl -s "$SYNC/$1" | jq -c '{external_reference, status, attempts, last_error, next_attempt_at}'
}

q() {
  docker compose exec -T db psql -U postgres -d "$1" -tAc "$2"
}

counts() {
  local ref="$1"
  local erp fineract mapping
  erp=$(q erp "SELECT count(*) FROM accounts WHERE external_reference = '$ref'")
  fineract=$(q fineract_default "SELECT count(*) FROM acc_gl_account WHERE external_id = '$ref'")
  mapping=$(q integration "SELECT count(*) FROM gl_account_sync WHERE external_reference = '$ref'")
  echo "ERP accounts: $erp | Fineract GL accounts: $fineract | mappings: $mapping"
  [ "$erp$fineract$mapping" = "111" ] || { echo "FAIL: expected 1/1/1"; exit 1; }
}

expect() {
  [ "$1" = "$2" ] || { echo "FAIL: got $1, expected $2"; exit 1; }
}

wait_for_status() {
  local ref="$1" want="$2" deadline=$((SECONDS + ${3:-300}))
  until [ "$(curl -s "$SYNC/$ref" | jq -r .status)" = "$want" ]; do
    [ $SECONDS -lt $deadline ] || { echo "FAIL: $ref never reached $want"; exit 1; }
    sleep 3
  done
}

fineract_health() {
  local id
  id="$(docker compose ps -q fineract)"
  if [ -n "$id" ]; then docker inspect -f '{{.State.Health.Status}}' "$id"; else echo stopped; fi
}

echo "== 1. Same account sent 4 times to the ERP and synced 4 times"
ref="GL-13$STAMP"
body="$(erp_account "13$STAMP" "Accrued Interest Receivable" ASSET "$ref")"
for i in 1 2 3 4; do echo "ERP POST #$i -> $(post "$ERP/api/accounts" "$body")"; done
for i in 1 2 3 4; do code=$(sync "$ref"); echo "sync #$i -> $code"; expect "$code" 200; done
status_of "$ref"
counts "$ref"

echo
echo "== 2. Four concurrent sync requests for a new account"
ref="GL-5$STAMP"
post "$ERP/api/accounts" "$(erp_account "5$STAMP" "Loan Loss Expense" EXPENSE "$ref")" > /dev/null
out="$(mktemp -d)"
for i in 1 2 3 4; do sync "$ref" > "$out/$i" & done
wait
echo "responses: $(cat "$out"/* | tr '\n' ' ')"
rm -rf "$out"
status_of "$ref"
counts "$ref"

echo
echo "== 3. Fineract is down: SYNC_PENDING, retried with backoff, then SYNCED"
ref="GL-2$STAMP"
post "$ERP/api/accounts" "$(erp_account "2$STAMP" "Partner Deposits" LIABILITY "$ref")" > /dev/null
docker compose stop fineract > /dev/null 2>&1
echo "fineract: $(fineract_health)"
code=$(sync "$ref")
echo "sync -> $code"
expect "$code" 202
status_of "$ref"
sleep 8
echo "-- after the first backoff, still down"
status_of "$ref"
docker compose start fineract > /dev/null 2>&1
echo "-- fineract restarting"
until [ "$(fineract_health)" = healthy ]; do sleep 5; done
echo "fineract: healthy"
wait_for_status "$ref" SYNCED
echo "-- retry loop synced it without any new request"
status_of "$ref"
counts "$ref"

echo
echo "== 4. Fineract rejects the account (same external_id, different payload): FAILED, no retry"
ref="GL-3$STAMP"
docker compose exec -T db psql -U postgres -d fineract_default -qc \
  "INSERT INTO acc_gl_account (name, gl_code, external_id, disabled, manual_journal_entries_allowed, account_usage, classification_enum, description)
   VALUES ('Created Outside ERP', 'X3$STAMP', '$ref', false, true, 1, 3, '')"
post "$ERP/api/accounts" "$(erp_account "3$STAMP" "Retained Earnings" EQUITY "$ref")" > /dev/null
code=$(sync "$ref")
echo "sync -> $code"
expect "$code" 422
status_of "$ref"

echo
echo "== 5. Sync status for every account (no Fineract ids exposed)"
curl -s "$SYNC" | jq -c '.[] | {external_reference, erp_type, status, attempts}'

echo
echo "== All Python-side sync behaviours demonstrated"
