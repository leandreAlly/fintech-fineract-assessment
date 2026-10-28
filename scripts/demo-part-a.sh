#!/usr/bin/env bash
set -euo pipefail

BASE="${FINERACT_URL:-https://localhost:8443/fineract-provider/api/v1}"
TENANT="${FINERACT_TENANT:-default}"
ADMIN="${FINERACT_ADMIN:-mifos:password}"
SUFFIX="$(date +%s)"

call() {
  local auth="$1" method="$2" path="$3" body="${4:-}"
  local args=(-sk -u "$auth" -H "Fineract-Platform-TenantId: $TENANT" -H "Content-Type: application/json" -X "$method" -w '\n%{http_code}')
  [ -n "$body" ] && args+=(-d "$body")
  curl "${args[@]}" "$BASE$path"
}

show() {
  local response="$1" expected="$2"
  local status="${response##*$'\n'}" body="${response%$'\n'*}"
  echo "HTTP $status (expected $expected)"
  echo "$body" | jq -c '{resourceId, resourceExternalId, httpStatusCode, userMessageGlobalisationCode, defaultUserMessage} | with_entries(select(.value != null))'
  [ "$status" = "$expected" ] || { echo "FAIL"; exit 1; }
}

sql() {
  docker compose exec -T db psql -U postgres -d fineract_default -c "$1"
}

account() {
  jq -nc --arg ref "$1" --arg code "$2" --arg name "$3" \
    '{externalId: $ref, glCode: $code, name: $name, type: 1, usage: 1, manualEntriesAllowed: true}'
}

echo "== 1. external_id is persisted on the GL account and unique"
show "$(call "$ADMIN" POST /glaccounts/sync "$(account GL-1200 1200 'Loan Receivable')")" 200
sql "SELECT id, gl_code, name, external_id FROM acc_gl_account WHERE external_id = 'GL-1200';"
sql "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'acc_gl_account_external_id_key';"

echo "== 2. same reference + identical payload returns the existing account"
show "$(call "$ADMIN" POST /glaccounts/sync "$(account GL-1200 1200 'Loan Receivable')")" 200
sql "SELECT count(*) AS accounts_with_ref FROM acc_gl_account WHERE external_id = 'GL-1200';"

echo "== 3. same reference + different payload is rejected with 409"
show "$(call "$ADMIN" POST /glaccounts/sync "$(account GL-1200 1200 'Loans Receivable')")" 409

echo "== 4. endpoint is protected by Fineract's permission model (SYNC_GLACCOUNT)"
role_id=$(call "$ADMIN" POST /roles "$(jq -nc --arg n "gl-sync-demo-$SUFFIX" '{name: $n, description: "Part A demo role"}')" | head -1 | jq -r .resourceId)
password="Demo-$(openssl rand -hex 8 | tr -s '0-9a-f')-A1!"
user="gl_sync_demo_$SUFFIX"
user_id=$(call "$ADMIN" POST /users "$(jq -nc --arg u "$user" --arg p "$password" --argjson r "$role_id" \
  '{username: $u, firstname: "GL", lastname: "Sync", email: "gl-sync-demo@example.test", officeId: 1, roles: [$r], sendPasswordToEmail: false, password: $p, repeatPassword: $p}')" | head -1 | jq -r .resourceId)
[ "$user_id" != "null" ] || { echo "FAIL: could not create demo user"; exit 1; }
echo "-- user $user has a role without SYNC_GLACCOUNT"
show "$(call "$user:$password" POST /glaccounts/sync "$(account "GL-9$SUFFIX" "9$SUFFIX" 'Permission Demo')")" 403
call "$ADMIN" PUT "/roles/$role_id/permissions" '{"permissions": {"SYNC_GLACCOUNT": true}}' > /dev/null
echo "-- after granting SYNC_GLACCOUNT"
show "$(call "$user:$password" POST /glaccounts/sync "$(account "GL-9$SUFFIX" "9$SUFFIX" 'Permission Demo')")" 200

echo "== All Part A behaviours demonstrated"
