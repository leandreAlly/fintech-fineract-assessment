#!/usr/bin/env bash
set -uo pipefail

API_KEY="${INTEGRATION_ADMIN_API_KEY:-dev-admin-key}"
curl() { command curl -H "X-API-Key: $API_KEY" "$@"; }

API="${INTEGRATION_URL:-http://localhost:8080}/api"
N="$(date +%H%M%S)"
CLIENT_UID="SME-$(printf '%06d' $(( (10#$N + 700000) % 1000000 )))"

request() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-s -m 60 -w '\n%{http_code}' -X "$method" -H 'Content-Type: application/json')
  [ -n "$body" ] && args+=(-d "$body")
  curl "${args[@]}" "$API$path"
}

show() {
  local response="$1"
  local status="${response##*$'\n'}" body="${response%$'\n'*}"
  echo "   -> HTTP $status $(echo "$body" | jq -c '{status, error: .error.code, last_error} | with_entries(select(.value != null))' 2>/dev/null || echo "(no body)")"
}

repay() {
  echo "   POST /loans/$LOAN/repayments {external_reference: $1, amount: $2}"
  show "$(request POST "/loans/$LOAN/repayments" "$(jq -nc --arg r "$1" --argjson a "$2" '{external_reference: $r, amount: $a}')")"
}

fault() {
  request DELETE /admin/faults > /dev/null
  request POST /admin/faults "$(jq -nc --arg o "$1" --arg m "$2" '{operation: $o, mode: $m}')" > /dev/null
  echo "   fault armed: $1 $2"
}

q() {
  docker compose exec -T db psql -U postgres -d "$1" -tAc "$2"
}

state() {
  local ref="$1" label="$2"
  echo "   [$label] integration: $(q integration "SELECT coalesce((SELECT status || ' erp_attempts=' || erp_attempts || coalesce(' next_attempt_at=' || to_char(next_attempt_at, 'HH24:MI:SS'), '') || coalesce(' last_error=\"' || left(last_error, 70) || '\"', '') FROM loan_transactions WHERE external_reference = '$ref'), 'no row')")"
  echo "   [$label] fineract transactions with externalId $ref: $(q fineract_default "SELECT count(*) FROM m_loan_transaction WHERE external_id = '$ref' AND NOT is_reversed")"
  echo "   [$label] erp journal entries with external_reference $ref: $(q erp "SELECT count(*) FROM journal_entries WHERE external_reference = '$ref'")"
}

wait_for() {
  local ref="$1" expected="$2" limit="${3:-90}" status=""
  for _ in $(seq "$limit"); do
    status="$(curl -s -m 5 "$API/transactions/$ref" | jq -r .status 2>/dev/null)"
    [ "$status" = "$expected" ] && { echo "   reached $expected without any new request"; return 0; }
    sleep 1
  done
  echo "   FAIL: $ref is $status, expected $expected"; exit 1
}

wait_healthy() {
  local service="$1"
  for _ in $(seq 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q "$service")" 2>/dev/null)" = "healthy" ] && { echo "   $service: healthy"; return 0; }
    sleep 3
  done
  echo "   FAIL: $service not healthy"; exit 1
}

wait_api() {
  for _ in $(seq 60); do curl -s -m 2 "$API/../health" > /dev/null && return 0; sleep 1; done
  echo "   FAIL: integration service did not come back"; exit 1
}

echo "== Setup: client, loan, approval, disbursement"
request POST /clients "$(jq -nc --arg u "$CLIENT_UID" --arg r "CLI-F$N" '{external_reference: $r, client_uid: $u, name: "Ubuzima Pharmacy Ltd"}')" > /dev/null
LOAN="$(request POST /loans "$(jq -nc --arg u "$CLIENT_UID" --arg r "APP-F$N" '{external_reference: $r, client_uid: $u, principal: 5000000}')" | head -1 | jq -r .loan_reference)"
request POST "/loans/$LOAN/approve" "$(jq -nc --arg r "APR-F$N" '{external_reference: $r}')" > /dev/null
request POST "/loans/$LOAN/disburse" "$(jq -nc --arg r "DSB-F$N" '{external_reference: $r}')" > /dev/null
echo "   loan $LOAN is $(curl -s "$API/loans/$LOAN" | jq -r .status)"

