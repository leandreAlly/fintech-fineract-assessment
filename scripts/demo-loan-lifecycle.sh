#!/usr/bin/env bash
set -euo pipefail

API_KEY="${INTEGRATION_API_KEY:-dev-teller-key}"
curl() { command curl -H "X-API-Key: $API_KEY" "$@"; }

API="${INTEGRATION_URL:-http://localhost:8080}/api"
N="$(date +%H%M%S)"
CLIENT_UID="SME-$(printf '%06d' $(( (10#$N + 500000) % 1000000 )))"

post() {
  local path="$1" body="$2" expected="$3"
  local response status
  response="$(curl -s -w '\n%{http_code}' -H 'Content-Type: application/json' -d "$body" "$API$path")"
  status="${response##*$'\n'}"
  echo "POST $path $body" >&2
  echo "HTTP $status (expected $expected)" >&2
  [ "$status" = "$expected" ] || { echo "${response%$'\n'*}" >&2; echo "FAIL" >&2; exit 1; }
  echo "${response%$'\n'*}"
}

fineract_db() {
  docker compose exec -T db psql -U postgres -d fineract_default -tAc \
    "SELECT 'fineract m_loan: status_id=' || l.loan_status_id || ' (' || CASE l.loan_status_id
       WHEN 100 THEN 'Submitted and pending approval' WHEN 200 THEN 'Approved' WHEN 300 THEN 'Active'
       WHEN 600 THEN 'Closed (obligations met)' WHEN 700 THEN 'Overpaid' ELSE 'other' END || ')'
       || ' principal_outstanding=' || coalesce(l.principal_outstanding_derived, 0)::bigint
       || ' interest_outstanding=' || coalesce(l.interest_outstanding_derived, 0)::bigint
       || ' total_outstanding=' || coalesce(l.total_outstanding_derived, 0)::bigint
     FROM m_loan l WHERE l.external_id = '$1'"
}

state() {
  local loan="$1" expected_status="$2"
  local body
  body="$(curl -s "$API/loans/$loan")"
  echo "$body" | jq -c '{status, submitted_on, approved_on, disbursed_on, closed_on, balances}'
  fineract_db "$loan"
  [ "$(echo "$body" | jq -r .status)" = "$expected_status" ] || { echo "FAIL: expected $expected_status"; exit 1; }
}

repay() {
  local loan="$1" ref="$2" amount="$3"
  post "/loans/$loan/repayments" "$(jq -nc --arg r "$ref" --argjson a "$amount" '{external_reference: $r, amount: $a}')" 201 \
    | jq -c '{external_reference, status, amount, principal_portion, interest_portion, fee_portion, penalty_portion}'
}

ERP="${ERP_URL:-http://localhost:8081}"

erp_entry() {
  curl -s "$ERP/api/journal-entries/$1" \
    | jq -c '{external_reference, transaction_type, total_amount, lines: [.lines[] | "\(.account_code) DR \(.debit) CR \(.credit)"]}'
}

echo "== 0. Client: create, find by client_uid, validate"
post /clients "$(jq -nc --arg u "$CLIENT_UID" --arg r "CLI-$N" '{external_reference: $r, client_uid: $u, name: "Inyange Workshop Ltd"}')" 201 | jq -c .
curl -s "$API/clients/$CLIENT_UID" | jq -c '{client_uid, name, status, office, checks}'

echo
echo "== 1. Application: 5,000,000 RWF, SME Asset Finance (18% a year, 12 monthly instalments)"
LOAN="$(post /loans "$(jq -nc --arg u "$CLIENT_UID" --arg r "APP-$N" '{external_reference: $r, client_uid: $u, principal: 5000000}')" 201 | jq -r .loan_reference)"
echo "loan_reference: $LOAN"
state "$LOAN" SUBMITTED_AND_PENDING_APPROVAL

echo
echo "== 2. Approval"
post "/loans/$LOAN/approve" "$(jq -nc --arg r "APR-$N" '{external_reference: $r}')" 200 > /dev/null
state "$LOAN" APPROVED

echo
echo "== 3. Disbursement"
post "/loans/$LOAN/disburse" "$(jq -nc --arg r "DSB-$N" '{external_reference: $r}')" 201 \
  | jq -c '{external_reference, status, type, amount}'
state "$LOAN" ACTIVE

echo
echo "== 4. Partial repayment 1 of 3 (one instalment)"
repay "$LOAN" "RPY-$N-1" 458400
state "$LOAN" ACTIVE

echo
echo "== 5. Partial repayment 2 of 3"
repay "$LOAN" "RPY-$N-2" 300000
state "$LOAN" ACTIVE

echo
echo "== 6. Partial repayment 3 of 3"
repay "$LOAN" "RPY-$N-3" 1000000
state "$LOAN" ACTIVE

echo
echo "== 7. Final payoff of Fineract's total outstanding"
PAYOFF="$(curl -s "$API/loans/$LOAN" | jq -r .balances.total_outstanding)"
echo "total_outstanding reported by Fineract: $PAYOFF"
repay "$LOAN" "RPY-$N-FINAL" "$PAYOFF"
state "$LOAN" CLOSED_OBLIGATIONS_MET

echo
echo "== 8. Transactions recorded with Fineract's breakdown (no Fineract ids)"
curl -s "$API/loans/$LOAN" | jq -c '.transactions[] | {external_reference, type, amount, principal_portion, interest_portion}'
curl -s "$API/loans/$LOAN" | jq -e '[.transactions[] | select(.type == "REPAYMENT") | .principal_portion] | add == 5000000' > /dev/null \
  && echo "repayment principal portions add up to the 5,000,000 disbursed"
curl -s "$API/transactions/RPY-$N-2" | jq -c .

echo
echo "== 9. ERP: one balanced journal entry per Fineract transaction, same external_reference"
for ref in "DSB-$N" "RPY-$N-1" "RPY-$N-2" "RPY-$N-3" "RPY-$N-FINAL"; do erp_entry "$ref"; done

echo
echo "== 10. ERP ledger for this loan vs Fineract"
docker compose exec -T db psql -U postgres -d erp -tAc \
  "SELECT a.code || ' ' || a.name || ': debits=' || sum(l.debit)::bigint || ' credits=' || sum(l.credit)::bigint
   FROM journal_lines l JOIN journal_entries e ON e.id = l.entry_id JOIN accounts a ON a.id = l.account_id
   WHERE e.loan_reference = '$LOAN' GROUP BY a.code, a.name ORDER BY a.code"
docker compose exec -T db psql -U postgres -d fineract_default -tAc \
  "SELECT 'fineract: principal_disbursed=' || principal_disbursed_derived::bigint || ' principal_repaid=' || principal_repaid_derived::bigint
     || ' interest_repaid=' || interest_repaid_derived::bigint || ' total_repaid=' || total_repayment_derived::bigint
   FROM m_loan WHERE external_id = '$LOAN'"
docker compose exec -T db psql -U postgres -d integration -tAc \
  "SELECT 'integration: ' || count(*) || ' transactions, ' || count(*) FILTER (WHERE t.status = 'COMPLETED') || ' COMPLETED'
   FROM loan_transactions t JOIN loans l ON l.id = t.loan_id WHERE l.loan_reference = '$LOAN'"

echo
echo "== Loan $LOAN closed with obligations met, ledger posted in the ERP"
