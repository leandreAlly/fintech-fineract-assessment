#!/usr/bin/env bash
set -euo pipefail

API_KEY="${INTEGRATION_API_KEY:-dev-teller-key}"
curl() { command curl -H "X-API-Key: $API_KEY" "$@"; }

API="${INTEGRATION_URL:-http://localhost:8080}/api/clients"
N="$(date +%H%M%S)"
UID1="SME-$N"
REF1="CLI-$N-A"

post() {
  curl -s -w '\n%{http_code}' -H 'Content-Type: application/json' -d "$1" "$API"
}

show() {
  local response="$1" expected="$2"
  local status="${response##*$'\n'}" body="${response%$'\n'*}"
  echo "HTTP $status (expected $expected) $(echo "$body" | jq -c .)"
  [ "$status" = "$expected" ] || { echo "FAIL"; exit 1; }
}

client_body() {
  jq -nc --arg ref "$1" --arg uid "$2" --arg name "$3" '{external_reference: $ref, client_uid: $uid, name: $name}'
}

q() {
  docker compose exec -T db psql -U postgres -d "$1" -tAc "$2"
}

counts() {
  echo "Fineract clients with externalId $1: $(q fineract_default "SELECT count(*) FROM m_client WHERE external_id = '$1'")" \
    "| integration clients: $(q integration "SELECT count(*) FROM clients WHERE client_uid = '$1'")"
}

echo "== 1. Create client $UID1"
show "$(post "$(client_body "$REF1" "$UID1" 'Umucyo Traders Ltd')")" 201

echo "== 2. Find by client_uid and validate (exists, active, correct name)"
curl -s "$API/$UID1" | jq -c .
curl -s "$API/$UID1" | jq -e '.checks == {exists: true, active: true, name_matches: true}' > /dev/null || { echo "FAIL"; exit 1; }

echo "== 3. Same reference + same payload, 3 more times: original result, no new client"
for i in 1 2 3; do show "$(post "$(client_body "$REF1" "$UID1" 'Umucyo Traders Ltd')")" 200; done
counts "$UID1"

echo "== 4. Same reference + different payload: 409"
show "$(post "$(client_body "$REF1" "$UID1" 'Umucyo Traders')")" 409

echo "== 5. New reference reusing client_uid: 409"
show "$(post "$(client_body "CLI-$N-B" "$UID1" 'Umucyo Traders Ltd')")" 409
counts "$UID1"

echo "== 6. Ten concurrent requests with the same new reference"
UID2="SME-$(printf '%06d' $(( (10#$N + 1) % 1000000 )))"
out="$(mktemp -d)"
for i in $(seq 1 10); do
  curl -s -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
    -d "$(client_body "CLI-$N-C" "$UID2" 'Inzozi Bakery')" "$API" > "$out/$i" &
done
wait
echo "responses: $(cat "$out"/* | sort | uniq -c | awk '{printf "%sx%s ", $1, $2}')"
rm -rf "$out"
counts "$UID2"
[ "$(q fineract_default "SELECT count(*) FROM m_client WHERE external_id = '$UID2'")" = 1 ] || { echo "FAIL"; exit 1; }

echo "== 7. Unknown client_uid: 404"
curl -s -w ' HTTP %{http_code}\n' "$API/SME-999999"

echo "== Client identity behaviours demonstrated"