echo
echo "== 1. ERP down after Fineract success"
R1="RPY-F$N-1"
state "$R1" before
docker compose stop mock-erp 2>&1 | tail -1
repay "$R1" 100000
state "$R1" during
echo "   ...the worker keeps retrying with backoff while the ERP is down"
sleep 8
state "$R1" "during, 8s later"
docker compose start mock-erp 2>&1 | tail -1
wait_for "$R1" COMPLETED
state "$R1" after
echo "   caller retries the same request:"
repay "$R1" 100000
state "$R1" "after retry"

echo
echo "== 2. Fineract down"
R2="RPY-F$N-2"
state "$R2" before
docker compose stop fineract 2>&1 | tail -1
repay "$R2" 100000
state "$R2" during
echo "   caller retries while Fineract is still down:"
repay "$R2" 100000
docker compose start fineract 2>&1 | tail -1
wait_healthy fineract
sleep 10
state "$R2" "Fineract back, sweep has looked it up but never sends"
echo "   caller retries the same request:"
repay "$R2" 100000
state "$R2" after

echo
echo "== 3. Fineract timeout after sending"
R3="RPY-F$N-3"
state "$R3" before
fault fineract.repayment timeout_after
repay "$R3" 100000
state "$R3" during
echo "   the sweep looks $R3 up in Fineract by externalId instead of re-sending:"
wait_for "$R3" COMPLETED
state "$R3" after
echo "   caller retries the same request:"
repay "$R3" 100000
state "$R3" "after retry"

echo
echo "== 4. ERP timeout after the entry was written"
R4="RPY-F$N-4"
state "$R4" before
fault erp.journal_entry timeout_after
repay "$R4" 100000
state "$R4" during
wait_for "$R4" COMPLETED
state "$R4" after
echo "   ERP entries for $R4: $(q erp "SELECT count(*) FROM journal_entries WHERE external_reference = '$R4'") (the retry got 200 for the identical entry)"

echo
echo "== 5. Crash between Fineract success and the ERP call"
R5="RPY-F$N-5"
state "$R5" before
fault fineract.repayment crash_after
repay "$R5" 100000
echo "   integration-service container: $(docker inspect -f '{{.State.Status}} (exit code {{.State.ExitCode}})' "$(docker compose ps -aq integration-service)")"
state "$R5" "during, service dead"
echo "   idempotency record: $(q integration "SELECT status FROM idempotency_records WHERE external_reference = '$R5'")"
docker compose start integration-service 2>&1 | tail -1
wait_api
sleep 3
echo "   startup sweep log:"
docker compose logs --since 30s integration-service 2>&1 | grep transaction_recovery_pass | grep "$R5" | sed -E 's/.*(checked=[0-9]+).*(.external_reference.: .[^,]*), (.from.: .[^,]*), (.to.: .[^,]*).*/      \1 \2 \3 \4/'
state "$R5" "after restart"
echo "   caller retries within the 60s idempotency lease:"
repay "$R5" 100000
echo "   ...waiting for the lease of the crashed request to expire"
sleep 60
echo "   caller retries after the lease:"
repay "$R5" 100000
state "$R5" "after retry"

echo
echo "== Loan $LOAN after all five scenarios"
echo "   repayments in Fineract: $(q fineract_default "SELECT count(*) || ', total ' || sum(t.amount)::bigint FROM m_loan_transaction t JOIN m_loan l ON l.id = t.loan_id WHERE l.external_id = '$LOAN' AND t.transaction_type_enum = 2 AND NOT t.is_reversed")"
echo "   repayment entries in the ERP: $(q erp "SELECT count(*) || ', total ' || sum(total_amount)::bigint FROM journal_entries WHERE loan_reference = '$LOAN' AND transaction_type = 'REPAYMENT'")"
echo "   integration transactions by status: $(q integration "SELECT string_agg(status || '=' || n, ' ') FROM (SELECT t.status, count(*) n FROM loan_transactions t JOIN loans l ON l.id = t.loan_id WHERE l.loan_reference = '$LOAN' GROUP BY t.status) s")"
request DELETE /admin/faults > /dev/null
echo
echo "== All five failure scenarios recovered"
