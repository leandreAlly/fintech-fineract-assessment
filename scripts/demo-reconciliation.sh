#!/usr/bin/env bash
set -euo pipefail

API_KEY="${INTEGRATION_API_KEY:-dev-teller-key}"
curl() { command curl -H "X-API-Key: $API_KEY" "$@"; }

API="${INTEGRATION_URL:-http://localhost:8080}/api"
ERP="${ERP_URL:-http://localhost:8081}/api"
FINERACT="${FINERACT_URL:-https://localhost:8443/fineract-provider/api/v1}"
ADMIN="${FINERACT_ADMIN:-mifos:password}"
N="$(date +%H%M%S)"
CLIENT_UID="SME-$(printf '%06d' $(( (10#$N + 300000) % 1000000 )))"
TODAY="$(date +%Y-%m-%d)"

post() {
  curl -s -H 'Content-Type: application/json' -d "$2" "$1"
}

fineract() {
  curl -sk -u "$ADMIN" -H 'Fineract-Platform-TenantId: default' -H 'Content-Type: application/json' "$@"
}

erp_entry() {
  jq -nc --arg ref "$1" --arg loan "$LOAN" --arg d "$TODAY" --argjson a "$2" \
    '{external_reference: $ref, entry_date: $d, currency: "RWF", description: "manual entry", loan_reference: $loan, transaction_type: "REPAYMENT",
      lines: [{account_code: "1100", debit: $a}, {account_code: "1200", credit: $a}]}'
}

fineract_repayment() {
  jq -nc --arg ref "$1" --arg d "$TODAY" --argjson a "$2" \
    '{transactionDate: $d, transactionAmount: $a, externalId: $ref, paymentTypeId: 1, dateFormat: "yyyy-MM-dd", locale: "en"}'
}

report() {
  post "$API/reconciliation" "$(jq -nc --arg loan "$LOAN" '{loan_references: [$loan]}')" \
    | jq -r '"   summary: \(.summary | to_entries | map("\(.key)=\(.value)") | join(" "))",
             (.items[] | "   \(.status | . + " " * (20 - length)) \(.external_reference | . + " " * (22 - length)) fineract=\(.fineract.net_amount // "-") erp=\(.erp.net_amount // "-") diff=\(.difference)\(if .note then "  (" + .note + ")" else "" end)")'
}

repair() {
  echo "   POST /api/reconciliation/repairs {external_reference: $1}"
  post "$API/reconciliation/repairs" "$(jq -nc --arg r "$1" --arg loan "$LOAN" '{external_reference: $r, loan_reference: $loan}')" \
    | jq -c '"   -> " + (if .action then "action=\(.action) status=\(.item.status)" else "\(.error.code): \(.error.message)" end)' -r
}

echo "== Setup: a loan disbursed and repaid three times through the Integration Service"
post "$API/clients" "$(jq -nc --arg u "$CLIENT_UID" --arg r "CLI-K$N" '{external_reference: $r, client_uid: $u, name: "Isoko Grain Traders"}')" > /dev/null
LOAN="$(post "$API/loans" "$(jq -nc --arg u "$CLIENT_UID" --arg r "APP-K$N" '{external_reference: $r, client_uid: $u, principal: 5000000}')" | jq -r .loan_reference)"
post "$API/loans/$LOAN/approve" "$(jq -nc --arg r "APR-K$N" '{external_reference: $r}')" > /dev/null
post "$API/loans/$LOAN/disburse" "$(jq -nc --arg r "DSB-K$N" '{external_reference: $r}')" | jq -r '"   \(.external_reference) \(.status)"'
for i in 1 2 3; do
  post "$API/loans/$LOAN/repayments" "$(jq -nc --arg r "RPY-K$N-$i" '{external_reference: $r, amount: 458400}')" | jq -r '"   \(.external_reference) \(.status)"'
done
LOAN_ID="$(fineract "$FINERACT/loans/external-id/$LOAN" | jq .id)"
echo "   loan $LOAN"

echo
echo "== Seeding the other outcomes deliberately"
echo "   MISSING_IN_ERP: repayment MISS-ERP-$N posted straight into Fineract (e.g. from the Fineract UI)"
fineract -X POST -d "$(fineract_repayment "MISS-ERP-$N" 100000)" "$FINERACT/loans/$LOAN_ID/transactions?command=repayment" > /dev/null
echo "   MISSING_IN_FINERACT: manual journal entry MISS-FIN-$N posted straight into the ERP"
post "$ERP/journal-entries" "$(erp_entry "MISS-FIN-$N" 75000)" > /dev/null
echo "   AMOUNT_MISMATCH: MISMATCH-$N is 100000 in Fineract but 90000 in the ERP"
fineract -X POST -d "$(fineract_repayment "MISMATCH-$N" 100000)" "$FINERACT/loans/$LOAN_ID/transactions?command=repayment" > /dev/null
post "$ERP/journal-entries" "$(erp_entry "MISMATCH-$N" 90000)" > /dev/null
echo "   Reversal: RPY-K$N-2 reversed in Fineract after it was posted to the ERP (Fineract then re-splits RPY-K$N-3)"
TXN_ID="$(fineract "$FINERACT/loans/$LOAN_ID/transactions/external-id/RPY-K$N-2" | jq .id)"
fineract -X POST -d "$(jq -nc --arg d "$TODAY" '{transactionDate: $d, transactionAmount: 0, dateFormat: "yyyy-MM-dd", locale: "en"}')" \
  "$FINERACT/loans/$LOAN_ID/transactions/$TXN_ID" | jq -c '{resourceId, defaultUserMessage} | with_entries(select(.value != null))' | sed 's/^/   fineract: /'
echo "   fineract manuallyReversed: $(fineract "$FINERACT/loans/$LOAN_ID/transactions/external-id/RPY-K$N-2" | jq .manuallyReversed)"

echo
echo "== POST /api/reconciliation (read-only)"
report

echo
echo "== Explicit, idempotent repairs"
repair "MISS-ERP-$N"
repair "MISS-ERP-$N"
repair "RPY-K$N-2"
repair "RPY-K$N-2"
repair "MISMATCH-$N"
repair "MISS-FIN-$N"

echo
echo "== POST /api/reconciliation after the repairs"
report

echo
echo "== ERP entries written by the repairs"
curl -s "$ERP/journal-entries?external_reference=MISS-ERP-$N&external_reference=REV-RPY-K$N-2" \
  | jq -r '.[] | "   \(.entry_id) \(.external_reference) \(.transaction_type): " + ([.lines[] | "\(.account_code) DR \(.debit) CR \(.credit)"] | join(", "))'
