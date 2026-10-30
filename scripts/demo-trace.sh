#!/usr/bin/env bash
set -euo pipefail

API_KEY="${INTEGRATION_ADMIN_API_KEY:-dev-admin-key}"
curl() { command curl -H "X-API-Key: $API_KEY" "$@"; }

API="${INTEGRATION_URL:-http://localhost:8080}/api"
N="$(date +%H%M%S)"
CLIENT_UID="SME-$(printf '%06d' $(( (10#$N + 100000) % 1000000 )))"
REF="${TRACE_REFERENCE:-FIN-2026-$N}"
SINCE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

post() {
  curl -s -H 'Content-Type: application/json' -H "X-Correlation-ID: $1" -d "$3" "$API$2"
}

post "setup-$N" /clients "$(jq -nc --arg u "$CLIENT_UID" --arg r "CLI-T$N" '{external_reference: $r, client_uid: $u, name: "Akagera Motors Ltd"}')" > /dev/null
LOAN="$(post "setup-$N" /loans "$(jq -nc --arg u "$CLIENT_UID" --arg r "APP-T$N" '{external_reference: $r, client_uid: $u, principal: 5000000}')" | jq -r .loan_reference)"
post "setup-$N" "/loans/$LOAN/approve" "$(jq -nc --arg r "APR-T$N" '{external_reference: $r}')" > /dev/null
post "setup-$N" "/loans/$LOAN/disburse" "$(jq -nc --arg r "DSB-T$N" '{external_reference: $r}')" > /dev/null
curl -s -X POST -H 'Content-Type: application/json' -d '{"operation": "erp.journal_entry", "mode": "timeout_after"}' "$API/admin/faults" > /dev/null

echo "== A teller posts repayment $REF on $LOAN; the ERP answer is lost on the first attempt"
curl -s -i -H 'Content-Type: application/json' -H "X-Correlation-ID: teller-$N" \
  -d "$(jq -nc --arg r "$REF" '{external_reference: $r, amount: 458400}')" "$API/loans/$LOAN/repayments" \
  | tr -d '\r' | awk 'NR==1 || tolower($0) ~ /^x-correlation-id/ {print "   " $0} /^\{/ {print "   " $0}' | cut -c1-200
sleep 8

echo
echo "== Question: what happened to $REF?"
echo
echo "-- 1. One API call"
echo "   GET /api/transactions/$REF"
curl -s "$API/transactions/$REF" | jq -c . | sed 's/^/   /'

echo
echo "-- 2. Logs: every line about $REF, from both services, oldest first"
echo "   docker compose logs integration-service mock-erp | jq 'select(.external_reference == \"$REF\")'"
docker compose logs --no-log-prefix --since "$SINCE" integration-service mock-erp 2>/dev/null \
  | jq -c --arg ref "$REF" 'select(type == "object" and .external_reference == $ref)
      | {timestamp, service, event, correlation_id, status, from_status, loan_reference, client_uid, fineract_transaction_id, erp_transaction_id, error}
      | with_entries(select(.value != null))' 2>/dev/null \
  | sort | sed 's/^/   /'

echo
echo "-- 3. Logs: the full request behind the first attempt (correlation_id teller-$N)"
docker compose logs --no-log-prefix --since "$SINCE" integration-service mock-erp 2>/dev/null \
  | jq -c --arg id "teller-$N" 'select(type == "object" and .correlation_id == $id) | {timestamp, service, event, status, method, path, status_code, error} | with_entries(select(.value != null))' 2>/dev/null \
  | sort | sed 's/^/   /'
