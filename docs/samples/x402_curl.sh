#!/usr/bin/env bash
# Free-tier round trip with curl and jq: see what a stamp costs, buy one on the
# free tier, upload a file, download it. Paying needs an EIP-3009 signature,
# which curl cannot make; use x402_client.py or x402_client.mjs with --paid.
#
#   ./x402_curl.sh http://localhost:8000
set -euo pipefail
GW=${1:-http://localhost:8000}
FILE=$(mktemp)
BODY=$(mktemp)
trap 'rm -f "$FILE" "$BODY"' EXIT

# 1. What a stamp costs, without buying one.
#
# GET /api/v1/pricing quotes it for free where the gateway has it. Otherwise
# fall back to reading the 402 of an unpaid purchase. That fallback is only
# safe on a gateway with x402 on: with x402 off the same request is not refused
# but served, and buys a stamp. So check /health first, and stop unless the
# answer really is a 402.
echo "== price of a stamp"
if [ "$(curl -s -o "$BODY" -w '%{http_code}' "$GW/api/v1/pricing")" = "200" ]; then
  if [ "$(jq -r .x402_enabled "$BODY")" != "true" ]; then
    echo "x402 is not enabled on $GW; nothing to pay." >&2
    exit 1
  fi
  jq '{network, asset, pay_to, stamp_purchase: .quotes.stamp_purchase}' "$BODY"
else
  if [ "$(curl -sf "$GW/health" | jq -r '.x402.enabled // false')" != "true" ]; then
    echo "x402 is not enabled on $GW; nothing to pay, not probing." >&2
    exit 1
  fi
  STATUS=$(curl -s -o "$BODY" -w '%{http_code}' -X POST "$GW/api/v1/stamps/" \
    -H 'Content-Type: application/json' -d '{}')
  if [ "$STATUS" != "402" ]; then
    echo "Expected 402 from the unpaid probe, got $STATUS; stopping." >&2
    exit 1
  fi
  # `accepts` is at the top level in the x402 spec; older gateways nest it under
  # `detail`, so read either.
  jq '(.accepts // .detail.accepts)[0] | {maxAmountRequired, network, payTo, asset}' "$BODY"
fi

# 2. Buy a stamp on the free tier. Success is 201 Created. -f fails on any 4xx/5xx.
echo "== buy a stamp (free tier)"
STAMP=$(curl -sf -X POST "$GW/api/v1/stamps/" -H 'X-Payment-Mode: free' \
  -H 'Content-Type: application/json' -d '{"size": "small", "duration_hours": 25}' | jq -r .batchID)
echo "batchID $STAMP"

echo "== wait until usable"
USABLE=false
for _ in $(seq 60); do
  if [ "$(curl -sf "$GW/api/v1/stamps/$STAMP/check" | jq -r .can_upload)" = "true" ]; then
    USABLE=true
    break
  fi
  sleep 5
done
if [ "$USABLE" != "true" ]; then
  echo "Stamp did not become usable in time" >&2
  exit 1
fi

# 3. Upload is multipart/form-data with the file in a field named "file".
echo "== upload"
echo '{"hello": "swarm"}' > "$FILE"
REF=$(curl -sf -X POST "$GW/api/v1/data/?stamp_id=$STAMP&content_type=application/json" \
  -H 'X-Payment-Mode: free' -F "file=@$FILE;type=application/json" | jq -r .reference)
echo "reference $REF"

echo "== download"
curl -sf "$GW/api/v1/data/$REF"
