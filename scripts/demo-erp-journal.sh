#!/usr/bin/env bash
set -euo pipefail

ERP="${ERP_URL:-http://localhost:8081}"
N="$(date +%H%M%S)"
LOAN="LN-DEMO-$N"

post() {
  curl -s -w '\n%{http_code}' -H 'Content-Type: application/json' -d "$1" "$ERP/api/journal-entries"
}

show() {
  local response="$1" expected="$2"
  local status="${response##*$'\n'}" body="${response%$'\n'*}"
  echo "HTTP $status (expected $expected) $(echo "$body" | jq -c 'if .lines then {external_reference, total_amount, lines: [.lines[] | "\(.account_code) DR \(.debit) CR \(.credit)"]} else . end')"
  [ "$status" = "$expected" ] || { echo "FAIL"; exit 1; }
}

entry() {
  local ref="$1" type="$2" lines="$3"
  jq -nc --arg ref "$ref" --arg loan "$LOAN" --arg type "$type" --argjson lines "$lines" \
    '{external_reference: $ref, entry_date: "2026-10-30", currency: "RWF", loan_reference: $loan, transaction_type: $type, lines: $lines}'
}

entries() {
  docker compose exec -T db psql -U postgres -d erp -tAc \
    "SELECT count(*) FROM journal_entries WHERE external_reference = '$1'"
}

balances() {
  for code in 1100 1200 4000; do
    curl -s "$ERP/api/accounts/$code/balance" | jq -c '{code, name, total_debits, total_credits, balance, line_count}'
  done
}

DISB="DISB-$N"
DISB_BODY="$(entry "$DISB" DISBURSEMENT '[{"account_code":"1200","debit":"5000000"},{"account_code":"1100","credit":"5000000"}]')"
REP="REP-$N"
REP_BODY="$(entry "$REP" REPAYMENT '[{"account_code":"1100","debit":"458400"},{"account_code":"1200","credit":"383400"},{"account_code":"4000","credit":"75000"}]')"

echo "== Balances before"
balances

echo
echo "== 1. Same disbursement entry posted 4 times"
show "$(post "$DISB_BODY")" 201
for _ in 2 3 4; do show "$(post "$DISB_BODY")" 200; done
echo "journal entries with $DISB: $(entries "$DISB")"

echo
echo "== 2. 10 concurrent posts of a new repayment entry"
seq 10 | xargs -P 10 -I{} curl -s -o /dev/null -w '%{http_code} ' -H 'Content-Type: application/json' -d "$REP_BODY" "$ERP/api/journal-entries"
echo
echo "journal entries with $REP: $(entries "$REP")"

echo
echo "== 3. Same reference, different amounts"
show "$(post "$(entry "$DISB" DISBURSEMENT '[{"account_code":"1200","debit":"4000000"},{"account_code":"1100","credit":"4000000"}]')")" 409

echo
echo "== 4. Unbalanced entry (debits 458400, credits 453400)"
show "$(post "$(entry "BAD-$N" REPAYMENT '[{"account_code":"1100","debit":"458400"},{"account_code":"1200","credit":"383400"},{"account_code":"4000","credit":"70000"}]')")" 422
echo "journal entries with BAD-$N: $(entries "BAD-$N")"

echo
echo "== 5. Unknown account"
show "$(post "$(entry "UNK-$N" DISBURSEMENT '[{"account_code":"9999","debit":"100"},{"account_code":"1100","credit":"100"}]')")" 422

echo
echo "== 6. Entry by reference"
curl -s "$ERP/api/journal-entries/$REP" | jq -c '{external_reference, loan_reference, transaction_type, total_amount, lines}'

echo
echo "== Balances after (1200 +5000000 -383400, 1100 -5000000 +458400, 4000 +75000)"
balances

echo
echo "== All ERP journal behaviours demonstrated"
