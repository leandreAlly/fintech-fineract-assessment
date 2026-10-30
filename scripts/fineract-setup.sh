#!/usr/bin/env bash
set -euo pipefail

API_KEY="${INTEGRATION_API_KEY:-dev-teller-key}"
curl() { command curl -H "X-API-Key: $API_KEY" "$@"; }

BASE="${FINERACT_URL:-https://localhost:8443/fineract-provider/api/v1}"
TENANT="${FINERACT_TENANT:-default}"
ADMIN="${FINERACT_ADMIN_USERNAME:-mifos}:${FINERACT_ADMIN_PASSWORD:-password}"
API_USER="${FINERACT_API_USERNAME:-integration_service}"
API_PASSWORD="${FINERACT_API_PASSWORD:-Integr4tion-Svc-26!}"
SYNC="${INTEGRATION_URL:-http://localhost:8080}/api/accounts/sync"
CONFIG="$(cd "$(dirname "$0")/../fineract/configuration" && pwd)"

OFFICE_NAME="Kigali Main Office"
OFFICE_EXTERNAL_ID="OFFICE-KGL-MAIN"
ROLE_NAME="integration-service"

api() {
  local method="$1" path="$2" body="${3:-}" auth="${4:-$ADMIN}"
  local args=(-sk -u "$auth" -H "Fineract-Platform-TenantId: $TENANT" -H "Content-Type: application/json" -X "$method" -w '\n%{http_code}')
  [ -n "$body" ] && args+=(-d "$body")
  local response status
  response="$(curl "${args[@]}" "$BASE$path")"
  status="${response##*$'\n'}"
  if [ "$status" -ge 400 ]; then
    echo "FAIL: $method $path -> $status" >&2
    echo "${response%$'\n'*}" | jq . >&2 || true
    exit 1
  fi
  echo "${response%$'\n'*}"
}

echo "== Currency: RWF"
api PUT /currencies '{"currencies": ["RWF"]}' > /dev/null
api GET /currencies | jq -c '[.selectedCurrencyOptions[] | {code, decimalPlaces}]'

echo "== Office: $OFFICE_NAME"
office_id="$(api GET /offices | jq -r --arg n "$OFFICE_NAME" '.[] | select(.name == $n) | .id')"
if [ -z "$office_id" ]; then
  office_id="$(api POST /offices "$(jq -nc --arg n "$OFFICE_NAME" --arg e "$OFFICE_EXTERNAL_ID" \
    '{name: $n, externalId: $e, parentId: 1, openingDate: "2026-01-01", dateFormat: "yyyy-MM-dd", locale: "en"}')" | jq -r .officeId)"
  echo "created office $office_id"
else
  echo "exists (office $office_id)"
fi

echo "== Role '$ROLE_NAME' with least-privilege permissions"
role_id="$(api GET /roles | jq -r --arg n "$ROLE_NAME" '.[] | select(.name == $n) | .id')"
if [ -z "$role_id" ]; then
  role_id="$(api POST /roles "$(jq -nc --arg n "$ROLE_NAME" '{name: $n, description: "Integration Service API access"}')" | jq -r .resourceId)"
  echo "created role $role_id"
else
  echo "exists (role $role_id)"
fi
api PUT "/roles/$role_id/permissions" "$(jq -c '{permissions: (map({key: ., value: true}) | from_entries)}' "$CONFIG/integration-service-permissions.json")" > /dev/null
api GET "/roles/$role_id/permissions" | jq -c '[.permissionUsageData[] | select(.selected) | .code] | sort'

echo "== API user '$API_USER'"
user_id="$(api GET /users | jq -r --arg u "$API_USER" '.[] | select(.username == $u) | .id')"
if [ -z "$user_id" ]; then
  user_id="$(api POST /users "$(jq -nc --arg u "$API_USER" --arg p "$API_PASSWORD" --argjson o "$office_id" --argjson r "$role_id" \
    '{username: $u, firstname: "Integration", lastname: "Service", email: "integration-service@example.test",
      officeId: $o, roles: [$r], sendPasswordToEmail: false, passwordNeverExpires: true, password: $p, repeatPassword: $p}')" | jq -r .resourceId)"
  echo "created user $user_id"
else
  echo "exists (user $user_id)"
fi
api GET "/users/$user_id" | jq -c '{username, officeName, roles: [.selectedRoles[].name]}'

echo "== GL accounts: ERP chart of accounts synced through the Integration Service"
curl -sf -X POST "$SYNC/all" | jq -c '.[] | {external_reference, erp_type, status}'
not_synced="$(curl -sf "$SYNC" | jq '[.[] | select(.external_reference | test("^GL-[0-9]{4}$")) | select(.status != "SYNCED")] | length')"
[ "$not_synced" = 0 ] || { echo "FAIL: $not_synced ERP accounts are not SYNCED; the loan product needs them"; exit 1; }

echo "== Loan product: SME Asset Finance (cash-based accounting)"
product_id="$(api GET /loanproducts | jq -r '.[] | select(.name == "SME Asset Finance") | .id')"
if [ -z "$product_id" ]; then
  gl_ids="$(api GET /glaccounts | jq -c 'map({key: .glCode, value: .id}) | from_entries')"
  mapping="$(jq -c --argjson ids "$gl_ids" 'with_entries(.value = $ids[.value])' "$CONFIG/cash-accounting-mapping.json")"
  echo "$mapping" | jq -e 'all(.[]; . != null)' > /dev/null || { echo "FAIL: a mapped GL code is missing in Fineract"; exit 1; }
  product_id="$(api POST /loanproducts "$(jq -c --argjson m "$mapping" '. + $m' "$CONFIG/loan-product-sme-asset-finance.json")" | jq -r .resourceId)"
  echo "created loan product $product_id"
else
  echo "exists (loan product $product_id)"
fi
api GET "/loanproducts/$product_id" | jq -c '{name, shortName, externalId, currency: .currency.code, principal, numberOfRepayments,
  repaymentEvery, repaymentFrequency: .repaymentFrequencyType.value, interestRatePerPeriod, interestRateFrequency: .interestRateFrequencyType.value,
  interestType: .interestType.value, amortization: .amortizationType.value, daysInYear: .daysInYearType.value, daysInMonth: .daysInMonthType.value,
  multiDisburseLoan, accountingRule: .accountingRule.value}'
api GET "/loanproducts/$product_id" | jq -c '.accountingMappings | to_entries | map({(.key): .value.glCode}) | add'

echo "== Least privilege check as '$API_USER'"
api GET "/loanproducts/$product_id" "" "$API_USER:$API_PASSWORD" | jq -c '{readable_product: .name}'
status="$(curl -sk -o /dev/null -w '%{http_code}' -u "$API_USER:$API_PASSWORD" -H "Fineract-Platform-TenantId: $TENANT" \
  -H 'Content-Type: application/json' -X POST "$BASE/offices" -d '{"name":"Should Fail","parentId":1,"openingDate":"2026-01-01","dateFormat":"yyyy-MM-dd","locale":"en"}')"
echo "create office as $API_USER -> $status (expected 403)"
[ "$status" = 403 ] || { echo "FAIL: API user can create offices"; exit 1; }

echo "== Fineract setup complete"
